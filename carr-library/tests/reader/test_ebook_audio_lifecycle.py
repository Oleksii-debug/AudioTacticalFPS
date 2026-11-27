"""Retention and native-lifetime controls around actual retained EPUB preparations."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import pytest

from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.reader import ebook_audio
from carr_library.ui.ebook_audio_dialog import EbookAudioDialog

_spec = importlib.util.spec_from_file_location('epub_owned_fixtures',
    Path(__file__).with_name('test_ebook_audio.py'))
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)
audio, imported = _fixtures.audio, _fixtures.imported


def test_raw_nul_member_alias_never_enters_overlay_graph(audio):
    raw = _fixtures.make_epub(audio)
    from io import BytesIO
    class Alias(ZipInfo):
        def _encodeFilenameFlags(self):
            return b'OPS/recording.mp3\x00hidden', self.flag_bits
    output = BytesIO()
    with ZipFile(BytesIO(raw)) as source, ZipFile(output, 'w') as target:
        for item in source.infolist():
            target.writestr(Alias(item.filename) if item.filename == 'OPS/recording.mp3' else item,
                            source.read(item.filename))
    with ZipFile(BytesIO(output.getvalue())) as checked:
        assert checked.getinfo('OPS/recording.mp3').orig_filename != 'OPS/recording.mp3'
    with pytest.raises(ValueError):
        ebook_audio.parse_overlay(output.getvalue(), 1)


@pytest.mark.parametrize('masked', [False, True])
def test_pending_decoder_retains_source_clip_lock_and_shared_reservations(imported, monkeypatch, masked):
    from carr_library.audio import AudioRuntime
    session = ebook_audio.EbookAudioSession(imported[-1], 1)
    source_root = session.root
    exited, staged = Event(), []
    owner = CleanupOwner(exited.is_set)
    pending = AudioCleanupPending('Synthetic exact decoder still owns its stage.', cleanup_owner=owner)
    def inspect(runtime, source, **options):
        staged.append(Path(source).parent)
        assert session.broker.snapshot()['usage']['cpu_slots'] == 1
        if masked:
            try:
                raise pending
            except AudioCleanupPending:
                raise OSError('Synthetic journal failure masks the same native owner.')
        raise pending
    monkeypatch.setattr(AudioRuntime, 'inspect', inspect)
    with pytest.raises(OSError if masked else AudioCleanupPending):
        session.prepare(0)
    assert staged and staged[0].exists() and source_root.exists()
    assert session.broker.snapshot()['usage']['cpu_slots'] == 1
    assert not session._lock.acquire(blocking=False), 'Preparation ownership escaped before quiescence.'
    session.close()
    assert source_root.exists() and staged[0].exists()
    exited.set()
    assert owner.close()
    assert not source_root.exists() and not staged[0].exists()
    assert session.broker.snapshot()['leases'] == []


def test_explicit_restored_audio_settings_fail_before_decoder(imported, monkeypatch):
    from carr_library.audio import AudioRuntime
    from carr_library.durable_state import atomic_json
    from carr_library import runtime_settings
    app = imported[0]
    settings = runtime_settings.load_settings(app.root / 'state')
    settings['restore_requires_revalidation'] = True
    folder = app.root / 'state' / 'audio'; folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / 'engines.json', settings, replace=True)
    session = ebook_audio.EbookAudioSession(imported[-1], 1)
    monkeypatch.setattr(AudioRuntime, 'inspect',
                        lambda *a, **k: pytest.fail('Restored configuration reached a decoder.'))
    try:
        with pytest.raises(ValueError, match='revalidat|restor'):
            session.prepare(0)
        assert len(session.broker.snapshot()['leases']) == 1
    finally:
        session.close()
    assert session.broker.snapshot()['leases'] == []


def test_direct_destroy_discards_queued_actual_pcm_and_keeps_borrowed_worker(imported):
    session = ebook_audio.EbookAudioSession(imported[-1], 1)
    queued, ready, errors = [], Event(), []
    with ThreadPoolExecutor(max_workers=1) as executor:
        ui = EbookAudioDialog.__new__(EbookAudioDialog)
        ui.executor, ui.parent_cancelled = executor, lambda: False
        ui.cancelled = Event()
        ui.closed = ui.closing = ui.busy = ui.destroyed = False
        ui._owner_id = 301
        ui._result_lock, ui._completed = Lock(), {}
        ui.session, ui.transport, ui.player, ui._pending, ui.progress = session, None, None, None, None
        ui.timer = SimpleNamespace(Stop=lambda: None)
        ui.guard = lambda: None
        ui._enable = lambda available: errors.append('late widget access') if ui.destroyed else None
        ui._error = lambda error: errors.append(str(error))
        def after(callback):
            queued.append(callback); ready.set()
        ui.wx = SimpleNamespace(CallAfter=after)
        ui._submit(lambda: session.prepare(0), lambda result: pytest.fail('Destroyed reader adopted audio.'),
                   lambda result: result.close())
        assert ready.wait(15), 'The actual decoder did not reach its queued completion.'
        assert len(ui._completed) == 1
        prepared = next(iter(ui._completed.values()))[0]
        assert prepared.path.exists()
        # Child control destruction cannot revoke the owner.
        EbookAudioDialog._destroyed(ui, SimpleNamespace(GetId=lambda: 999, Skip=lambda: None))
        assert not ui.closed
        EbookAudioDialog._destroyed(ui, SimpleNamespace(GetId=lambda: 301, Skip=lambda: None))
        assert ui.closed and ui.destroyed
        assert not prepared.path.exists() and not session.root.exists()
        for callback in queued:
            callback()
        assert not errors
        assert executor.submit(lambda: 'still parent-owned').result(timeout=2) == 'still parent-owned'
    assert session.broker.snapshot()['leases'] == []


def test_reading_desk_forwards_actual_shared_progress_owner_and_borrowed_worker(monkeypatch):
    from carr_library.ui.reading_desk import ReadingDialog
    from carr_library.ui import ebook_audio_dialog
    progress, worker, desk, window = object(), object(), object(), object()
    page = SimpleNamespace(ordinal=3)
    checked, shown, focus = [], [], []
    ui = ReadingDialog.__new__(ReadingDialog)
    ui.parent = SimpleNamespace(operation_progress=progress)
    ui.dialog, ui.executor = window, worker
    ui.busy = ui.closed = False
    ui.cancelled = Event()
    ui.controller = SimpleNamespace(current=page, desk=desk,
                                    check=lambda: checked.append(True))
    ui.text = SimpleNamespace(SetFocus=lambda: focus.append(True))
    ui._error = lambda error: pytest.fail(str(error))
    def reader(parent, actual_desk, ordinal, guard, **options):
        assert parent is window and actual_desk is desk and ordinal == 3
        assert options['progress'] is progress
        assert options['executor'] is worker
        assert not options['parent_cancelled']()
        def show():
            guard()
            shown.append(True)
        return SimpleNamespace(show=show)
    monkeypatch.setattr(ebook_audio_dialog, 'EbookAudioDialog', reader)
    ui.open_audio()
    assert shown == [True] and checked == [True, True] and focus == [True]
    assert ui._audio_dialog is None
