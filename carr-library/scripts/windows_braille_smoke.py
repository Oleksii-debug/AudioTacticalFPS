"""Actual optional Liblouis C API + retained-source export on native Windows.

Acquire the exact official ZIP before the offline gate. This program never
downloads it, embeds it in a deliverable or claims tactile/NVDA qualification.
"""
import argparse
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

MODULES = (
    'carr_library.__main__', 'carr_library.application',
    'carr_library.integrations.liblouis', 'carr_library.reader.braille',
    'carr_library.reader.reading_desk', 'carr_library.speech.source',
    'carr_library.research.export_file', 'carr_library.audio.process',
    'carr_library.operations.broker', 'carr_library.operations.models',
    'carr_library.ui.braille_actions', 'carr_library.ui.reading_desk',
)
REQUIRED = (
    'native Windows x64', 'exact installed module bytes',
    'official archive and 17-table source closure',
    'actual isolated installed CLI inspection',
    'actual isolated installed CLI export',
    'English uncontracted literal cells', 'Ukrainian literal cells',
    'Unicode archive and private source paths',
    'long line beyond legacy CLI buffer', 'literal backslash is not an escape',
    'unsupported symbol refused without output', 'eight-dot cells refused without output',
    'source archived after review refuses native work',
    'destination changed since question remains untouched',
    'original bytes unchanged', 'all native reservations released',
    'installed module bytes unchanged after native execution',
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--runtime-archive', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or sys.maxsize <= 2 ** 32:
        raise RuntimeError('This required optional-runtime probe needs native Windows x64.')
    source = args.source_root.resolve(strict=True)
    installed = Path(sys.prefix).resolve(strict=True)
    modules = {name: importlib.import_module(name) for name in MODULES}
    def origins():
        result = {}
        for name, module in modules.items():
            actual = Path(module.__file__).resolve(strict=True)
            expected = source / Path(*name.split('.')).with_suffix('.py')
            if (not actual.is_relative_to(installed) or 'site-packages' not in actual.parts
                    or actual.is_relative_to(source.parent)):
                raise AssertionError('The optional runtime probe imported a checkout module: ' + name)
            digest = sha256(actual.read_bytes()).hexdigest()
            if digest != sha256(expected.read_bytes()).hexdigest():
                raise AssertionError('The installed optional runtime differs from source: ' + name)
            result[name] = {'path': str(actual), 'sha256': digest}
        return result
    before = origins()
    from carr_library.application import LibraryApp
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    from carr_library.integrations import liblouis
    from carr_library.operations import ResourceBroker
    from carr_library.reader.braille import BrailleSourceExport
    from carr_library.research.export_file import capture_export_destination

    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
    check('native Windows x64', True)
    check('exact installed module bytes', len(before) == len(MODULES))
    archive = args.runtime_archive.resolve(strict=True)
    inspection = liblouis.inspect_archive(archive)
    check('official archive and 17-table source closure',
          inspection['archive_sha256'] == liblouis.ARCHIVE_SHA256 and len(inspection['tables']) == 17)
    with TemporaryDirectory(prefix='carr-brf-native-') as directory:
        root = Path(directory)
        unicode_archive = root / 'Обраний офіційний Liblouis.zip'
        with unicode_archive.open('xb') as output:
            output.write(archive.read_bytes())
        answer = run_process([sys.executable, '-I', '-B', '-m', 'carr_library', 'braille',
            'inspect', '--runtime-archive', str(unicode_archive)],
            owned_windows_job=True, budget=AudioBudget(timeout=30, max_log_bytes=65536))
        cli = json.loads(answer.stdout)
        check('actual isolated installed CLI inspection',
              cli['archive_sha256'] == inspection['archive_sha256']
              and cli['tables'] == inspection['tables'] and cli['native_execution'] == 'NOT_RUN')
        app = LibraryApp(root / 'Приватна бібліотека')
        workspace = app.create_workspace('Synthetic BRF qualification')
        service = BrailleSourceExport(app)
        runtime_proofs = []

        def publish(value, name, table):
            imported = app.add_text(workspace, name, value)
            selected = service.review(workspace, imported['edition'], 1, 1, table)
            original = {str(p.relative_to(app.assets_dir)): p.read_bytes()
                        for p in app.assets_dir.rglob('*') if p.is_file()}
            destination = root / (name + '.brf')
            receipt = service.export(selected, unicode_archive, destination)
            if original != {str(p.relative_to(app.assets_dir)): p.read_bytes()
                            for p in app.assets_dir.rglob('*') if p.is_file()}:
                raise AssertionError('The original retained source changed.')
            runtime_proofs.append(receipt['runtime'])
            return destination.read_bytes(), selected

        cli_source = app.add_text(workspace, 'Explicit installed CLI export', 'abc')
        cli_destination = root / 'cli-export.brf'
        answer = run_process([sys.executable, '-I', '-B', '-m', 'carr_library', 'braille',
            'export', '--root', str(app.root), '--workspace', workspace,
            '--edition', cli_source['edition'], '--first', '1', '--last', '1',
            '--table', 'en-ueb-g1', '--runtime-archive', str(unicode_archive),
            '--output', str(cli_destination)], owned_windows_job=True,
            budget=AudioBudget(timeout=90, max_log_bytes=65536))
        cli_export = json.loads(answer.stdout)
        check('actual isolated installed CLI export',
              cli_export['status'] == 'complete' and cli_destination.read_bytes() == b'ABC'
              and cli_export['runtime']['archive_sha256'] == liblouis.ARCHIVE_SHA256
              and cli_export['source']['edition'] == cli_source['edition'])
        runtime_proofs.append(cli_export['runtime'])
        english, _ = publish('abc', 'English cells', 'en-ueb-g1')
        check('English uncontracted literal cells', english == b'ABC')
        ukrainian, _ = publish('абв', 'Українські клітинки', 'uk')
        check('Ukrainian literal cells', ukrainian == b'ABW')
        check('Unicode archive and private source paths',
              all(row['version'] == '3.39.0' and row['widechar_bytes'] == 4
                  for row in runtime_proofs))
        long_line, _ = publish('abc' * 1000, 'Long literal line', 'en-ueb-g1')
        check('long line beyond legacy CLI buffer', long_line == b'ABC' * 1000)
        literal, _ = publish(r'\x2801', 'Literal slash', 'en-ueb-g1')
        cell, _ = publish('\u2801', 'Actual Unicode cell', 'en-ueb-g1')
        check('literal backslash is not an escape', cell == b'A' and literal != cell and len(literal) > 1)
        for value, label in (
                ('\U0001f680', 'unsupported symbol refused without output'),
                ('\u28ff', 'eight-dot cells refused without output')):
            imported = app.add_text(workspace, label, value)
            selected = service.review(workspace, imported['edition'], 1, 1, 'en-ueb-g1')
            destination = root / (label + '.brf')
            refused = False
            try:
                service.export(selected, unicode_archive, destination)
            except ValueError:
                refused = True
            check(label, refused and not destination.exists())
        imported = app.add_text(workspace, 'Explicit approval', 'abc')
        selected = service.review(workspace, imported['edition'], 1, 1, 'en-ueb-g1')
        existing = root / 'approved.brf'
        existing.write_bytes(b'OLD')
        approval = capture_export_destination(existing, private_root=app.root)
        existing.write_bytes(b'CHANGED')
        refused = False
        try:
            service.export(selected, unicode_archive, existing, approval=approval)
        except ValueError:
            refused = True
        check('destination changed since question remains untouched',
              refused and existing.read_bytes() == b'CHANGED')
        old = {str(p.relative_to(app.assets_dir)): p.read_bytes()
               for p in app.assets_dir.rglob('*') if p.is_file()}
        app.archive_workspace(workspace)
        refused = False
        try:
            service.export(selected, unicode_archive, root / 'archived.brf')
        except (ValueError, PermissionError):
            refused = True
        check('source archived after review refuses native work',
              refused and not (root / 'archived.brf').exists())
        check('original bytes unchanged', old == {
            str(p.relative_to(app.assets_dir)): p.read_bytes()
            for p in app.assets_dir.rglob('*') if p.is_file()})
        check('all native reservations released', ResourceBroker.for_app(app.root).snapshot()['leases'] == [])
    after = origins()
    check('installed module bytes unchanged after native execution', before == after)
    if set(checks) != set(REQUIRED) or len(checks) != len(REQUIRED):
        raise AssertionError('The exact required optional-runtime checks were not performed.')
    report = {'status': 'PASS', 'checks': checks, 'installed_origins': after,
        'official_runtime': inspection, 'native_runtime_proofs': runtime_proofs,
        'actual_C_API': 'PASS', 'actual_local_retained_source_exports': 'PASS',
        'plain_BRF': 'six-dot Braille ASCII; logical line boundaries only',
        'manual_NVDA': 'NOT_RUN', 'tactile_language_quality': 'NOT_RUN',
        'embosser_layout': 'NOT_RUN', 'native_reader_menu': 'NOT_RUN',
        'languages_135': 'NOT_RUN'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    # The existing per-child process owner is still responsible for descendants.
    # On this hard CI bound, process exit also closes its owned Windows Job.
    watchdog = threading.Timer(300, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    try:
        raise SystemExit(main())
    finally:
        watchdog.cancel()
