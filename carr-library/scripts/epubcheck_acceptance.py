"""Check actual Studio publications with an explicitly supplied pinned EPUBCheck.

No downloads or Java installation. The conformance checker is a CI tool, not a
production dependency. All fixture voices are synthetic tones, not model output.
"""
import argparse
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import wave
from zipfile import ZipFile


def digest(path):
    value = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class SyntheticTones:
    def voices(self):
        return (SimpleNamespace(id='synthetic-tone', name='Synthetic conformance tone',
                                language='und', engine='test fixture'),)

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        if cancelled():
            raise InterruptedError('Synthetic acceptance cancelled.')
        assert voice_id == 'synthetic-tone'
        with wave.open(str(destination), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'synthetic tone'))
            stream.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(
                2 * math.pi * 440 * index / 24000))) for index in range(4800)))


def validate(java, jar, publication, report, *, expected_valid=True):
    identity = digest(publication)
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {'JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'CLASSPATH'}}
    run = subprocess.run([str(java), '-jar', str(jar), '--failonwarnings', '--json',
                          str(report), str(publication)], capture_output=True,
                         encoding='utf-8', errors='replace', timeout=180, env=env)
    if not report.is_file():
        raise RuntimeError('EPUBCheck did not publish its actual report: ' + run.stderr[-4000:])
    result = json.loads(report.read_text(encoding='utf-8'))
    checker = result['checker']
    if checker['checkerVersion'] != '5.3.0' or digest(publication) != identity:
        raise RuntimeError('EPUBCheck version or publication byte identity changed.')
    counts = {key: checker[key] for key in ('nFatal', 'nError', 'nWarning', 'nUsage')}
    if expected_valid:
        if run.returncode or any(counts.values()):
            raise RuntimeError('Actual publication failed EPUBCheck: '
                               + json.dumps(result.get('messages', []), ensure_ascii=False)
                               + '\n' + run.stdout[-4000:] + run.stderr[-4000:])
    elif (run.returncode == 0 or not counts['nFatal'] + counts['nError']
          or not any(message.get('ID') == 'PKG-007' for message in result.get('messages', []))):
        raise RuntimeError('The deliberately invalid EPUB mimetype was not rejected with PKG-007.')
    return {'file': publication.name, 'sha256': identity, 'checker_version': checker['checkerVersion'],
            'exit_code': run.returncode, 'counts': counts, 'expected_valid': expected_valid,
            'messages': result.get('messages', [])}


def inspect(archive, java, lock_path, *, require_installed=False,
            audio_directory=None, audio_lock_path=None):
    from carr_library.native_runtime import activate_bundled_runtime
    runtime_handle = activate_bundled_runtime()
    from carr_library.studio import publishing
    from carr_library.studio.project import Cue, parse_script
    from carr_library.studio.render import ProductionService

    if require_installed and 'site-packages' not in {
            part.casefold() for part in Path(publishing.__file__).resolve().parts}:
        raise RuntimeError('Conformance acceptance must use the installed application.')
    pin = json.loads(lock_path.read_text(encoding='utf-8'))
    if (pin['version'] != '5.3.0' or archive.stat().st_size != pin['size']
            or digest(archive) != pin['sha256']):
        raise RuntimeError('Supply the exact hash-verified EPUBCheck archive.')
    binaries = {}
    for name in ('ffmpeg', 'ffprobe'):
        executable = shutil.which(name)
        if executable is None:
            raise RuntimeError('The reviewed audio executable is missing: ' + name)
        binaries[name] = Path(executable).resolve(strict=True)
    bundle = os.environ.get('CARR_PROGRAM_DIR')
    audio_hashes = {name: digest(path) for name, path in binaries.items()}
    audio_binding = 'observed source-test runtime'
    if audio_directory is not None:
        audio_root = Path(audio_directory).resolve(strict=True)
        audio_pin = json.loads(Path(audio_lock_path).read_text(encoding='utf-8'))
        for name, executable in binaries.items():
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            if (not executable.samefile(audio_root / 'bin' / (name + '.exe'))
                    or executable.stat().st_size != expected['size']
                    or audio_hashes[name] != expected['sha256']):
                raise RuntimeError('Conformance audio runtime differs from the selected pinned directory: ' + name)
        audio_binding = 'explicit pinned audio directory'
    manifest = None
    if bundle:
        bundle_root = Path(bundle).resolve(strict=True)
        if require_installed and not Path(publishing.__file__).resolve().is_relative_to(bundle_root / '.venv'):
            raise RuntimeError('The publisher is outside the selected installed bundle.')
        manifest = json.loads((bundle_root / 'bundle-manifest.json').read_text(encoding='utf-8'))
        if manifest.get('bundled_audio'):
            for name, executable in binaries.items():
                relative = 'audio/bin/' + name + '.exe'
                if (not executable.samefile(bundle_root / relative)
                        or audio_hashes[name] != manifest['files'].get(relative)):
                    raise RuntimeError('Conformance audio runtime escaped the installed bundle: ' + name)
            audio_binding = 'sealed bundled audio runtime'
    if require_installed and audio_directory is None and not (manifest and manifest.get('bundled_audio')):
        raise RuntimeError('Installed conformance requires a selected pinned audio directory or sealed bundled audio.')
    results = []
    with TemporaryDirectory(prefix='resonance-epubcheck-') as temporary:
        root = Path(temporary).resolve()
        tools = root / 'validator'
        tools.mkdir()
        with ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                if (not (tools / entry.filename).resolve().is_relative_to(tools)
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise RuntimeError('Invalid validator archive path.')
            zipped.extractall(tools)
        for notice in pin['notices']:
            if not (tools / notice).is_file():
                raise RuntimeError('Pinned validator license notices are missing.')
        jar = tools / pin['jar']
        library = root / 'synthetic library'
        service = ProductionService(library / 'state' / 'studio', voices=SyntheticTones(),
                                    input_roots=(root,))
        publisher = publishing.PublicationService(service)
        script = ('[Scene: Архів & letters]\r\nNarrator: Exact <original> & preserved words.\r\n'
                  '[Pause: 0.2]\r\nReader: Другий рядок «цитати».\r\n'
                  '[Scene: Closing]\r\nNarrator: The final line.\r\n')
        project = service.create(script, title='Synthetic synchronized publication',
                                 voice_ids=('synthetic-tone',))
        for name, output_format in (('narrated-from-wav', 'wav'), ('reused-job-mp3', 'mp3')):
            output = root / name
            output.mkdir()
            identity = service.submit(project, output, format=output_format, stems=False)
            assert service.run(identity)['status'] == 'complete'
            publication = root / (name + '.epub')
            receipt = publisher.export_epub(identity, publication, language='uk', creator='Synthetic fixture')
            provenance = publishing.inspect_epub(publication)
            assert provenance['original_script'] == script
            assert provenance['audio']['reused'] == (output_format == 'mp3')
            result = validate(java, jar, publication, root / (name + '.json'))
            assert result['sha256'] == receipt['artifact']['sha256']
            results.append(result)
        clip = root / 'synthetic-clip.wav'
        SyntheticTones().synthesize('', 'synthetic-tone', clip, settings=None, cancelled=lambda: False)
        audio_project = parse_script('[Scene: Untranscribed audio]\n[Duration: 0.3]',
                                     title='Untranscribed local audio')
        audio_project = replace(audio_project, original_script='', scenes=(replace(
            audio_project.scenes[0], cues=(Cue('synthetic-source', str(clip), start=.1, role='music'),)),))
        output = root / 'audio-only'
        output.mkdir()
        identity = service.submit(audio_project, output, stems=False)
        assert service.run(identity)['status'] == 'complete'
        publication = root / 'audio-only.epub'
        publisher.export_epub(identity, publication)
        assert all(row['kind'] == 'audio-interval'
                   for row in publishing.inspect_epub(publication)['intervals'])
        results.append(validate(java, jar, publication, root / 'audio-only.json'))
        invalid = root / 'deliberately-invalid.epub'
        with ZipFile(publication) as source, ZipFile(invalid, 'x') as target:
            for entry in source.infolist():
                target.writestr(entry, b'application/not-epub' if entry.filename == 'mimetype'
                                else source.read(entry.filename))
        rejected = validate(java, jar, invalid, root / 'invalid.json', expected_valid=False)
        if hasattr(service, 'close'):
            service.close()
        # Keep the DLL directory handle alive until all render/validation work ends.
        if runtime_handle is not None:
            runtime_handle.close()
    return {'schema': 1, 'status': 'PASS', 'epub_specification': '3.3',
            'validator_version': pin['version'], 'validator_archive_sha256': pin['sha256'],
            'publisher_module_sha256': digest(publishing.__file__),
            'audio_binaries': {name: str(path) for name, path in binaries.items()},
            'audio_sha256': audio_hashes,
            'audio_binding': audio_binding,
            'publications': results, 'deliberately_invalid_rejected': rejected,
            'audio_fixture': 'Synthetic PCM tones through actual FFmpeg',
            'reader_interoperability': 'NOT_RUN', 'nvda': 'NOT_RUN', 'wcag_conformance': 'NOT_CLAIMED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--java', required=True, type=Path)
    parser.add_argument('--lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'epubcheck.json')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--audio-directory', type=Path,
                        help='Explicit pinned external audio directory for installed acceptance')
    parser.add_argument('--audio-lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    args = parser.parse_args()
    result = inspect(args.archive.resolve(strict=True), args.java.resolve(strict=True),
                     args.lock.resolve(strict=True), require_installed=args.require_installed,
                     audio_directory=args.audio_directory, audio_lock_path=args.audio_lock)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
