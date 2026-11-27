"""Check installed native dependencies without opening a desktop window.

Run after the installer has verified and installed the bundle. These checks
load real extension modules; a wheel inventory alone cannot detect a missing
Windows DLL. The canonical OCR diagnostic performs its own bounded synthetic
recognition check. No model session, network request or user document is used.
"""
from __future__ import annotations

import argparse
from importlib import import_module
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys

MAX_MANIFEST = 8 * 1024 * 1024
MAX_FILES = 50000
TARGET = 'Windows x64 / CPython 3.11'
NATIVE_MODULES = (
    ('wx desktop extension', 'wx._core'),
    ('PDFium renderer', 'pypdfium2.raw'),
    ('lxml document parser', 'lxml.etree'),
    ('Pillow image extension', 'PIL._imaging'),
    ('QuickJS citation extension', 'quickjs'),
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate installation manifest key: ' + key)
        result[key] = value
    return result


def _manifest(root):
    path = root / 'bundle-manifest.json'
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not 0 < info.st_size <= MAX_MANIFEST):
        raise ValueError('The installation manifest must be a bounded regular file.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) != info.st_size:
        raise ValueError('The installation manifest changed while being read.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    required = {'schema', 'target', 'bundled_python', 'bundled_model', 'bundled_ocr', 'files'}
    optional_flags = ('bundled_native_runtime', 'bundled_audio', 'bundled_audio_runtime',
                      'bundled_citation_runtime')
    identities = {'source_commit': 40, 'source_tree': 40,
                  'parent_source_commit': 40, 'parent_manifest_sha256': 64}
    optional = {*optional_flags, *identities, 'use_scope', 'native_launcher'}
    if (type(value) is not dict or not required <= value.keys()
            or value.keys() - required - optional
            or type(value['schema']) is not int or value['schema'] != 1
            or value['target'] != TARGET
            or any(type(value[name]) is not bool
                   for name in ('bundled_python', 'bundled_model', 'bundled_ocr'))
            or type(value['files']) is not dict or not 1 <= len(value['files']) <= MAX_FILES):
        raise ValueError('Unsupported installation manifest or optional-component flags.')
    for name in optional_flags:
        if name in value and type(value[name]) is not bool:
            raise ValueError('The ' + name + ' installation flag must be a boolean.')
    for name, length in identities.items():
        if (name in value and (not isinstance(value[name], str)
                or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value[name]))):
            raise ValueError('Invalid installation source identity: ' + name)
    if 'use_scope' in value and value['use_scope'] != 'private-personal-installation':
        raise ValueError('Unsupported installation use scope.')
    folded = set()
    for name, digest in value['files'].items():
        if not isinstance(name, str):
            raise ValueError('Invalid installation inventory path.')
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or path.as_posix() != name
                or '..' in path.parts or '\\' in name or ':' in name
                or any(ord(c) < 32 for c in name)
                or name == 'bundle-manifest.json' or name.casefold() in folded
                or any(part.endswith((' ', '.')) for part in path.parts)
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('Invalid or duplicate installation inventory entry.')
        folded.add(name.casefold())
    from .native_launcher import validate_manifest_entry
    validate_manifest_entry(value)
    return value


def _checked_bundled_audio(root):
    """Use one identity report, then probe only its verified executable paths."""
    from .audio_runtime import inspect_bundled_audio
    selected = inspect_bundled_audio(root)
    if selected is None:
        return {'status': 'NOT_INCLUDED'}
    from .audio.models import AudioBudget
    from .audio.process import run_process
    version = selected['version']
    if not isinstance(version, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.+_-]{0,100}', version):
        raise ValueError('Invalid verified audio runtime version.')
    versions = {}
    for name in ('ffmpeg', 'ffprobe'):
        executable = selected['tools'][name]
        output = run_process([executable, '-hide_banner', '-version'],
            budget=AudioBudget(timeout=30, max_log_bytes=65536)).stdout.decode('utf-8', 'strict')
        lines = output.splitlines()
        if not lines or lines[0].split()[:3] != [name, 'version', version]:
            raise ValueError('Bundled audio engine reported a different version.')
        versions[name] = lines[0]
    return dict(selected, status='PASS', native_execution='PASS', versions=versions,
                execution_scope='ffmpeg and ffprobe version probes',
                actual_asr='NOT_RUN', microphone='NOT_RUN', rights='NOT_ASSESSED')


def check_native_dependencies(bundle_root):
    """Return a truthful host-specific report; failure never means installed."""
    report = {'status': 'FAIL', 'platform': sys.platform, 'machine': platform.machine(),
              'python': platform.python_version(), 'windows_execution': 'NOT_RUN',
              'native_gui': 'NOT_RUN', 'windows_nvda': 'NOT_RUN', 'checks': [],
              'semantic': {'status': 'NOT_INCLUDED'}, 'ocr': {'status': 'NOT_INCLUDED'},
              'native_launcher': {'status': 'NOT_INCLUDED'}, 'audio': {'status': 'NOT_INCLUDED'},
              'citations': {'status': 'NOT_RUN'}}
    component = 'installation manifest'
    _runtime_handle = None
    try:
        root = Path(bundle_root).absolute()
        manifest = _manifest(root)
        report['bundle_target'] = manifest['target']
        if 'source_commit' in manifest:
            report['source_commit'] = manifest['source_commit']
        if 'ResearchLibrary.exe' in manifest['files']:
            component = 'native launch entry identity'
            report['native_launcher']['status'] = 'FAIL'
            from .native_launcher import verify_payload
            report['native_launcher'] = verify_payload(root, manifest)
            report['checks'].append(component)
        for name, flag in (('semantic', 'bundled_model'), ('ocr', 'bundled_ocr')):
            if manifest[flag]:
                report[name]['status'] = 'NOT_RUN'
        component = 'bundled native runtime'
        native_runtime = import_module('carr_library.native_runtime')
        # Keep the shared loader's returned directory handle alive until all
        # extension loading finishes, including delayed semantic dependencies.
        _runtime_handle = native_runtime.activate_bundled_runtime(bundle_root=root)
        for component, module in NATIVE_MODULES:
            import_module(module)
            report['checks'].append(component)
        if manifest['bundled_model']:
            report['semantic']['status'] = 'FAIL'
            component = 'ONNX Runtime offline initialization'
            # The shared policy sets ORT_DISABLE_TELEMETRY before importing the
            # pinned runtime, then calls its official telemetry-disable API.
            policy = import_module('carr_library.offline_policy')
            policy.disable_native_telemetry()
            report['checks'].append(component)
            component = 'tokenizers native extension'
            import_module('tokenizers.tokenizers')
            report['checks'].append(component)
            report['semantic'] = {'status': 'PASS', 'telemetry': 'disabled', 'inference': 'NOT_RUN'}
        if manifest['bundled_ocr']:
            report['ocr']['status'] = 'FAIL'
            component = 'bundled OCR engine'
            previous = os.environ.get('CARR_OCR_DIR')
            os.environ['CARR_OCR_DIR'] = str(root / 'ocr')
            try:
                diagnostic = import_module('carr_library.extract.ocr_runtime')
                result = diagnostic.check_runtime(timeout=30)
            finally:
                if previous is None:
                    os.environ.pop('CARR_OCR_DIR', None)
                else:
                    os.environ['CARR_OCR_DIR'] = previous
            if type(result) is not dict or result.get('status') != 'PASS':
                raise RuntimeError('The canonical OCR installation check did not pass.')
            report['ocr'] = result
            report['checks'].append(component)
        component = 'bundled audio runtime'
        report['audio'] = {'status': 'FAIL'}
        report['audio'] = _checked_bundled_audio(root)
        if report['audio']['status'] == 'PASS':
            report['checks'].append(component)
        # Old sealed Node files remain verifiable installation data. Citation
        # formatting itself uses the one packaged QuickJS runtime.
        component = 'legacy citation runtime inventory'
        from .bundled_engines import verify_optional_runtime
        legacy_citations = verify_optional_runtime(root, manifest, 'citation-runtime', execute=False)
        report['citations'] = {'status': 'PASS', 'runtime': 'QuickJS',
            'native_extension': 'PASS', 'formatting': 'NOT_RUN',
            'legacy_inventory': legacy_citations}
        if legacy_citations['status'] == 'PASS':
            report['checks'].append(component)
        report['status'] = 'PASS'
    except Exception as exc:
        report['failed_component'] = component
        report['error'] = f'{component}: {type(exc).__name__}: {exc}'
    finally:
        if _runtime_handle is not None:
            _runtime_handle.close()
    if sys.platform == 'win32':
        report['windows_execution'] = report['status']
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-root', required=True, type=Path)
    arguments = parser.parse_args(argv)
    report = check_native_dependencies(arguments.bundle_root)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
