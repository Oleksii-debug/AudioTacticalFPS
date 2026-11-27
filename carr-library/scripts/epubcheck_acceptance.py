"""Independent EPUBCheck acceptance for this application's one audio renderer.

Requires an explicitly supplied pinned validator archive and Java. Downloads,
Java installation and production dependencies are outside this script.
All narrated fixtures use synthetic PCM tones, never a claimed installed voice.
"""
import argparse
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import wave
from zipfile import ZipFile


MAX_REPORT = 8 * 1024 ** 2


def digest(path):
    from carr_library.audio.jobs import sha256_file
    return sha256_file(path)


class SyntheticTones:
    def cache_identity(self, voice, **kwargs):
        return {'fixture': 'publication-conformance-tones-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        if kwargs.get('cancelled', lambda: False)():
            raise InterruptedError('Synthetic acceptance cancelled.')
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 24000, 4800, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(
                2 * math.pi * 440 * index / 24000))) for index in range(4800)))
        return output


def _read_json(path, maximum=MAX_REPORT):
    from carr_library.audio.jobs import read_json
    return read_json(path, limit=maximum)


def _write_report(path, result):
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if len(data) > MAX_REPORT:
        raise ValueError('Acceptance report exceeds its bounded size.')
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _application(*, require_installed=False, bundle_root=None):
    from carr_library.audio import clips, daisy, publishing, production
    from carr_library.audio.runtime import resolve_ffmpeg, verify_runtime
    modules = (clips, daisy, publishing, production)
    if require_installed:
        prefix = Path(sys.prefix).resolve(strict=True)
        if any('site-packages' not in {part.casefold() for part in Path(module.__file__).resolve().parts}
               or not Path(module.__file__).resolve().is_relative_to(prefix) for module in modules):
            raise RuntimeError('Acceptance must import the installed application from this interpreter prefix.')
    binary = resolve_ffmpeg()
    if binary is None or not binary.is_file():
        raise RuntimeError('The current reviewed local FFmpeg runtime is required.')
    proof = {'sha256': digest(binary), 'path': str(binary)}
    if bundle_root is not None:
        bundle = Path(bundle_root).resolve(strict=True)
        if require_installed and any(not Path(module.__file__).resolve().is_relative_to(bundle / '.venv')
                                     for module in modules):
            raise RuntimeError('An application module escaped the selected installed bundle.')
        verify_runtime(bundle / 'audio-runtime')
        if not binary.samefile(bundle / 'audio-runtime' / 'ffmpeg.exe'):
            raise RuntimeError('The audio runtime escaped the verified selected bundle.')
        proof['verified_bundle_runtime'] = True
    return binary, {module.__name__: digest(module.__file__) for module in modules}, proof


def acceptance_cases(root, book_format, *, ffmpeg):
    """Actual current renders and exports, shared by both independent checkers."""
    from carr_library.audio import publishing
    from carr_library.audio.daisy import inspect_daisy
    from carr_library.audio.clips import ClipEditor
    from carr_library.audio.production import parse_script, render_project
    library = root / 'library'
    library.mkdir()
    engine = SyntheticTones()
    script = ('[output sample_rate=24000 stems=false]\r\n# Архів & letters\r\n'
              'Narrator: Exact <source> & preserved words.\r\n[pause 0.2]\r\n'
              'Reader: Другий рядок «цитати».\r\n# Closing\r\nNarrator: The final line.\r\n')
    for name, output_format in (('narrated-from-wav', 'wav'), ('narrated-from-mp3-render', 'mp3'),
                                ('untranscribed-retained-clip', 'wav')):
        project = root / name
        project.mkdir()
        if name == 'untranscribed-retained-clip':
            selected = root / 'selected.wav'
            engine.synthesize('', selected)
            editor = ClipEditor('', project)
            edited = editor.apply({'action': 'add', 'scene_id': 'scene-000001', 'source': selected,
                                   'values': {'track': 'music', 'start': .1, 'duration': .2}})
            selected.unlink()
            plan = parse_script('[output sample_rate=24000 stems=false]\n' + edited['script'])
        else:
            plan = replace(parse_script(script, voice='fixture:tones'), output_format=output_format)
        result = render_project(plan, project, engine=engine, ffmpeg=ffmpeg)
        if result['status'] != 'complete':
            raise AssertionError('The current typed audio fixture failed to render.')
        service = publishing.PublicationService(project, private_root=library, ffmpeg=ffmpeg,
                                                 max_bytes=256 * 1024 ** 2)
        output = root / (name + ('.epub' if book_format == 'epub' else '.zip'))
        receipt = (service.export_epub(result['run_key'], output, language='uk', creator='Synthetic fixture')
                   if book_format == 'epub' else service.export_daisy(result['run_key'], output,
                       language='uk', publisher='Synthetic fixture', creator='Synthetic fixture'))
        provenance = (publishing.inspect_epub if book_format == 'epub' else inspect_daisy)(output)
        if provenance['original_script'] != plan.original_script:
            raise AssertionError('The publication changed its exact source script.')
        if name == 'untranscribed-retained-clip' and any(
                row['kind'] != 'audio-interval' for row in provenance['intervals']):
            raise AssertionError('The retained clip acquired an invented transcript.')
        if receipt['artifact']['sha256'] != digest(output):
            raise AssertionError('The export receipt does not bind the published bytes.')
        yield name, output, receipt, provenance


def unpack_validator(archive, tools, pin):
    from carr_library.audio.jobs import regular_file
    if (pin.get('version') != '5.3.0'
            or pin.get('sha256') != '6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5'
            or pin.get('size') != 33071108 or pin.get('jar') != 'epubcheck-5.3.0/epubcheck.jar'
            or pin.get('notices') != ['epubcheck-5.3.0/LICENSE.txt', 'epubcheck-5.3.0/THIRD-PARTY.txt']
            or regular_file(archive).st_size != pin['size'] or digest(archive) != pin['sha256']):
        raise ValueError('Supply the exact reviewed EPUBCheck 5.3.0 archive and pin.')
    seen, total = set(), 0
    with ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 5000:
            raise ValueError('Validator archive exceeds the entry budget.')
        for entry in entries:
            name = entry.filename
            parts = PurePosixPath(name).parts
            mode = entry.external_attr >> 16
            if (not parts or parts[0] != 'epubcheck-5.3.0' or name.startswith('/')
                    or '\\' in name or ':' in name or any(part in ('.', '..') for part in parts)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or name.casefold() in seen or entry.flag_bits & 1):
                raise ValueError('Unexpected validator archive path or file type.')
            seen.add(name.casefold())
            total += entry.file_size
            if total > 256 * 1024 ** 2:
                raise ValueError('Validator archive exceeds the extracted byte budget.')
            target = tools.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry) as incoming, target.open('xb') as output:
                remaining = entry.file_size
                while remaining:
                    block = incoming.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('Truncated validator archive entry.')
                    output.write(block)
                    remaining -= len(block)
                if incoming.read(1):
                    raise ValueError('Unexpected validator archive entry growth.')
    if digest(archive) != pin['sha256']:
        raise ValueError('The validator archive changed while extracting.')
    for name in pin['notices']:
        regular_file(tools / name)
    jar = tools / pin['jar']
    regular_file(jar)
    return jar


def validate(java, jar, publication, report, *, expected_valid=True):
    identity = digest(publication)
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {'JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'CLASSPATH'}}
    with TemporaryDirectory(prefix='epubcheck-logs-') as temporary:
        log = Path(temporary)
        with (log / 'stdout').open('wb') as out, (log / 'stderr').open('wb') as err:
            run = subprocess.run([str(java), '-Xmx512m', '-jar', str(jar), '--failonwarnings',
                                  '--json', str(report), str(publication)],
                                 stdout=out, stderr=err, timeout=180, env=env, shell=False)
        if not report.is_file():
            with (log / 'stderr').open('rb') as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - 4000))
                tail = stream.read(4000).decode('utf-8', errors='replace')
            raise RuntimeError('EPUBCheck did not publish its actual report: ' + tail)
        result = _read_json(report)
    checker = result['checker']
    if checker['checkerVersion'] != '5.3.0' or digest(publication) != identity:
        raise RuntimeError('The actual EPUBCheck version or publication identity changed.')
    counts = {key: checker[key] for key in ('nFatal', 'nError', 'nWarning', 'nUsage')}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError('The checker returned malformed diagnostic counts.')
    if expected_valid:
        if run.returncode or any(counts[key] for key in ('nFatal', 'nError', 'nWarning')):
            raise RuntimeError('Actual publication failed EPUBCheck: '
                               + json.dumps(result.get('messages', []), ensure_ascii=False)[:16000])
    elif run.returncode == 0 or not counts['nFatal'] + counts['nError']:
        raise RuntimeError('EPUBCheck did not reject the deliberately invalid EPUB.')
    return {'file': publication.name, 'sha256': identity, 'checker_version': checker['checkerVersion'],
            'exit_code': run.returncode, 'counts': counts, 'expected_valid': expected_valid,
            'messages': result.get('messages', [])}


def inspect(archive, java, lock_path, *, require_installed=False, bundle_root=None):
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime(bundle_root)
    try:
        binary, modules, audio = _application(require_installed=require_installed, bundle_root=bundle_root)
        pin = _read_json(lock_path, 32768)
        with TemporaryDirectory(prefix='resonance-epubcheck-') as temporary:
            root = Path(temporary).resolve()
            tools = root / 'validator'
            tools.mkdir()
            jar = unpack_validator(archive, tools, pin)
            results = []
            for name, publication, receipt, provenance in acceptance_cases(root, 'epub', ffmpeg=binary):
                result = validate(java, jar, publication, root / (name + '.json'))
                if result['sha256'] != receipt['artifact']['sha256']:
                    raise AssertionError('Independent conformance did not bind the export receipt.')
                result['case'] = name
                results.append(result)
            invalid = root / 'deliberately-invalid.epub'
            with ZipFile(publication) as source, ZipFile(invalid, 'x') as target:
                for entry in source.infolist():
                    target.writestr(entry, b'application/not-epub' if entry.filename == 'mimetype'
                                    else source.read(entry.filename))
            rejected = validate(java, jar, invalid, root / 'invalid.json', expected_valid=False)
        return {'schema': 1, 'status': 'PASS', 'epub_specification': '3.3',
                'validator_version': pin['version'], 'validator_archive_sha256': pin['sha256'],
                'application_module_sha256': modules, 'audio_runtime': audio,
                'publications': results, 'deliberately_invalid_rejected': rejected,
                'audio_fixture': 'Synthetic PCM tones through actual current production and FFmpeg',
                'reader_interoperability': 'NOT_RUN', 'nvda': 'NOT_RUN', 'wcag_conformance': 'NOT_CLAIMED'}
    finally:
        if handle is not None:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--java', required=True, type=Path)
    parser.add_argument('--lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'epubcheck.json')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--bundle-root', type=Path)
    args = parser.parse_args()
    result = inspect(args.archive.resolve(strict=True), args.java.resolve(strict=True),
                     args.lock.resolve(strict=True), require_installed=args.require_installed,
                     bundle_root=args.bundle_root.resolve(strict=True) if args.bundle_root else None)
    _write_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
