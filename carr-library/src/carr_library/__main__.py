"""Desktop launcher and installed-package diagnostic entrypoint."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


def self_test():
    """Exercise real SQLite and retained originals without touching user data."""
    from .application import LibraryApp
    with TemporaryDirectory(prefix='research-library-check-') as temp:
        app = LibraryApp(Path(temp) / 'library')
        first = app.create_workspace('Synthetic research')
        second = app.create_workspace('Separate research')
        collection = app.create_collection(first, 'Transport')
        topic = app.create_topic(first, 'Railways')
        text = 'Railway transport connected industrial cities.\r\nЗалізниця з’єднувала міста.'
        imported = app.add_text(first, 'Synthetic source', text, authors=('Test author',),
                                collections=(collection,))
        if imported['state'] != 'complete':
            raise RuntimeError('Import did not complete.')
        hit, = app.search(first, 'Railway transport', collection=collection)
        if app.copy(first, hit.ref, hit.revision, 'quote') != 'Railway transport':
            raise RuntimeError('Exact quotation check failed.')
        if app.search(second, 'Railway transport'):
            raise RuntimeError('Workspace isolation failed.')
        evidence = app.save_hit(first, topic, hit.ref, hit.revision, 'A separate test note.')
        reopened = LibraryApp(app.root)
        saved, = reopened.evidence(first, topic)
        if saved.evidence_id != evidence or 'A separate test note.' not in reopened.export_topic(first, topic):
            raise RuntimeError('Saved research did not survive reopening.')
        from .research.academic_library import AcademicLibrary
        with reopened.write(first) as db:
            references = AcademicLibrary(db, first)
            reference = references.create(title='Synthetic academic reference',
                authors=('Synthetic researcher',), year='2026', reading_status='reading',
                priority='high', progress=25, tags=('Installation check',),
                edition_id=imported['edition'])
        with reopened.read(first) as db:
            references = AcademicLibrary(db, first)
            if references.list(status='reading') != (reference,):
                raise RuntimeError('Academic reading-list check failed.')
            if 'Synthetic academic reference' not in references.export(format='ris'):
                raise RuntimeError('Academic bibliography export check failed.')
        backup = Path(temp) / 'backup'
        checksum = app.backup(backup)
        from .library_backup import restore_backup
        restored = Path(temp) / 'restored'
        restore_backup(backup, restored, expected_manifest_sha256=checksum)
        recovered = LibraryApp(restored)
        if recovered.evidence(first, topic)[0].anchor.quote != hit.quote:
            raise RuntimeError('Backup quotation check failed.')
        with recovered.read(first) as db:
            if AcademicLibrary(db, first).get(reference.id) != reference:
                raise RuntimeError('Academic reference backup check failed.')
        return {'status': 'PASS', 'checks': ['original import', 'scoped search', 'exact quotation',
            'workspace isolation', 'saved research', 'reopen persistence', 'academic reading list',
            'bibliography export', 'backup and restore'],
            'native_gui': 'NOT_RUN', 'windows_nvda': 'NOT_RUN'}


def main(argv=None, *, delivery=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ['delivery']:
        from .delivery_cli import main as delivery_main
        return delivery_main(arguments[1:], service=delivery)
    if arguments[:1] == ['--carr-doi-metadata-worker'] and len(arguments) in (1, 3):
        from .academic_integration.doi import _worker_main
        return _worker_main(arguments[1:])
    if len(arguments) == 3 and arguments[0] == '--carr-sapi-native-worker':
        from .speech.sapi_native_worker import main as native_main
        return native_main(arguments[1:])
    if arguments and arguments[0] in ('translate', 'translation'):
        from .translation_cli import main as translation_main
        return translation_main(arguments[1:])
    if arguments and arguments[0] in ('transcribe', 'transcription'):
        from .transcription_cli import main as transcription_main
        return transcription_main(arguments[1:])
    if arguments[:1] == ['sync']:
        from .sync.cli import main as sync_main
        return sync_main(arguments[1:])
    if arguments[:1] == ['backup-incremental']:
        from .backup_incremental_cli import main as backup_incremental_main
        return backup_incremental_main(arguments[1:])
    if arguments[:1] == ['operations']:
        from .operations_cli import main as operations_main
        return operations_main(arguments[1:])
    if arguments and arguments[0] in ('studio', 'assistant'):
        from .studio.cli import main as studio_main, assistant_main
        return (studio_main if arguments[0] == 'studio' else assistant_main)(arguments[1:])
    from . import __version__
    parser = argparse.ArgumentParser(description='Resonance offline research library and audio workspace')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--data-dir', type=Path, help='Private persistent data directory')
    parser.add_argument('--self-test', action='store_true', help='Check an isolated synthetic library and exit')
    parser.add_argument('--model-dir', type=Path, help='Verified local MiniLM model directory')
    parser.add_argument('--model-manifest', type=Path, help='Pinned model manifest outside the model directory')
    parser.add_argument('--backup', type=Path, help='Back up all local libraries to a new directory')
    parser.add_argument('--restore', type=Path, help='Restore a backup to a new --data-dir')
    parser.add_argument('--manifest-sha256', help='Previously recorded backup manifest checksum')
    ocr = parser.add_mutually_exclusive_group()
    ocr.add_argument('--list-ocr-models', action='store_true',
                     help='List the pinned official OCR catalogue; never downloads models')
    ocr.add_argument('--install-ocr-models', type=Path, metavar='FOLDER',
                     help='Verify and install selected OCR models from a local offline folder')
    ocr.add_argument('--verify-ocr-models', metavar='LANGUAGES',
                     help='Verify installed selected models, for example eng+ukr+script/Latin')
    parser.add_argument('--ocr-profile', choices=('fast', 'best'),
                        help='Pinned Fast or Best model family for OCR model operations')
    parser.add_argument('--ocr-model-store', type=Path,
                        help='Absolute local OCR model-store folder')
    parser.add_argument('--ocr-model', action='append', default=[],
                        help='Model identifier for offline installation; repeat for mixed languages')
    parser.add_argument('--ocr-all-models', action='store_true',
                        help='Explicitly install the complete selected profile from the local folder')
    parser.add_argument('--ocr-model-search', default='',
                        help='Filter --list-ocr-models by language, script or model identifier')
    research = parser.add_mutually_exclusive_group()
    research.add_argument('--document-formats',action='store_true',
                          help='Print supported import formats and fidelity limits without opening a library')
    research.add_argument('--list-libraries', action='store_true',
                          help='Print active library names and stable IDs as JSON, without opening the desktop')
    research.add_argument('--search', metavar='TEXT',
                          help='Search explicitly selected libraries and print source excerpts as JSON')
    from .acquisition.cli import add_arguments as add_acquisition_arguments, validate_arguments as validate_acquisition_arguments
    add_acquisition_arguments(parser,research)
    parser.add_argument('--workspace', action='append', dest='workspaces',
                        help='Library ID for search or identifier lookup; only search accepts multiple libraries')
    parser.add_argument('--search-mode', choices=('phrase', 'word', 'terms', 'advanced', 'thematic', 'semantic'),
                        default='phrase', help='Retrieval method for --search')
    parser.add_argument('--result-mode', choices=('short', 'thematic'), default='short',
                        help='Short quotations or contiguous thematic excerpts')
    parser.add_argument('--title', help='Source title filter for --search')
    parser.add_argument('--author', help='Source author filter for --search')
    parser.add_argument('--language', help='Exact language code filter for --search')
    parser.add_argument('--edition', help='Exact edition filter for --search')
    parser.add_argument('--limit-per-library', type=int, default=20,
                        help='Maximum results from each selected library, 1 to 100')
    from .audio.cli import add_arguments as add_audio_arguments, requested as audio_requested
    add_audio_arguments(parser)
    args = parser.parse_args(argv)
    validate_acquisition_arguments(parser, args)
    identifier_lookup = args.lookup_doi is not None or args.lookup_isbn is not None
    ocr_operation = (args.list_ocr_models or args.install_ocr_models is not None
                     or args.verify_ocr_models is not None)
    operations = (audio_requested(args), ocr_operation, args.document_formats,
                  args.list_libraries, args.search is not None, identifier_lookup,
                  args.self_test, args.backup is not None, args.restore is not None)
    if sum(bool(value) for value in operations) > 1:
        parser.error('Choose one operation: audio, OCR models, document formats, library search, '
                     'DOI or ISBN lookup, self-test, backup, or restore.')
    if (args.ocr_model or args.ocr_all_models) and not args.install_ocr_models:
        parser.error('--ocr-model and --ocr-all-models require --install-ocr-models.')
    if args.ocr_all_models and args.ocr_model:
        parser.error('Choose explicit --ocr-model entries or --ocr-all-models, not both.')
    if args.ocr_model_search and not args.list_ocr_models:
        parser.error('--ocr-model-search requires --list-ocr-models.')
    if args.ocr_model_store and not ocr_operation:
        parser.error('--ocr-model-store requires an OCR model operation.')
    if args.ocr_profile and not ocr_operation:
        parser.error('--ocr-profile requires an OCR model operation.')
    args.ocr_profile = args.ocr_profile or 'fast'
    if args.search is not None and not args.workspaces:
        parser.error('--search requires at least one --workspace; use --list-libraries to find its ID.')
    if args.workspaces and args.search is None and not identifier_lookup:
        parser.error('--workspace is used with --search, --lookup-doi or --lookup-isbn.')
    native_handle = local_audio_binding = None
    frame = None
    try:
        if args.document_formats:
            from .extract.document_formats import supported_formats_text
            print(supported_formats_text())
            return 0
        from .native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        if ocr_operation:
            from .extract.model_catalog import models, profile_provenance
            from .extract.ocr_model_store import OcrModelStore
            store = OcrModelStore(args.ocr_model_store)
            if args.list_ocr_models:
                installed = set(store.available(args.ocr_profile))
                entries = models(args.ocr_profile, args.ocr_model_search)
                result = {**profile_provenance(args.ocr_profile),
                          'network_access': False,
                          'models': [{**asdict(model), 'installed': model.identifier in installed,
                                      'source_url': model.download_url} for model in entries],
                          'total_bytes': sum(model.size for model in entries)}
            elif args.install_ocr_models:
                selected = ([m.identifier for m in models(args.ocr_profile)]
                            if args.ocr_all_models else args.ocr_model)
                if not selected:
                    parser.error('Choose one or more --ocr-model entries, or --ocr-all-models.')
                result = asdict(store.install(args.ocr_profile, args.install_ocr_models, selected))
            else:
                result = store.select(args.ocr_profile, args.verify_ocr_models).fingerprint()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.self_test:
            print(json.dumps(self_test(), ensure_ascii=False))
            return 0
        if args.restore:
            if not args.data_dir or not args.manifest_sha256:
                parser.error('--restore requires a new --data-dir and --manifest-sha256')
            from .library_backup import restore_backup
            restore_backup(args.restore, args.data_dir, expected_manifest_sha256=args.manifest_sha256)
            print('Backup restored. Open the new data directory to continue.')
            return 0
        location_notice = ''
        if args.data_dir is None:
            from platformdirs import user_data_path
            from .preferences import selected_directory
            default = user_data_path('Universal Research Library', appauthor=False, roaming=False)
            try:
                selected = selected_directory()
                if selected is not None and not (selected / 'library.sqlite').is_file():
                    location_notice = 'The last library folder is unavailable. Use Library, Open existing library folder to locate it.'
                    selected = None
                args.data_dir = selected or default
            except (OSError, ValueError) as exc:
                location_notice = f'Could not read the last library location: {exc}. Use Library, Open existing library folder.'
                args.data_dir = default
        if audio_requested(args):
            from .audio.cli import execute as execute_audio
            media_operation = bool(args.music_sample or ((args.audio_output or args.studio_resume)
                and not any((args.voices, args.studio_list, args.studio_jobs, args.studio_status))))
            if media_operation:
                from .audio.local_runtime import open_local_audio_runtime
                with open_local_audio_runtime(args.data_dir, strict=True):
                    return execute_audio(args, args.data_dir)
            return execute_audio(args, args.data_dir)
        from .application import LibraryApp
        ranker = None
        model_notice = ''
        if args.model_dir or args.model_manifest:
            if not args.model_dir or not args.model_manifest:
                parser.error('Supply both --model-dir and --model-manifest')
            try:
                from .semantic import OfflineSemanticRanker
                ranker = OfflineSemanticRanker(args.model_dir, args.model_manifest)
            except (ImportError, ValueError, RuntimeError, OSError) as exc:
                model_notice = f'Local semantic model unavailable: {exc}. Literal and thematic keyword search remain available.'
        app = LibraryApp(args.data_dir, semantic_ranker=ranker)
        app.semantic_notice = ' '.join(filter(None, (location_notice, model_notice)))
        if identifier_lookup:
            from .acquisition.cli import run as run_acquisition
            return run_acquisition(app,args)
        if args.list_libraries:
            print(json.dumps([asdict(row) for row in app.workspaces()], ensure_ascii=False, indent=2))
            return 0
        if args.search is not None:
            from .search.federated import FederatedSearch
            notices = []
            session = FederatedSearch(app).search(args.workspaces, args.search,
                mode=args.search_mode, result_mode=args.result_mode, title=args.title,
                author=args.author, language=args.language, edition=args.edition,
                limit_per_library=args.limit_per_library, on_status=notices.append)
            print(json.dumps({'query': session.query, 'mode': session.mode,
                'limit_per_library': session.limit_per_library,
                'ordering': 'Selected libraries in selection order; relevance is local to each library.',
                'notices': [*([app.semantic_notice] if app.semantic_notice else []), *notices],
                'libraries': [{'id': group.workspace, 'name': group.library_name,
                               'results': [asdict(hit.result) for hit in group.hits]}
                              for group in session.groups]}, ensure_ascii=False, indent=2))
            return 0
        if args.backup:
            checksum = app.backup(args.backup)
            print(f'Backup complete. Keep this manifest checksum separately: {checksum}')
            return 0
        from .audio.local_runtime import prepare_local_audio_runtime
        local_audio_binding = prepare_local_audio_runtime(app.root).activate(strict=False)
        app.semantic_notice = ' '.join(filter(None, (app.semantic_notice, local_audio_binding.notice)))
        try:
            import wx
        except ImportError as exc:
            raise RuntimeError('The desktop components are missing. Run the supplied installer or install the desktop extra.') from exc
        from .ui.desktop_app import DesktopFrame
        from .preferences import remember_directory
        desktop = wx.App(False)
        frame = DesktopFrame(app, remember_data_dir=remember_directory,
                             audio_runtime_binding=local_audio_binding, **(
            {'delivery': delivery} if delivery is not None else {}))
        frame.Show()
        desktop.MainLoop()
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f'Research Library: {exc}', file=sys.stderr)
        return 1
    finally:
        active_binding = getattr(frame, 'audio_runtime_binding', None) if frame is not None else local_audio_binding
        if active_binding is not None:
            active_binding.close()
        if local_audio_binding is not None and local_audio_binding is not active_binding:
            local_audio_binding.close()
        if native_handle is not None:
            native_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
