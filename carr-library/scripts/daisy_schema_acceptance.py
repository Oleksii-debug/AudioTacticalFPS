"""Offline structural validation with explicitly supplied, hash-pinned DTDs.

Requires the separate lxml validation tool. Does not download/install anything,
resolve arbitrary entities or certify full DAISY rules/reader interoperability.
"""
import argparse
import importlib.util
import os
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def schema_resolver(etree, paths):
    """Resolve only exact aliases of the already pinned local schema files."""
    approved = {}
    for name, path in paths.items():
        # Generate aliases from admitted paths. Never decode or normalise an
        # incoming URL, infer a path from its basename, or allow a file host.
        uri = path.as_uri()
        aliases = {str(path), uri}
        if uri.startswith('file:///'):
            # libxml2 emits this equivalent local spelling on Windows when an
            # approved DTD refers to a sibling entity (for example file:/D:/...).
            aliases.add('file:/' + uri[len('file:///'):])
        aliases.add('http://www.w3.org/TR/REC-SMIL/SMIL10.dtd' if name == 'SMIL10.dtd'
                    else 'http://www.w3.org/TR/xhtml1/DTD/' + name)
        for alias in aliases:
            if alias in approved and approved[alias] != path:
                raise ValueError('The selected validation schema aliases are ambiguous.')
            approved[alias] = path

    class Resolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            path = approved.get(url)
            if path is None:
                raise ValueError('The schema validator refused an unapproved resource: ' + url)
            return self.resolve_filename(str(path), context)
    return Resolver


def validate(publication, schemas):
    from lxml import etree
    from carr_library.durable_state import local_path, regular
    from carr_library.studio import daisy, publishing
    schemas = local_path(schemas, exists=True)
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'packaging' /
                           'daisy-validation-schemas.json').read_text(encoding='utf-8'))
    paths, identities = {}, {}
    for name, expected in manifest['files'].items():
        path = local_path(schemas / name, root=schemas, exists=True)
        if regular(path).st_size != expected['size'] or sha256(path.read_bytes()).hexdigest() != expected['sha256']:
            raise ValueError('The explicitly selected validation schema does not match its pin: ' + name)
        paths[name], identities[name] = path, expected['sha256']
    Resolver = schema_resolver(etree, paths)
    publication = local_path(publication, exists=True)
    provenance = daisy.inspect_daisy(publication)
    before = publishing._digest(publication, lambda: None, 32 * 1024**3)
    results = []
    with ZipFile(publication) as archive:
        for name in ('ncc.html', 'content.html', 'source.html', 'book.smil'):
            parser = etree.XMLParser(load_dtd=True, dtd_validation=True, no_network=True,
                                     resolve_entities=False, huge_tree=False)
            parser.resolvers.add(Resolver())
            etree.fromstring(archive.read(name), parser)
            results.append({'file': name, 'dtd_valid': True})
        # A well-formed but structurally invalid XHTML must actually fail.
        invalid = archive.read('ncc.html').replace(b'<body>', b'<body><unsupported-element />', 1)
        try:
            etree.fromstring(invalid, parser_for_html(Resolver, etree))
        except etree.XMLSyntaxError:
            rejected = True
        else:
            raise AssertionError('The deliberately invalid XHTML passed DTD validation.')
    if publishing._digest(publication, lambda: None, 32 * 1024**3) != before:
        raise ValueError('The DAISY archive changed during structural validation.')
    for name, path in paths.items():
        if sha256(path.read_bytes()).hexdigest() != identities[name]:
            raise ValueError('A validation schema changed during the conformance check: ' + name)
    return {'schema': 1, 'status': 'PASS', 'publication_sha256': before['sha256'],
        'publisher_module_sha256': sha256(Path(publishing.__file__).read_bytes()).hexdigest(),
        'daisy_module_sha256': sha256(Path(daisy.__file__).read_bytes()).hexdigest(),
        'lxml_version': list(etree.LXML_VERSION), 'libxml_version': list(etree.LIBXML_VERSION),
        'schema_sha256': identities, 'documents': results, 'invalid_xhtml_rejected': rejected,
        'original_script_sha256': provenance['source']['source_script_sha256'],
        'full_daisy_rule_conformance': 'NOT_RUN', 'reader_interoperability': 'NOT_RUN', 'nvda': 'NOT_RUN'}


def parser_for_html(resolver, etree):
    parser = etree.XMLParser(load_dtd=True, dtd_validation=True, no_network=True,
                             resolve_entities=False, huge_tree=False)
    parser.resolvers.add(resolver())
    return parser


def acceptance(schemas, *, require_installed=False, audio_directory=None, audio_lock_path=None):
    """Use the existing synthetic fixture and canonical render; no pytest dependency."""
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        from carr_library.audio import audiobook, dsp
        from carr_library.studio import clips, daisy, publishing, render
        modules = (audiobook, dsp, clips, daisy, publishing, render)
        if require_installed:
            source_root = Path(__file__).resolve().parents[1] / 'src' / 'carr_library'
            for module in modules:
                relative = Path(*module.__name__.split('.')[1:]).with_suffix('.py')
                if Path(module.__file__).read_bytes() != (source_root / relative).read_bytes():
                    raise RuntimeError('DAISY acceptance source differs from the tested candidate: ' + module.__name__)
        if require_installed and any('site-packages' not in {
                part.casefold() for part in Path(module.__file__).resolve().parts} for module in modules):
            raise RuntimeError('DAISY acceptance must use the installed application modules.')
        bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve(strict=True) if os.environ.get('CARR_PROGRAM_DIR') else None
        if bundle and require_installed and any(not Path(module.__file__).resolve().is_relative_to(bundle / '.venv')
                                               for module in modules):
            raise RuntimeError('A DAISY application module escaped the selected installed bundle.')
        # Reuse the already-reviewed stdlib-only synthetic tone fixture. This
        # helper imports no pytest and cannot select or discover a private voice.
        specification = importlib.util.spec_from_file_location('daisy_tone_fixture',
            Path(__file__).with_name('epubcheck_acceptance.py'))
        fixture = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(fixture)
        with TemporaryDirectory(prefix='resonance-daisy-conformance-') as temporary:
            root = Path(temporary)
            library = root / 'library'
            audio_root = Path(audio_directory).resolve(strict=True) if audio_directory is not None else None
            audio_pin = None
            if audio_root is not None:
                from carr_library.runtime_settings import load_settings, save_settings
                lock = (Path(audio_lock_path) if audio_lock_path is not None else
                        Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
                audio_pin = json.loads(lock.read_text(encoding='utf-8'))
                settings = load_settings(library / 'state')
                settings.update(ffmpeg=str(audio_root / 'bin' / 'ffmpeg.exe'),
                                ffprobe=str(audio_root / 'bin' / 'ffprobe.exe'))
                save_settings(library / 'state', settings)
            service = render.ProductionService(library / 'state' / 'studio', voices=fixture.SyntheticTones())
            try:
                runtime = service._runtime()
                audio_proof = {}
                manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8')) if bundle else None
                for name in ('ffmpeg','ffprobe'):
                    binary = Path(getattr(runtime, name)).resolve(strict=True)
                    identity = publishing._digest(binary, lambda: None, 1024**3)['sha256']
                    audio_proof[name] = {'path': str(binary), 'sha256': identity,
                                         'binding': 'observed source-test runtime'}
                    if audio_root is not None:
                        relative = 'bin/' + name + '.exe'
                        expected = audio_pin['output_files'][relative]
                        if (not binary.samefile(audio_root / relative)
                                or binary.stat().st_size != expected['size']
                                or identity != expected['sha256']):
                            raise RuntimeError('DAISY audio runtime differs from the selected pinned directory: ' + name)
                        audio_proof[name]['binding'] = 'explicit pinned audio directory'
                    elif bundle and manifest.get('bundled_audio'):
                        relative = 'audio/bin/' + name + '.exe'
                        if (not binary.samefile(bundle / relative)
                                or identity != manifest['files'].get(relative)):
                            raise RuntimeError('DAISY audio runtime escaped the sealed installed bundle: ' + name)
                        audio_proof[name]['binding'] = 'sealed bundled audio runtime'
                    elif require_installed:
                        raise RuntimeError('Installed DAISY acceptance requires explicit pinned or sealed bundled audio.')
                publisher = publishing.PublicationService(service)
                script = ('[Scene: Архів & letters]\r\nNarrator: Exact <source> & words.\r\n'
                          '[Pause: 0.2]\r\n[Scene: Conclusion]\r\nNarrator: Другий розділ.\r\n')
                project = service.create(script, title='Synthetic DAISY source', voice_ids=('synthetic-tone',))
                results = []
                for name, format in (('narrated-from-wav','wav'), ('reused-job-mp3','mp3')):
                    parent = root / name; parent.mkdir()
                    job = service.submit(project, parent, format=format, stems=False)
                    if service.run(job)['status'] != 'complete':
                        raise AssertionError('Canonical Studio fixture failed to render.')
                    output = root / (name + '.zip')
                    receipt = publisher.export_daisy(job, output, language='uk', publisher='Synthetic acceptance fixture')
                    proof = daisy.inspect_daisy(output)
                    if proof['original_script'] != script or proof['audio']['reused'] != (format == 'mp3'):
                        raise AssertionError('DAISY did not preserve its real source/audio contract.')
                    result = validate(output, schemas)
                    if result['publication_sha256'] != receipt['artifact']['sha256']:
                        raise AssertionError('DTD validation did not bind the published receipt bytes.')
                    result['case'] = name; results.append(result)
                selected = root / 'selected.wav'
                fixture.SyntheticTones().synthesize('', 'synthetic-tone', selected,
                    settings=None, cancelled=lambda: False)
                editor = clips.ClipEditor(library, service=service)
                audio_project = editor.create('Untranscribed retained audio')
                audio_project = editor.add(audio_project, audio_project.scenes[0].id, selected,
                                           role='music', start=.1, duration=.2)
                selected.unlink()
                parent = root / 'audio-only'; parent.mkdir()
                job = service.submit(audio_project, parent, stems=False)
                if service.run(job)['status'] != 'complete':
                    raise AssertionError('Canonical retained-clip fixture failed to render.')
                output = root / 'audio-only.zip'
                publisher.export_daisy(job, output, publisher='Synthetic acceptance fixture')
                if any(row['kind'] != 'audio-interval' for row in daisy.inspect_daisy(output)['intervals']):
                    raise AssertionError('The retained recording acquired an invented transcript.')
                result = validate(output, schemas)
                result['case'] = 'untranscribed-retained-clip'; results.append(result)
                return {'schema': 1, 'status': 'PASS', 'publications': results,
                    'audio_binaries': audio_proof, 'installed_required': require_installed,
                    'audio_fixture': 'Synthetic PCM tones through actual canonical Studio and FFmpeg',
                    'publisher_module_sha256': fixture.digest(publishing.__file__),
                    'daisy_module_sha256': fixture.digest(daisy.__file__),
                    'full_daisy_rule_conformance': 'NOT_RUN', 'reader_interoperability': 'NOT_RUN',
                    'speech_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}
            finally:
                service.close()
    finally:
        if native_handle is not None:
            native_handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication', type=Path,
                        help='Existing DAISY archive; omit to render the three canonical acceptance fixtures')
    parser.add_argument('--schemas', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true',
                        help='Require exact installed application modules and explicit pinned or sealed bundled audio')
    parser.add_argument('--audio-directory', type=Path,
                        help='Explicit pinned external audio directory for installed acceptance')
    parser.add_argument('--audio-lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    args = parser.parse_args()
    if args.publication is not None and args.require_installed:
        parser.error('--require-installed applies to the actual render acceptance, not an existing unqualified archive')
    result = (validate(args.publication, args.schemas) if args.publication is not None
              else acceptance(args.schemas, require_installed=args.require_installed,
                              audio_directory=args.audio_directory, audio_lock_path=args.audio_lock))
    with args.report.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
