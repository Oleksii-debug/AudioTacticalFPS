"""Optional Liblouis boundary tests; synthetic archives never qualify Braille.

The actual official DLL/table probe is separate and required on Windows before
the optional runtime can be described as exercised.
"""
from hashlib import sha1, sha256
import io
import json
import os
from pathlib import Path
import stat
from threading import Event
from types import SimpleNamespace
import zipfile

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.models import AudioCleanupPending
from carr_library.integrations import liblouis
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.reader.braille import BrailleSourceExport
from carr_library.research.export_file import capture_export_destination


class NativeShape:
    """Only this adapter sees the fixture's Windows branch; pathlib stays real."""
    name = 'nt'

    def __getattr__(self, name):
        return getattr(os, name)


def pinned_synthetic_archive(tmp_path, monkeypatch, *, damage=None):
    # No copied upstream table, DLL or private model: this tests inspection only.
    table = b'# synthetic table used only by boundary tests\n'
    name = 'synthetic.ctb'
    pin = {'git_blob_sha1': sha1(b'blob ' + str(len(table)).encode('ascii') + b'\0' + table).hexdigest(),
           'size': len(table)}
    monkeypatch.setattr(liblouis, '_TABLE_PINS', {name: pin})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        if damage != 'missing-library':
            info = zipfile.ZipInfo('bin/liblouis.dll')
            info.external_attr = ((stat.S_IFLNK | 0o600) if damage == 'linked-library'
                                  else (stat.S_IFREG | 0o600)) << 16
            archive.writestr(info, b'SYNTHETIC-NOT-EXECUTABLE')
        archive.writestr('share/liblouis/tables/' + name,
                         table + b'changed' if damage == 'changed-table' else table)
        if damage == 'duplicate':
            archive.writestr('BIN/LIBLOUIS.DLL', b'SYNTHETIC-ALIAS')
    raw = stream.getvalue()
    monkeypatch.setattr(liblouis, 'ARCHIVE_SIZE', len(raw))
    monkeypatch.setattr(liblouis, 'ARCHIVE_SHA256', sha256(raw).hexdigest())
    path = tmp_path / 'Обраний синтетичний архів.zip'
    path.write_bytes(raw)
    return path


def test_plain_text_keeps_literal_backslashes_unicode_and_logical_lines():
    assert liblouis.selected_text('Є ї ґ\\x2801\r\nsecond\rthird\n') == 'Є ї ґ\\x2801\nsecond\nthird\n'


@pytest.mark.parametrize('value', ['', '  ', 'a\0b', 'a\tb', 'a\fb', 'a\x85b',
                                     '\ud800', 'x' * 100001, 'x\n' * 4096],
                         ids=['empty', 'whitespace', 'nul', 'tab', 'form-feed',
                              'next-line', 'unpaired-surrogate', 'too-many-characters',
                              'too-many-lines'])
def test_input_limits_refuse_unreviewed_layout_and_invalid_text(value):
    with pytest.raises((ValueError, UnicodeError)):
        liblouis.selected_text(value)


def test_archive_hash_is_checked_before_any_zip_or_native_parser(tmp_path, monkeypatch):
    path = tmp_path / 'unreviewed.zip'
    path.write_bytes(b'not the reviewed official archive')
    monkeypatch.setattr(liblouis.zipfile, 'ZipFile',
                        lambda *a, **k: pytest.fail('Unreviewed bytes reached ZIP parsing.'))
    with pytest.raises(ValueError, match='exact reviewed official'):
        liblouis.inspect_archive(path)


def test_inspection_checks_synthetic_source_closure_without_loading_library(tmp_path, monkeypatch):
    path = pinned_synthetic_archive(tmp_path, monkeypatch)
    monkeypatch.setattr(liblouis, 'run_process', lambda *a, **k: pytest.fail('Inspection launched a child.'))
    report = liblouis.inspect_archive(path)
    assert report['native_execution'] == report['tactile_quality'] == 'NOT_RUN'
    assert report['tables'] == liblouis._TABLE_PINS
    assert report['archive_sha256'] == sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('damage', ['missing-library', 'linked-library', 'changed-table', 'duplicate'])
def test_selected_archive_members_are_exact_and_regular(tmp_path, monkeypatch, damage):
    path = pinned_synthetic_archive(tmp_path, monkeypatch, damage=damage)
    with pytest.raises(ValueError):
        liblouis.inspect_archive(path)


def test_hardlinked_archive_is_refused_before_read(tmp_path):
    original, linked = tmp_path / 'one.zip', tmp_path / 'two.zip'
    original.write_bytes(b'not runtime')
    os.link(original, linked)
    with pytest.raises(ValueError, match='links'):
        liblouis.inspect_archive(linked)


def test_archive_inspection_observes_cancellation_before_parsing(tmp_path, monkeypatch):
    path = pinned_synthetic_archive(tmp_path, monkeypatch)
    calls = []
    def stopped():
        calls.append(1)
        return len(calls) == 2
    monkeypatch.setattr(liblouis.zipfile, 'ZipFile',
                        lambda *a, **k: pytest.fail('Cancelled bytes reached ZIP parsing.'))
    with pytest.raises(InterruptedError):
        liblouis.inspect_archive(path, cancelled=stopped)


@pytest.mark.parametrize('damage', [None, 'exit', 'width', 'version', 'partial-report',
                                   'digest', 'eight-bit', 'bare-newline', 'oversize'])
def test_fixed_native_boundary_checks_immutable_result_and_isolation(tmp_path, monkeypatch, damage):
    archive = pinned_synthetic_archive(tmp_path, monkeypatch)
    monkeypatch.setattr(liblouis, 'os', NativeShape())
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'hostile-hooks'))
    monkeypatch.setenv('LOUIS_TABLEPATH', str(tmp_path / 'unselected-tables'))
    stage = tmp_path / 'Приватна стадія'
    stage.mkdir()
    calls = []
    def native(args, **options):
        calls.append((args, options))
        assert args[1:5] == ['-I', '-B', '-S', '-c']
        assert args[5] == liblouis._NATIVE
        compile(args[5], '<fixed-liblouis-payload>', 'exec')
        assert args[6] == sha256((stage / 'request.json').read_bytes()).hexdigest()
        assert options['owned_windows_job'] is True and options['allow_failure'] is True
        assert options['env']['LOUIS_TABLEPATH'] == 'tables'
        assert options['env']['PYTHONDONTWRITEBYTECODE'] == '1'
        assert 'PYTHONPATH' not in options['env'] and options['cwd'] == stage
        assert tuple((stage / 'tables').iterdir()) == (stage / 'tables/synthetic.ctb',)
        payload = (b'\x80' if damage == 'eight-bit' else
                   b'A\nB' if damage == 'bare-newline' else
                   b'A' * (liblouis.MAX_BRF_BYTES + 1) if damage == 'oversize' else b'ABC\r\nDEF')
        (stage / 'output.brf').write_bytes(payload)
        report = {'status': 'PASS', 'version': '3.39.0', 'widechar_bytes': 4,
                  'table': 'uk', 'size': len(payload), 'sha256': sha256(payload).hexdigest()}
        if damage == 'width':
            report['widechar_bytes'] = 2
        elif damage == 'version':
            report['version'] = 'unselected'
        elif damage == 'digest':
            report['sha256'] = '0' * 64
        elif damage == 'partial-report':
            report['consumed_characters'] = 1
        return SimpleNamespace(returncode=1 if damage == 'exit' else 0,
                               stdout=json.dumps(report), stderr=b'')
    monkeypatch.setattr(liblouis, 'run_process', native)
    if damage is None:
        output, report = liblouis.render_selected('Exact Unicode ї source.', 'uk', archive, stage)
        assert output == b'ABC\r\nDEF' and report['size'] == len(output)
        assert report['tactile_quality'] == 'NOT_RUN'
    else:
        with pytest.raises(ValueError):
            liblouis.render_selected('Exact Unicode ї source.', 'uk', archive, stage)
    assert len(calls) == 1


@pytest.fixture
def source(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'Приватна бібліотека')
    workspace = app.create_workspace('BRF retained source')
    imported = app.add_text(workspace, 'Original BRF source', 'Незмінний оригінал.\nSecond literal line.')
    ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1,
        memory_bytes=1024 ** 3, min_free_disk_bytes=0))
    service = BrailleSourceExport(app)
    selection = service.review(workspace, imported['edition'], 1, 1, 'uk')
    calls = []
    def render(text, table, archive, stage, *, cancelled):
        assert not cancelled()
        calls.append((text, table, archive, stage))
        (stage / 'owned-runtime-input').write_bytes(b'synthetic')
        return b'ABC\r\nDEF', {'status': 'PASS', 'synthetic': True, 'tactile_quality': 'NOT_RUN'}
    monkeypatch.setattr(liblouis, 'render_selected', render)
    return SimpleNamespace(app=app, workspace=workspace, imported=imported, service=service,
                           selection=selection, calls=calls, output=tmp_path / 'окремий.brf')


def originals(app):
    return {str(p.relative_to(app.assets_dir)): p.read_bytes()
            for p in app.assets_dir.rglob('*') if p.is_file()}


def test_real_source_export_is_separate_and_stage_is_clean_before_publication(source, monkeypatch):
    e = source
    old = originals(e.app)
    import carr_library.reader.braille as module
    writer = module.write_binary_export
    def publish(*args, **kwargs):
        assert e.calls and not e.calls[0][3].exists()
        return writer(*args, **kwargs)
    monkeypatch.setattr(module, 'write_binary_export', publish)
    result = e.service.export(e.selection, Path('synthetic-selected.zip'), e.output)
    assert result['status'] == 'complete' and e.output.read_bytes() == b'ABC\r\nDEF'
    assert result['quotation_authority'] is False and result['tactile_quality'] == 'NOT_RUN'
    assert originals(e.app) == old
    assert ResourceBroker.for_app(e.app.root).snapshot()['leases'] == []


@pytest.mark.parametrize('damage', ['archived-source', 'replaced-root', 'cancelled'])
def test_changed_source_scope_or_stop_blocks_native_work(source, tmp_path, damage):
    e = source
    cancelled = lambda: damage == 'cancelled'
    if damage == 'archived-source':
        e.app.archive_workspace(e.workspace)
    elif damage == 'replaced-root':
        e.app.root.rename(tmp_path / 'old-root')
        e.app.root.mkdir()
    with pytest.raises((ValueError, PermissionError, InterruptedError)):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.output, cancelled=cancelled)
    assert not e.calls and not e.output.exists()


def test_missing_or_changed_overwrite_approval_preserves_existing_destination(source):
    e = source
    e.output.write_bytes(b'previous explicit output')
    with pytest.raises(FileExistsError):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.output)
    approval = capture_export_destination(e.output, private_root=e.app.root)
    e.output.write_bytes(b'changed after question')
    with pytest.raises(ValueError, match='destination changed'):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.output, approval=approval)
    assert not e.calls and e.output.read_bytes() == b'changed after question'


def test_destination_appearing_during_native_work_is_never_overwritten(source, monkeypatch):
    e = source
    old = originals(e.app)
    render = liblouis.render_selected
    def appeared(*args, **kwargs):
        result = render(*args, **kwargs)
        e.output.write_bytes(b'a competing file')
        return result
    monkeypatch.setattr(liblouis, 'render_selected', appeared)
    with pytest.raises((FileExistsError, ValueError)):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.output)
    assert e.output.read_bytes() == b'a competing file' and originals(e.app) == old


@pytest.mark.parametrize('stop', ['cancel', 'archive'])
def test_post_native_source_revalidation_precedes_final_publication(source, monkeypatch, stop):
    e = source
    stopped = [False]
    render = liblouis.render_selected
    def changed(*args, **kwargs):
        result = render(*args, **kwargs)
        if stop == 'cancel':
            stopped[0] = True
        else:
            e.app.archive_workspace(e.workspace)
        return result
    monkeypatch.setattr(liblouis, 'render_selected', changed)
    with pytest.raises((ValueError, InterruptedError)):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.output,
                         cancelled=lambda: stopped[0])
    assert len(e.calls) == 1 and not e.output.exists()
    assert not e.calls[0][3].exists()


def test_pending_native_owner_keeps_stage_and_shared_admission_until_quiescent(source, monkeypatch):
    e = source
    seen, stages = [], []
    class Owner:
        quiescent_event = Event()
        def close(self):
            seen.append((stages[0].exists(),
                len(ResourceBroker.for_app(e.app.root).snapshot()['leases'])))
            self.quiescent_event.set()
    owner = Owner()
    def pending(value, table, archive, stage, *, cancelled):
        stages.append(stage)
        (stage / 'owned-input').write_bytes(b'still owned')
        raise AudioCleanupPending('Synthetic retained owner, no native qualification.', cleanup_owner=owner)
    monkeypatch.setattr(liblouis, 'render_selected', pending)
    with pytest.raises(AudioCleanupPending):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.output)
    assert seen == [(True, 1)] and owner.quiescent_event.is_set()
    assert not stages[0].exists() and not e.output.exists()
    assert ResourceBroker.for_app(e.app.root).snapshot()['leases'] == []


def test_private_destination_is_denied_before_any_native_launch(source):
    e = source
    with pytest.raises(ValueError):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.app.root / 'must-not-exist.brf')
    assert not e.calls and not (e.app.root / 'must-not-exist.brf').exists()


@pytest.mark.parametrize('existing', [False, True])
def test_source_archived_during_final_export_flush_cannot_commit_stale_brf(source, monkeypatch, existing):
    e = source
    import carr_library.reader.braille as module
    import carr_library.research.export_file as publication
    previous = b'previous explicitly selected output'
    if existing:
        e.output.write_bytes(previous)
    approval = capture_export_destination(e.output, private_root=e.app.root)
    original = originals(e.app)
    writer = module.write_binary_export
    actual_fsync = publication.os.fsync
    changed = []
    def publish(*args, **kwargs):
        assert e.calls and not e.calls[0][3].exists()
        def archive_after_flush(descriptor):
            actual_fsync(descriptor)
            if not changed:
                changed.append(True)
                e.app.archive_workspace(e.workspace)
        with monkeypatch.context() as patch:
            patch.setattr(publication.os, 'fsync', archive_after_flush)
            return writer(*args, **kwargs)
    monkeypatch.setattr(module, 'write_binary_export', publish)
    with pytest.raises((ValueError, PermissionError)):
        e.service.export(e.selection, Path('synthetic-selected.zip'), e.output, approval=approval)
    assert changed == [True]
    assert e.output.read_bytes() == previous if existing else not e.output.exists()
    assert originals(e.app) == original
    assert not list(e.output.parent.glob('.research-export-*'))
    assert ResourceBroker.for_app(e.app.root).snapshot()['leases'] == []


def test_actual_cli_preserves_unicode_receipt_through_strict_legacy_stdout(source, monkeypatch):
    e = source
    import sys
    import carr_library.reader.braille as module
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding='cp1252', errors='strict')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'stdout', output)
            result = module.main(['export', '--root', str(e.app.root),
                '--workspace', e.workspace, '--edition', e.imported['edition'],
                '--first', '1', '--last', '1', '--table', 'uk',
                '--runtime-archive', str(e.app.root.parent / 'обраний-архів.zip'),
                '--output', str(e.output)])
            output.flush()
        receipt = json.loads(raw.getvalue().decode('ascii'))
    finally:
        output.detach()
    assert result == 0 and receipt['status'] == 'complete'
    assert receipt['path'] == str(e.output) and receipt['source']['edition'] == e.imported['edition']
    assert e.output.read_bytes() == b'ABC\r\nDEF' and len(e.calls) == 1
    assert ResourceBroker.for_app(e.app.root).snapshot()['leases'] == []
