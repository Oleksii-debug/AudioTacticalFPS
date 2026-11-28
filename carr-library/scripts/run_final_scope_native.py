"""Run the final scope's existing native probes against one exact installed head.

This is acceptance tooling. It never implements a product flow or substitutes a
native probe. Each probe retains its own scenario assertions and cleanup.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


ASSETS = (
    "reader/_vendor_num2words/COPYING",
    "reader/_vendor_num2words/UPSTREAM.json",
    "academic_integration/UPSTREAM.json",
    "academic_integration/csl_worker.cjs",
    "academic_integration/locales/locales-en-US.xml",
    "academic_integration/locales/locales-sk-SK.xml",
    "academic_integration/locales/locales-uk-UA.xml",
    "academic_integration/styles/apa.csl",
    "academic_integration/styles/chicago-author-date.csl",
    "academic_integration/styles/chicago-fullnote-bibliography.csl",
    "academic_integration/styles/harvard-cite-them-right.csl",
    "academic_integration/styles/ieee.csl",
    "academic_integration/styles/modern-language-association.csl",
    "academic_integration/styles/nlm-citation-sequence.csl",
    "academic_integration/vendor/LICENSE",
    "academic_integration/vendor/LICENSE-AGPL-3.0.txt",
    "academic_integration/vendor/LICENSE-CC-BY-SA-3.0.txt",
    "academic_integration/vendor/LICENSE-CPAL-1.0.txt",
    "academic_integration/vendor/NOTICE.txt",
    "academic_integration/vendor/SORT-MACRO-CACHE-NOTICE.txt",
    "academic_integration/vendor/citeproc-sort-macro-cache.json",
    "academic_integration/vendor/citeproc.js",
)


def digest(raw):
    return sha256(raw).hexdigest()


def regular_bytes(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise AssertionError('Acceptance input must remain a regular file: ' + str(path))
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise AssertionError('Acceptance input changed while reading: ' + str(path))
    return raw


def git_value(repository, *args):
    return subprocess.check_output(['git', *args], cwd=repository, timeout=30)


def package_inventory(directory):
    files = {}
    for path in sorted(directory.rglob('*.py')):
        files[path.relative_to(directory).as_posix()] = digest(regular_bytes(path))
    for name in ASSETS:
        files[name] = digest(regular_bytes(directory / name))
    if not files or '__init__.py' not in files:
        raise AssertionError('The complete installed Python inventory is empty.')
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--bundle-root', type=Path)
    parser.add_argument('--audio-evidence', type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or not re.fullmatch('[0-9a-f]{40}', args.expected_head):
        raise AssertionError('The native gate requires Windows and an exact Git commit.')
    project = Path(__file__).resolve().parents[1]
    repository = project.parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    audio_evidence = args.audio_evidence.resolve() if args.audio_evidence else None
    audio_before = digest(regular_bytes(audio_evidence)) if audio_evidence else None
    config_path = project / 'scripts/final_scope_native.json'
    config_raw = regular_bytes(config_path)
    config = json.loads(config_raw)
    if set(config) != {'schema', 'probes'} or config['schema'] != 'carr-final-scope-native/1':
        raise AssertionError('Unknown final native acceptance configuration.')
    if not 1 <= len(config['probes']) <= 12:
        raise AssertionError('The final native probe selection is empty or unbounded.')
    identifiers = [entry['id'] for entry in config['probes']]
    if len(set(identifiers)) != len(identifiers):
        raise AssertionError('A native acceptance probe is selected more than once.')

    origin = Path(importlib.util.find_spec('carr_library').origin).resolve()
    installed = origin.parent
    source = project / 'src/carr_library'
    if origin.is_relative_to(repository):
        raise AssertionError('Native acceptance must import the installed package.')
    committed = {}
    for entry in git_value(repository, 'ls-tree', '-r', '-z', '--full-tree',
                           args.expected_head, 'carr-library').split(b'\0'):
        if not entry:
            continue
        header, name = entry.split(b'\t', 1)
        mode, kind, value = header.decode('ascii').split()
        path = name.decode('utf-8')
        relative = path.removeprefix('carr-library/')
        if (relative.endswith('.py') or relative.startswith('src/carr_library/')
                or relative == 'scripts/final_scope_native.json'
                or relative.endswith('_native_oracle.json')):
            if mode not in ('100644', '100755') or kind != 'blob':
                raise AssertionError('Selected native source is not a regular committed blob.')
            committed[relative] = value
    if 'scripts/run_final_scope_native.py' not in committed:
        raise AssertionError('The executed acceptance helper is not committed at this head.')

    def source_identity():
        actual_head = git_value(repository, 'rev-parse', 'HEAD').decode('ascii').strip()
        if actual_head != args.expected_head:
            raise AssertionError('The checkout moved away from the exact event head.')
        result = {}
        for name, expected in sorted(committed.items()):
            raw = regular_bytes(project / name)
            actual = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw,
                          usedforsecurity=False).hexdigest()
            if actual != expected:
                raise AssertionError('Source differs from its committed Git blob: ' + name)
            result[name] = digest(raw)
        return result

    sources_before = source_identity()
    if sources_before['scripts/final_scope_native.json'] != digest(config_raw):
        raise AssertionError('The parsed native configuration differs from its committed bytes.')
    expected_package = {name.removeprefix('src/carr_library/'): value
                        for name, value in sources_before.items()
                        if name.startswith('src/carr_library/') and (
                            name.endswith('.py') or name.removeprefix('src/carr_library/') in ASSETS)}
    if package_inventory(source) != expected_package:
        raise AssertionError('The source package has missing, extra or changed Python/notice inputs.')
    if package_inventory(installed) != expected_package:
        raise AssertionError('Installed Python or runtime and notice bytes differ from the exact source.')
    if args.bundle_root:
        bundle = args.bundle_root.resolve()
        if not installed.is_relative_to(bundle):
            raise AssertionError('The installed package is outside the tested bundle.')

    report = dict(schema='carr-final-scope-native-result/1', source_commit=args.expected_head,
                  status='RUNNING', python=sys.executable, installed_package=str(installed),
                  source_files=len(sources_before), package_data_files=len(ASSETS), package_python_files=sum(
                      name.endswith('.py') for name in expected_package),
                  package_inventory_sha256=digest(json.dumps(
                      expected_package, sort_keys=True, separators=(',', ':')).encode()),
                  selected_source_sha256=sources_before, selected_audio_evidence_sha256=audio_before,
                  probes=[], NVDA='NOT_RUN')
    (output / 'before.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    wrapper = ("import runpy,sys; from carr_library.native_runtime import activate_bundled_runtime; "
               "handle=activate_bundled_runtime(); path=sys.argv[1]; sys.argv=sys.argv[1:]; "
               "runpy.run_path(path,run_name='__main__')")
    try:
        for spec in config['probes']:
            allowed = {'id', 'path', 'args', 'required', 'count_field', 'oracle',
                       'optional_office', 'timeout_seconds', 'owned_deadline_seconds'}
            if set(spec) - allowed or not {
                    'id', 'path', 'args', 'required', 'timeout_seconds', 'owned_deadline_seconds'} <= set(spec):
                raise AssertionError('Unexpected native probe configuration fields.')
            if not re.fullmatch('[a-z][a-z0-9-]{0,48}', spec['id']):
                raise AssertionError('Invalid native acceptance identifier.')
            if (spec['path'] not in committed or not spec['path'].endswith('.py')
                    or not spec['path'].startswith(('scripts/', 'tests/ui/'))):
                raise AssertionError('Native probe must be a selected committed acceptance script.')
            if type(spec['args']) is not list or any(type(value) is not str for value in spec['args']):
                raise AssertionError('Native probe arguments must be an explicit string list.')
            outer, owned = spec['timeout_seconds'], spec['owned_deadline_seconds']
            if (type(outer) is not int or type(owned) is not int
                    or not 1 <= owned < outer <= 900 or outer - owned < 60):
                raise AssertionError('The finite outer allowance must exceed the owned probe deadline by 60 seconds.')
            if '@audio-evidence' in spec['args'] and audio_evidence is None:
                raise AssertionError('This native probe requires the existing checked audio evidence.')
            arguments = [str(audio_evidence) if value == '@audio-evidence' else value
                         for value in spec['args']]
            command = [sys.executable, '-I', '-B', '-X', 'utf8', '-c', wrapper,
                       str(project / spec['path']), *arguments]
            probe = dict(id=spec['id'], source_sha256=sources_before[spec['path']],
                         timeout_seconds=outer, owned_deadline_seconds=owned)
            failure = None
            def retain(channel, text):
                if isinstance(text, bytes):
                    text = text.decode('utf-8', errors='replace')
                (output / (spec['id'] + '.' + channel + '.txt')).write_text(text or '', encoding='utf-8')
            try:
                try:
                    completed = subprocess.run(command, cwd=project, capture_output=True, timeout=outer,
                                               encoding='utf-8', errors='replace', check=False)
                except subprocess.TimeoutExpired as exc:
                    retain('stdout', exc.stdout)
                    retain('stderr', exc.stderr)
                    probe['ownership_after_outer_timeout'] = 'NOT_CONFIRMED; gate failed'
                    raise
                retain('stdout', completed.stdout)
                retain('stderr', completed.stderr)
                probe['returncode'] = completed.returncode
                if completed.returncode:
                    raise AssertionError('Native probe failed: ' + spec['id'] + '; see retained stdout/stderr.')
                records = []
                for line in completed.stdout.splitlines():
                    if line.startswith('{'):
                        try:
                            value = json.loads(line)
                        except ValueError:
                            continue
                        if type(value) is dict:
                            records.append(value)
                if not records:
                    raise AssertionError('Native probe emitted no final JSON: ' + spec['id'])
                actual = records[-1]
                probe['result'] = actual
                optional = spec.get('optional_office', False)
                if type(optional) is not bool:
                    raise AssertionError('Office qualification flag must be explicit.')
                if optional and actual.get('status') == 'NOT_RUN':
                    if (actual.get('schema') != 'carr-native-word-citations/2'
                            or actual.get('actual_office') != 'NOT_RUN'
                            or type(actual.get('checks')) is not int or actual['checks'] != 0
                            or actual.get('check_names') != []
                            or type(actual.get('reason')) is not str or not actual['reason'].strip()
                            or actual.get('native_keyboard') != 'NOT_RUN'
                            or actual.get('actual_nvda') != 'NOT_RUN'):
                        raise AssertionError('Only explicit unavailable Office with zero checks may be NOT_RUN.')
                else:
                    for name, value in spec['required'].items():
                        if actual.get(name) != value or type(actual.get(name)) is not type(value):
                            raise AssertionError('Missing native acceptance outcome: ' + spec['id'] + '.' + name)
                    count = actual.get(spec.get('count_field', 'checks'))
                    if isinstance(count, list):
                        count = len(count)
                    if type(count) is not int or count <= 0:
                        raise AssertionError('Native acceptance cannot be an empty or skipped scenario.')
                    if 'oracle' in spec:
                        oracle_path = spec['oracle']
                        if oracle_path not in committed:
                            raise AssertionError('The exact native oracle is not committed.')
                        oracle = json.loads(regular_bytes(project / oracle_path))
                        if Counter(actual['checks']) != Counter(oracle['checks']):
                            raise AssertionError('Actual native checks differ from the complete fixed oracle.')
            except BaseException as exc:
                failure = exc
                probe['failure_type'] = type(exc).__name__
                probe['failure_message'] = str(exc)[:1200]
            finally:
                try:
                    if source_identity() != sources_before:
                        raise AssertionError('A native probe changed committed acceptance inputs.')
                    if package_inventory(source) != expected_package:
                        raise AssertionError('A native probe added or changed source Python or runtime and notice bytes.')
                    if package_inventory(installed) != expected_package:
                        raise AssertionError('A native probe changed installed Python or runtime and notice bytes.')
                    if audio_evidence and digest(regular_bytes(audio_evidence)) != audio_before:
                        raise AssertionError('The selected existing audio evidence changed during acceptance.')
                    probe['exact_identity_after'] = 'PASS'
                except BaseException as exc:
                    probe['exact_identity_after'] = 'FAIL'
                    probe['identity_failure_type'] = type(exc).__name__
                    probe['identity_failure_message'] = str(exc)[:1200]
                report['probes'].append(probe)
            if failure is not None:
                raise failure
            if probe['exact_identity_after'] != 'PASS':
                raise AssertionError('Native input identity failed; see the separate retained identity record.')
        report['status'] = 'PASS_REQUIRED_NATIVE'
        report['qualification'] = 'Exact-head installed native scenarios only; optional Office and human evidence stay explicit.'
    except BaseException as exc:
        report['status'] = 'FAIL'
        report['failure_type'] = type(exc).__name__
        (output / 'result.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        raise
    (output / 'result.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print('CARR_FINAL_SCOPE_NATIVE ' + json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
