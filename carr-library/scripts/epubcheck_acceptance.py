"""Check actual current Studio publications with pinned external EPUBCheck.

Reuses the PR152 validator invocation and negative control. No downloads or
Java installation occur here. All fixture audio is public synthetic PCM.
"""
import argparse
import json
import importlib.util
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

_spec = importlib.util.spec_from_file_location('current_publication_acceptance_support',
    Path(__file__).with_name('publication_acceptance_support.py'))
_support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_support)
digest, produce, runtime = _support.digest, _support.produce, _support.runtime


def validate(java, jar, publication, report, *, expected_valid=True):
    identity = digest(publication)
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {'JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'CLASSPATH'}}
    run = subprocess.run([str(java), '-jar', str(jar), '--failonwarnings', '--json',
                          str(report), str(publication)], stdin=subprocess.DEVNULL, capture_output=True,
                         encoding='utf-8', errors='replace', timeout=180, env=env)
    if not report.is_file():
        raise RuntimeError('EPUBCheck did not publish its actual report: ' + run.stderr[-4000:])
    result = json.loads(report.read_text(encoding='utf-8'))
    checker = result['checker']
    if checker['checkerVersion'] != '5.3.0' or digest(publication) != identity:
        raise RuntimeError('EPUBCheck version or publication byte identity changed.')
    counts = {key: checker[key] for key in ('nFatal', 'nError', 'nWarning', 'nUsage')}
    if expected_valid:
        if run.returncode or any(counts[key] for key in ('nFatal', 'nError', 'nWarning')):
            raise RuntimeError('Actual publication failed EPUBCheck: '
                               + json.dumps(result.get('messages', []), ensure_ascii=False)
                               + '\n' + run.stdout[-4000:] + run.stderr[-4000:])
    elif run.returncode == 0 or not counts['nFatal'] + counts['nError']:
        raise RuntimeError('The deliberately invalid EPUB was not rejected.')
    return {'file': publication.name, 'sha256': identity, 'checker_version': checker['checkerVersion'],
            'exit_code': run.returncode, 'counts': counts, 'expected_valid': expected_valid,
            'messages': result.get('messages', [])}


def inspect(archive, java, lock_path, *, require_installed=False):
    from carr_library.native_runtime import activate_bundled_runtime
    from carr_library.audio import publishing
    from carr_library.audio.publication_io import stable_reader
    runtime_handle = activate_bundled_runtime()
    try:
        ffmpeg, runtime_evidence, unchanged = runtime(require_installed=require_installed)
        pin = json.loads(lock_path.read_text(encoding='utf-8'))
        if (pin['version'] != '5.3.0' or archive.stat().st_size != pin['size']
                or digest(archive) != pin['sha256']):
            raise RuntimeError('Supply the exact hash-verified EPUBCheck archive.')
        results = []
        with TemporaryDirectory(prefix='carr-epubcheck-') as temporary:
            root = Path(temporary).resolve(strict=True)
            tools = root / 'validator'
            tools.mkdir()
            with stable_reader(archive) as stream, ZipFile(stream) as zipped:
                entries = zipped.infolist()
                if len(entries) > 4096 or len({entry.filename for entry in entries}) != len(entries):
                    raise RuntimeError('Unexpected validator archive inventory.')
                if sum(entry.file_size for entry in entries) > 256 * 1024**2:
                    raise RuntimeError('Validator extraction exceeds its fixed byte allowance.')
                for entry in entries:
                    if (not (tools / entry.filename).resolve().is_relative_to(tools)
                            or '\\' in entry.filename or entry.flag_bits & 1
                            or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                        raise RuntimeError('Invalid validator archive path or type.')
                zipped.extractall(tools)
            if digest(archive) != pin['sha256']:
                raise RuntimeError('The validator archive changed during extraction.')
            for notice in pin['notices']:
                if not (tools / notice).is_file():
                    raise RuntimeError('Pinned validator license notices are missing.')
            jar = tools / pin['jar']
            productions = produce(root / 'productions', 'epub', ffmpeg)
            for record in productions:
                publication = record['path']
                result = validate(java, jar, publication, publication.with_suffix('.epubcheck.json'))
                if result['sha256'] != record['receipt']['artifact']['sha256']:
                    raise AssertionError('EPUBCheck did not bind the saved receipt bytes.')
                result['case'] = record['case']
                results.append(result)
            invalid = root / 'deliberately-invalid.epub'
            with ZipFile(productions[-1]['path']) as source, ZipFile(invalid, 'x') as target:
                for entry in source.infolist():
                    target.writestr(entry, b'application/not-epub' if entry.filename == 'mimetype'
                                    else source.read(entry.filename))
            rejected = validate(java, jar, invalid, root / 'invalid.json', expected_valid=False)
            unchanged()
        return {'schema': 1, 'status': 'PASS', 'epub_specification': '3.3',
            'validator_version': pin['version'], 'validator_archive_sha256': pin['sha256'],
            **runtime_evidence, 'publications': results, 'deliberately_invalid_rejected': rejected,
            'audio_fixture': 'Public synthetic PCM through actual current Audio Studio and FFmpeg',
            'reader_interoperability': 'NOT_RUN', 'speech_quality': 'NOT_RUN',
            'native_menu': 'NOT_RUN', 'nvda': 'NOT_RUN', 'wcag_conformance': 'NOT_CLAIMED'}
    finally:
        if runtime_handle is not None:
            runtime_handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--java', required=True, type=Path)
    parser.add_argument('--lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'epubcheck.json')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true')
    args = parser.parse_args()
    result = inspect(args.archive.resolve(strict=True), args.java.resolve(strict=True),
                     args.lock.resolve(strict=True), require_installed=args.require_installed)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
