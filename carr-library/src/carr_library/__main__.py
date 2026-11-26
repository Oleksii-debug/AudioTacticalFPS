"""Desktop launcher and installed-package diagnostic entrypoint."""
import argparse
import json
from pathlib import Path
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
        backup = Path(temp) / 'backup'
        checksum = app.backup(backup)
        from .library_backup import restore_backup
        restored = Path(temp) / 'restored'
        restore_backup(backup, restored, expected_manifest_sha256=checksum)
        recovered = LibraryApp(restored)
        if recovered.evidence(first, topic)[0].anchor.quote != hit.quote:
            raise RuntimeError('Backup quotation check failed.')
        return {'status': 'PASS', 'checks': ['original import', 'scoped search', 'exact quotation',
            'workspace isolation', 'saved research', 'reopen persistence', 'backup and restore'],
            'native_gui': 'NOT_RUN', 'windows_nvda': 'NOT_RUN'}


def main(argv=None):
    from . import __version__
    parser = argparse.ArgumentParser(description='Offline Universal Research Library')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--data-dir', type=Path, help='Private persistent data directory')
    parser.add_argument('--self-test', action='store_true', help='Check an isolated synthetic library and exit')
    parser.add_argument('--model-dir', type=Path, help='Verified local MiniLM model directory')
    parser.add_argument('--model-manifest', type=Path, help='Pinned model manifest outside the model directory')
    parser.add_argument('--backup', type=Path, help='Back up all local libraries to a new directory')
    parser.add_argument('--restore', type=Path, help='Restore a backup to a new --data-dir')
    parser.add_argument('--manifest-sha256', help='Previously recorded backup manifest checksum')
    args = parser.parse_args(argv)
    try:
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
        if args.backup:
            checksum = app.backup(args.backup)
            print(f'Backup complete. Keep this manifest checksum separately: {checksum}')
            return 0
        try:
            import wx
        except ImportError as exc:
            raise RuntimeError('The desktop components are missing. Run the supplied installer or install the desktop extra.') from exc
        from .ui.desktop_app import DesktopFrame
        from .preferences import remember_directory
        desktop = wx.App(False)
        frame = DesktopFrame(app, remember_data_dir=remember_directory)
        frame.Show()
        desktop.MainLoop()
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Research Library: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
