"""Accessible text commands and the native desktop entry point."""
import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sqlite3
import sys

from .application import Library


def parser():
    command = argparse.ArgumentParser(prog='carr-library',
        description='Local research libraries with exact source quotations.')
    command.add_argument('--data-dir', type=Path, help='Private library directory')
    command.add_argument('--model-dir', type=Path, help='Explicit verified offline semantic model directory')
    command.add_argument('--model-manifest', type=Path, help='Verified manifest for the selected model')
    subs = command.add_subparsers(dest='command')
    subs.add_parser('gui', help='Open the native desktop interface')
    create = subs.add_parser('create', help='Create a research library')
    create.add_argument('name')
    create.add_argument('--description', default='')
    subs.add_parser('libraries', help='List active libraries')
    topic = subs.add_parser('topic', help='Create a research topic')
    topic.add_argument('workspace')
    topic.add_argument('name')
    topic.add_argument('--parent')
    imp = subs.add_parser('import', help='Import an explicitly selected local TXT or PDF')
    imp.add_argument('workspace')
    imp.add_argument('path', type=Path)
    imp.add_argument('--title')
    imp.add_argument('--author', action='append', default=[])
    imp.add_argument('--language')
    imp.add_argument('--edition')
    imp.add_argument('--volume')
    imp.add_argument('--work-id')
    imp.add_argument('--edition-id')
    imp.add_argument('--collection', action='append', default=[])
    jobs = subs.add_parser('jobs', help='List persistent import checkpoints')
    jobs.add_argument('workspace')
    resume = subs.add_parser('resume', help='Resume an import from retained originals')
    resume.add_argument('workspace')
    resume.add_argument('job')
    resume.add_argument('--retry', action='store_true')
    search = subs.add_parser('search', help='Search within one library')
    search.add_argument('workspace')
    search.add_argument('query')
    search.add_argument('--collection')
    search.add_argument('--mode', choices=('literal', 'thematic'), default='literal')
    search.add_argument('--excerpts', action='store_true')
    search.add_argument('--include-unverified', action='store_true',
                        help='Include clearly marked PDF previews; verified copy/save remains unavailable')
    search.add_argument('--limit', type=int, default=20)
    save = subs.add_parser('save', help='Save a freshly reverified search hit to a topic')
    save.add_argument('workspace')
    save.add_argument('topic')
    save.add_argument('query')
    save.add_argument('--result', type=int, default=1, help='One-based result number')
    export = subs.add_parser('export', help='Export one topic with source and notes separated')
    export.add_argument('workspace')
    export.add_argument('topic')
    export.add_argument('--format', choices=('text', 'markdown', 'ris'), default='markdown')
    export.add_argument('--output', type=Path, help='New file; existing files are never overwritten')
    archive = subs.add_parser('archive', help='Archive a library, retaining all evidence')
    archive.add_argument('workspace')
    backup = subs.add_parser('backup', help='Back up all published libraries and originals')
    backup.add_argument('destination', type=Path)
    backup.add_argument('--all-libraries', required=True, action='store_true',
                        help='Explicitly include all libraries, including archived ones')
    restore = subs.add_parser('restore', help='Restore a verified backup into a new directory')
    restore.add_argument('bundle', type=Path)
    restore.add_argument('destination', type=Path)
    restore.add_argument('--manifest-sha256', required=True,
                         help='Checksum retained independently when making the backup')
    subs.add_parser('check', help='Verify database, source identities and evidence integrity')
    return command


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2,
                     default=lambda item: asdict(item) if is_dataclass(item) else str(item)))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if bool(args.model_dir) != bool(args.model_manifest):
            raise ValueError('supply both --model-dir and --model-manifest')
        if args.command == 'restore':
            restored = Library.restore(args.bundle, args.destination, args.manifest_sha256)
            _print({'restored': str(restored.root), **restored.check()})
            return 0
        semantic = None
        if args.model_dir is not None:
            from .search.offline_runtime import OfflineSemantic
            semantic = OfflineSemantic(args.model_dir, args.model_manifest)
        library = Library(args.data_dir, semantic=semantic)
        if args.command in (None, 'gui'):
            try:
                from .desktop import launch
            except ImportError as exc:
                if exc.name and exc.name.startswith('wx'):
                    print('Desktop support is not installed. Install the desktop extra, '
                          'or use carr-library --help for text commands.', file=sys.stderr)
                    return 2
                raise
            return launch(library)
        if args.command == 'create':
            _print(library.create_workspace(args.name, args.description))
        elif args.command == 'libraries':
            _print(library.workspaces())
        elif args.command == 'topic':
            _print({'topic': library.create_topic(args.workspace, args.name, parent=args.parent)})
        elif args.command == 'import':
            result = library.import_file(args.workspace, args.path, title=args.title,
                authors=tuple(args.author), language=args.language, edition=args.edition,
                volume=args.volume, work_id=args.work_id, edition_id=args.edition_id,
                collections=tuple(args.collection))
            _print(result)
            return 0 if result['state'] == 'complete' else 3
        elif args.command == 'jobs':
            _print(library.import_jobs(args.workspace))
        elif args.command == 'resume':
            result = library.resume_import(args.workspace, args.job, retry=args.retry)
            _print(result)
            return 0 if result['state'] == 'complete' else 3
        elif args.command == 'search':
            _print(library.search(args.workspace, args.query, collection=args.collection,
                retrieval_mode=args.mode, result_mode='thematic' if args.excerpts else 'short',
                limit=args.limit, include_unverified=args.include_unverified))
        elif args.command == 'save':
            if not 1 <= args.result <= 100:
                raise ValueError('result number must be between 1 and 100')
            service = library.bind_scope(args.workspace, args.topic)
            hits = service.search(args.workspace, args.query, 'literal', 'short', args.result)
            if len(hits) < args.result:
                raise ValueError('requested search result does not exist')
            hit = hits[args.result - 1]
            evidence = service.save_hit(args.workspace, hit.ref, hit.revision, hit.quote, hit.citation)
            _print({'evidence': evidence})
        elif args.command == 'export':
            value = library.export_topic(args.workspace, args.topic, format=args.format)
            if args.output:
                with args.output.open('x', encoding='utf-8', newline='') as destination:
                    destination.write(value)
                _print({'saved': str(args.output)})
            else:
                print(value, end='')
        elif args.command == 'archive':
            library.archive_workspace(args.workspace)
            _print({'archived': args.workspace})
        elif args.command == 'backup':
            checksum = library.backup(args.destination, include_all_workspaces=args.all_libraries)
            _print({'saved': str(args.destination), 'manifest_sha256': checksum,
                    'pending_imports_included': False})
        elif args.command == 'check':
            _print(library.check())
        return 0
    except ModuleNotFoundError as exc:
        extras = {'pypdf': 'pdf', 'pypdfium2': 'pdf', 'PIL': 'pdf',
                  'fastembed': 'semantic', 'onnxruntime': 'semantic', 'tokenizers': 'semantic',
                  'huggingface_hub': 'semantic', 'numpy': 'semantic',
                  'wx': 'desktop', 'docx': 'docx', 'defusedxml': 'docx'}
        missing = (exc.name or '').split('.', 1)[0]
        if missing not in extras:
            raise
        print(f'Optional {extras[missing]} support is not installed. '
              f'Install carr-research-library[{extras[missing]}] to use this operation.',
              file=sys.stderr)
        return 2
    except sqlite3.Error:
        # Do not expose SQL, private source metadata or an internal traceback.
        # Programming errors outside SQLite still surface for diagnosis.
        print('Operation failed: the database rejected this change or could not be read. '
              'Check existing item IDs and run the check command.', file=sys.stderr)
        return 2
    except (ValueError, OSError, LookupError) as exc:
        print('Operation failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
