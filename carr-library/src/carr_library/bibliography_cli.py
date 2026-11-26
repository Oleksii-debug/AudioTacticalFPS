"""Headless import and CSL formatting for the same private academic library."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys


def _controller(args):
    from .application import LibraryApp
    from .ui.academic_library_dialog import AcademicController
    if not (args.data_dir / 'library.sqlite').is_file():
        raise ValueError('Choose an existing library data directory; this command never creates one.')
    return AcademicController(LibraryApp(args.data_dir), args.workspace)


def main(argv=None):
    from .citations.bibliography_workbench import (
        BibliographyWorkbench, EXPORT_FORMATS, LOCALES, NAME_MODES, load_style_file,
    )
    parser = argparse.ArgumentParser(description='Local bibliography import and citation styles')
    commands = parser.add_subparsers(dest='operation', required=True)
    commands.add_parser('styles', help='List the pinned offline CSL styles')
    importer = commands.add_parser('import', help='Preview a bibliography; --commit applies the selected rows')
    formatter = commands.add_parser('format', help='Format existing academic references without changing them')
    for command in (importer, formatter):
        command.add_argument('--data-dir', required=True, type=Path)
        command.add_argument('--workspace', required=True, help='Stable library ID')
    importer.add_argument('--input', required=True, type=Path)
    importer.add_argument('--type', choices=('ris', 'bibtex', 'csl-json'),
                          help='Defaults to the file extension .ris, .bib or .json')
    importer.add_argument('--commit', action='store_true', help='Import the previewed metadata into this library')
    importer.add_argument('--select', type=int, action='append', help='Zero-based preview row; repeat to select several')
    importer.add_argument('--skip-duplicates', action='store_true')
    formatter.add_argument('--reference', help='Format one reference; default is all active references')
    styles = formatter.add_mutually_exclusive_group()
    styles.add_argument('--style', default='apa', help='Use the styles command to list available IDs')
    styles.add_argument('--style-file', type=Path,
                        help='Explicit local independent CSL file; loaded offline with resource limits')
    formatter.add_argument('--locale', choices=[item[0] for item in LOCALES], default='en-US')
    formatter.add_argument('--names', choices=[item[0] for item in NAME_MODES], default='literal')
    formatter.add_argument('--format', choices=[item[0] for item in EXPORT_FORMATS], default='text')
    formatter.add_argument('--output', type=Path, help='Explicit destination outside the private library')
    formatter.add_argument('--overwrite', action='store_true', help='Allow replacement of the selected existing output')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'styles':
            from .citations.csl import available_styles
            print(json.dumps([asdict(style) for style in available_styles()], ensure_ascii=False, indent=2))
            return 0
        controller = _controller(args)
        if args.operation == 'import':
            format = args.type or {'.ris': 'ris', '.bib': 'bibtex', '.json': 'csl-json'}.get(args.input.suffix.lower())
            if format is None:
                raise ValueError('Choose a .ris, .bib or .json input, or specify --type.')
            with args.input.open('rb') as source:
                data = source.read(8 * 1024 * 1024 + 1)
            preview = controller.preview(data, format)
            report = {'workspace': preview.workspace, 'format': preview.format,
                      'source_sha256': preview.source_sha256, 'warnings': preview.warnings,
                      'entries': [dict(index=row.index, fields=dict(row.fields), warnings=row.warnings,
                                       duplicate_ids=row.duplicate_ids, duplicate_indices=row.duplicate_indices)
                                  for row in preview.entries]}
            if args.commit:
                selected = tuple(args.select) if args.select is not None else tuple(range(len(preview.entries)))
                report['result'] = asdict(controller.import_selected(
                    preview, selected, skip_duplicates=args.skip_duplicates))
            else:
                report['result'] = 'PREVIEW_ONLY'
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.format != 'text' and args.output is None:
            raise ValueError('HTML and Word export require an explicit --output file.')
        if args.output is not None and (args.output.exists() or args.output.is_symlink()) and not args.overwrite:
            raise ValueError('The output exists. Choose another path or explicitly use --overwrite.')
        workbench = BibliographyWorkbench(controller)
        style = load_style_file(args.style_file) if args.style_file is not None else args.style
        preview = workbench.preview(identity=args.reference, style=style,
                                     locale=args.locale, name_mode=args.names)
        if args.output is not None:
            workbench.export(preview, args.output, format=args.format, require_absent=not args.overwrite)
            print(json.dumps({'result': 'SAVED', 'output': str(args.output),
                              'style': preview.result.style.id,
                              'style_sha256': preview.result.style_sha256,
                              'custom_style': preview.result.custom_style,
                              'references': len(preview.result.entries), 'warnings': preview.warnings},
                             ensure_ascii=False, indent=2))
        else:
            print(workbench.copy_text(preview), end='')
            for warning in preview.warnings:
                print('Review: ' + warning, file=sys.stderr)
        return 0
    except (ImportError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f'Research bibliography: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
