"""Public synthetic production fixtures shared by external format proofs.

No voice/model discovery, microphone or provider requests occur. The current
production renderer, FFmpeg, file publication and inspectors remain real.
"""
from dataclasses import replace
from hashlib import sha256
import math
import os
from pathlib import Path
import struct
import wave


def digest(path):
    from carr_library.audio.publication_io import read_identity
    return read_identity(path, lambda: None, 1024**3, allow_hardlinks=True)['sha256']


class SyntheticTones:
    def cache_identity(self, voice):
        return {'fixture': 'current-publication-tone-v1', 'voice': voice}

    def synthesize(self, text, destination, **kwargs):
        if kwargs.get('cancelled', lambda: False)():
            raise InterruptedError('Synthetic publication fixture cancelled.')
        with wave.open(str(destination), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'synthetic PCM tone'))
            stream.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(
                2 * math.pi * 440 * index / 24000))) for index in range(4800)))
        return destination


def runtime(*, require_installed=False):
    from carr_library.audio import daisy, publishing, publication_io, publication_source, production
    from carr_library.audio.runtime import find_ffmpeg, trusted_executable
    modules = (daisy, publishing, publication_io, publication_source, production)
    selected = find_ffmpeg()
    if selected is None:
        raise RuntimeError('Actual selected FFmpeg is required; this proof cannot skip.')
    binaries = {'ffmpeg': selected,
        'ffprobe': trusted_executable(selected.with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe'))}
    hashes = {name: digest(path) for name, path in binaries.items()}
    sources = Path(__file__).resolve().parents[1] / 'src' / 'carr_library' / 'audio'
    module_hashes = {}
    for module in modules:
        installed = Path(module.__file__).resolve(strict=True)
        source = sources / installed.name
        module_hashes[module.__name__] = digest(installed)
        if require_installed:
            if 'site-packages' not in {part.casefold() for part in installed.parts}:
                raise RuntimeError('Publication acceptance requires the installed application modules.')
            if not source.is_file() or digest(source) != module_hashes[module.__name__]:
                raise RuntimeError('Installed publication source differs from this exact checked-out proof.')
    bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve(strict=True) if os.environ.get('CARR_PROGRAM_DIR') else None
    manifest_path = bundle / 'bundle-manifest.json' if bundle else None
    manifest_hash = digest(manifest_path) if manifest_path else None
    if bundle:
        import json
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for name, path in binaries.items():
            relative = 'audio/bin/' + name + '.exe'
            if not path.samefile(bundle / relative) or hashes[name] != manifest['files'].get(relative):
                raise RuntimeError('Publication audio escaped the selected sealed bundle: ' + name)
        if require_installed and any(not Path(module.__file__).resolve().is_relative_to(bundle / '.venv')
                                     for module in modules):
            raise RuntimeError('A publication module escaped the installed bundle.')
    def unchanged():
        if any(digest(path) != hashes[name] for name, path in binaries.items()):
            raise RuntimeError('A native publication executable changed during acceptance.')
        if any(digest(module.__file__) != module_hashes[module.__name__] for module in modules):
            raise RuntimeError('A publication module changed during acceptance.')
        if manifest_path and digest(manifest_path) != manifest_hash:
            raise RuntimeError('The sealed bundle manifest changed during acceptance.')
    evidence = {'installed_required': require_installed, 'module_sha256': module_hashes,
        'audio_binaries': {name: {'path': str(path), 'sha256': hashes[name]}
                           for name, path in binaries.items()},
        'sealed_manifest_sha256': manifest_hash}
    return selected, evidence, unchanged


def produce(root, kind, ffmpeg):
    from carr_library.audio.production import parse_script, plan_narration, render_project
    from carr_library.audio.publishing import PublicationService, inspect_epub
    from carr_library.audio.daisy import inspect_daisy
    inspector = inspect_epub if kind == 'epub' else inspect_daisy
    script = ('[output sample_rate=24000 stems=false]\r\n'
              '# Архів & letters\r\nNarrator: Exact <original> & preserved words.\r\n'
              '[pause 0.001]\r\nReader: Другий рядок «цитати».\r\n'
              '# Closing\r\nNarrator: The final line.\r\n')
    literal = '  [pause 2]\r\nRead the brackets literally.\fДругий збережений сегмент.\r\n'
    results = []
    for name, output_format in (('narrated-from-wav', 'wav'), ('reused-job-mp3', 'mp3'),
                                 ('literal-source-pages', 'wav'), ('audio-only', 'wav')):
        case = root / name
        project, private = case / 'audio-project', case / 'library'
        project.mkdir(parents=True)
        private.mkdir()
        if name == 'literal-source-pages':
            plan = replace(plan_narration(literal, title='Exact literal pages'),
                           sample_rate=24000, export_stems=False)
        elif name == 'audio-only':
            (project / 'assets').mkdir()
            SyntheticTones().synthesize('', project / 'assets' / 'retained.wav')
            plan = parse_script('[output sample_rate=24000 stems=false]\n# Untranscribed retained sound\n[sound retained.wav]\n',
                                title='Untranscribed retained sound')
        else:
            plan = replace(parse_script(script, title='Synthetic synchronized publication'),
                           output_format=output_format)
        result = render_project(plan, project, engine=SyntheticTones(), ffmpeg=ffmpeg)
        if result['status'] != 'complete':
            raise RuntimeError('The existing production renderer did not complete.')
        before = {str(path.relative_to(project)): digest(path) for path in project.rglob('*')
                  if path.is_file() and not path.name.endswith('.lock')}
        service = PublicationService(project, private_root=private, ffmpeg=ffmpeg)
        output = case / ('publication.epub' if kind == 'epub' else 'publication.zip')
        if kind == 'epub':
            receipt = service.export_epub(result['run_key'], output, language='uk', creator='Public synthetic fixture')
        else:
            receipt = service.export_daisy(result['run_key'], output, language='uk',
                creator='Public synthetic fixture', publisher='Public synthetic validator fixture')
        provenance = inspector(output)
        if (provenance['original_script'] != plan.original_script
                or provenance['source']['approved_library_evidence'] is not False
                or provenance['audio']['reused'] != (output_format == 'mp3')
                or provenance['source']['frame_intervals'] != receipt['source']['frame_intervals']
                or digest(output) != receipt['artifact']['sha256']):
            raise AssertionError('The actual publication does not retain its source/audio/receipt contract.')
        if name == 'audio-only' and any(row['kind'] != 'audio-interval' for row in provenance['intervals']):
            raise AssertionError('A retained sound acquired an invented transcript.')
        if name == 'literal-source-pages' and provenance['source_display_encoding'] != 'json-string':
            raise AssertionError('The literal form-feed source lost its reversible display.')
        for relative, identity in before.items():
            if digest(project / relative) != identity:
                raise AssertionError('Publication changed a source production artifact.')
        if service.verify(receipt['id']) != receipt:
            raise AssertionError('The saved publication receipt did not verify unchanged.')
        results.append({'case': name, 'path': output, 'receipt': receipt})
    return results
