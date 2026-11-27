"""Native handler decisions with the actual shared operation session."""
from collections import deque
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import os
import sys

import pytest

from carr_library.audio.media_types import (AudioStream, ConversionPreset, MediaInspection)
from carr_library.ui import media_conversion as ui


class Control:
    def __init__(self, value=''):
        self.value, self.selection, self.items, self.enabled = value, -1, [], True
    def GetValue(self):
        return self.value
    def SetValue(self, value):
        self.value = value
    ChangeValue = SetValue
    def GetSelection(self):
        return self.selection
    def SetSelection(self, value):
        self.selection = value
    def GetStringSelection(self):
        return self.items[self.selection] if 0 <= self.selection < len(self.items) else ''
    def SetItems(self, values):
        self.items, self.selection = list(values), -1
    def Enable(self, enabled=True):
        self.enabled = bool(enabled)
    def SetInsertionPoint(self, position):
        pass


def fields():
    controls = {name: Control() for name in ConversionPreset.__dataclass_fields__}
    ui.apply_preset(controls, ConversionPreset())
    return controls


def inspected(root, *, tracks=1, name='Україна Ω.wav'):
    return MediaInspection(str(root / name), 100, 'a' * 64, (1, 2, 100, 3, 4),
        'matroska' if tracks > 1 else 'wav', tracks > 1,
        tuple(AudioStream(index + 1, 'pcm_s16le', 24000, 1, 1, 'uk', 'Доріжка ' + str(index), 16)
              for index in range(tracks)), ('b' * 64, 'c' * 64))


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.cancel_event = Event()
        self.working = self._closing = False
        self.pending, self.calls = None, []
        self.operation_progress = SimpleNamespace(request_cancel=Mock(), update=Mock())
    def run_job(self, title, operation, *, done, failed, **options):
        self.working = True
        self.pending = operation, done, failed
        self.calls.append((title, options))
    def finish(self, *, deliver=True):
        operation, done, failed = self.pending
        try:
            result = operation()
        except Exception as error:
            self.working = False
            if deliver:
                failed(error)
        else:
            self.working = False
            if deliver:
                done(result)


class Service:
    def __init__(self, root):
        self.library_root, self.ffmpeg = root, None
        self.calls, self.saved_rows, self.named = [], (), {}
        self.prepared = None
    def inspect(self, paths, *, cancelled, progress):
        assert not cancelled()
        self.calls.append(('inspect', tuple(paths)))
        return tuple(inspected(Path(path).parent, name=Path(path).name) for path in paths)
    def prepare(self, choices, *, allowance, title, cancelled, progress):
        assert not cancelled()
        self.calls.append(('prepare', tuple(choices), allowance))
        self.prepared = dict(id='a' * 32, title=title,
            processor=dict(ffmpeg_sha256='b' * 64, ffprobe_sha256='c' * 64),
            allowance=allowance.to_dict(), inputs=[dict(name=Path(row.source.path).name,
                stream_index=row.stream_index, preset=row.preset.to_dict(),
                streams=[stream.to_dict() for stream in row.source.streams]) for row in choices])
        return self.prepared['id']
    def definition(self, identity):
        assert identity == self.prepared['id']
        return self.prepared
    def run(self, identity, *, cancelled, progress):
        self.calls.append(('run', identity))
        if cancelled():
            raise InterruptedError('Synthetic acknowledged stop')
        return dict(id=identity, status='complete', completed=1, total=1)
    def verify(self, identity, *, cancelled):
        self.calls.append(('verify', identity))
        assert not cancelled()
        return dict(id=identity, status='complete', completed=1, total=1,
                    outputs=[dict(path='000001-Україна.flac', bytes=100, decoded_frames=24000)])
    def jobs(self, *, cancelled):
        assert not cancelled()
        return self.saved_rows
    def presets(self):
        return dict(self.named)
    def save_preset(self, name, preset):
        self.named[name] = preset.to_dict()
    def control(self, identity, action):
        self.calls.append(('control', identity, action))


def view_fixture(tmp_path):
    frame, queue = Frame(tmp_path), deque()
    view = object.__new__(ui.MediaConversionDialog)
    view.frame, view.root, view.service = frame, tmp_path, Service(tmp_path)
    view.wx = SimpleNamespace(CallAfter=lambda callback, *args: queue.append((callback, args)))
    view.setup, view.sources = ui.MediaSetup(), ()
    view.current_job = view.definition = view.summary = None
    view.rows, view.preset_names = (), ()
    view.fields = fields()
    for name in ('title', 'runtime', 'identity', 'frozen_allowance', 'source_list', 'stream',
                 'jobs_list', 'outputs', 'status', 'preset_list'):
        setattr(view, name, Control('Synthetic media title' if name == 'title' else ''))
    view.disk, view.timeout, view.threads = Control(32), Control(60), Control(2)
    names = ('files', 'folder', 'remove', 'runtime', 'inspect', 'apply', 'all_single',
             'preset_load', 'preset_save', 'new', 'refresh', 'load', 'prepare', 'start',
             'pause', 'stop', 'verify', 'export', 'close')
    view.buttons = {name: Control() for name in names}
    view.session = ui.TranscriptionSession(frame, dispatch=view.wx.CallAfter,
        changed=view.controls, report=view.report, close=Mock(), request_stop=view._request_stop)
    def finish(*, deliver=True):
        frame.finish(deliver=deliver)
        while queue:
            callback, args = queue.popleft()
            callback(*args)
    return view, finish


def prepare_view(view, finish, path):
    view.add_paths((path,))
    view.inspect()
    finish()
    view.apply()
    view.prepare()
    finish()


def test_native_output_fields_reuse_typed_profile_and_preserve_unicode_tags():
    controls = fields()
    controls['format'].SetSelection(ui._CHOICES['format'].index('flac'))
    controls['pcm_bits'].SetSelection(1)
    controls['title'].SetValue('Текст Ω')
    controls['bitrate_kbps'].SetValue(256)
    result = ui.preset_from_controls(controls)
    assert result.format == 'flac' and result.pcm_bits == 24 and result.title == 'Текст Ω'
    assert type(result.channels) is int and result.bitrate_kbps == 256


@pytest.mark.parametrize('field,value', [('bitrate_kbps', True), ('title', 'unsafe\nfield')])
def test_native_invalid_values_are_rejected_by_same_profile_contract(field, value):
    controls = fields()
    controls[field].SetValue(value)
    with pytest.raises(ValueError):
        ui.preset_from_controls(controls)


def test_missing_native_selection_cannot_reuse_an_implicit_audio_format():
    controls = fields()
    controls['format'].SetSelection(-1)
    with pytest.raises(ValueError, match='Choose'):
        ui.preset_from_controls(controls)


def test_multitrack_inputs_require_explicit_audio_index_and_single_track_batch_is_separate(tmp_path):
    setup = ui.MediaSetup()
    one, two = inspected(tmp_path), inspected(tmp_path, tracks=2, name='video.mkv')
    setup.inspected((one, two))
    assert setup.choices == {}
    assert setup.single_streams(ConversionPreset()) == 1
    assert set(setup.choices) == {0}
    with pytest.raises(ValueError, match='every input'):
        setup.frozen_choices()
    with pytest.raises(ValueError, match='audio stream'):
        setup.choose(1, 0, ConversionPreset())
    setup.choose(1, two.streams[1].index, ConversionPreset(format='flac', pcm_bits=24))
    assert [row.stream_index for row in setup.frozen_choices()] == [1, 2]


def test_reinspection_drops_old_stream_choices_instead_of_reusing_changed_metadata(tmp_path):
    setup = ui.MediaSetup()
    setup.inspected((inspected(tmp_path),))
    setup.single_streams(ConversionPreset())
    setup.inspected((inspected(tmp_path, tracks=2),))
    assert setup.choices == {}
    with pytest.raises(ValueError):
        setup.frozen_choices()


def test_preparation_is_separate_from_start_and_freezes_source_and_output_settings(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.add_paths((tmp_path / 'Україна Ω.wav',))
    view.inspect()
    assert view.service.calls == []
    finish()
    view.fields['format'].SetSelection(ui._CHOICES['format'].index('flac'))
    view.fields['pcm_bits'].SetSelection(1)
    view.apply()
    view.prepare()
    assert [row[0] for row in view.service.calls] == ['inspect']
    finish()
    assert [row[0] for row in view.service.calls] == ['inspect', 'prepare']
    assert view.current_job == 'a' * 32 and view.definition['inputs'][0]['preset']['pcm_bits'] == 24
    assert all(not field.enabled for field in view.fields.values())
    assert not view.buttons['prepare'].enabled and view.buttons['start'].enabled
    assert '33554432 bytes' in view.frozen_allowance.value
    with pytest.raises(ValueError, match='New job'):
        view.add_paths((tmp_path / 'another.wav',))
    view.start()
    finish()
    assert [row[0] for row in view.service.calls] == ['inspect', 'prepare', 'run', 'verify']
    assert view.summary['completed'] == 1 and view.buttons['export'].enabled


def test_new_job_reenables_controls_without_mutating_frozen_preparation(tmp_path):
    view, finish = view_fixture(tmp_path)
    prepare_view(view, finish, tmp_path / 'selected.wav')
    saved = view.service.prepared['inputs'][0]['preset'].copy()
    view.new()
    assert view.current_job is None and view.sources == () and view.setup.choices == {}
    assert all(field.enabled for field in view.fields.values())
    assert view.service.prepared['inputs'][0]['preset'] == saved
    assert view.frozen_allowance.value == '' and ui.preset_from_controls(view.fields) == ConversionPreset()


def test_ambiguous_native_stream_choice_is_empty_until_explicit_selection(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.setup.inspected((inspected(tmp_path, tracks=2),))
    view._input_labels()
    view.source_list.SetSelection(0)
    view.show_input()
    assert view.stream.GetSelection() == -1
    view.apply()
    assert view.setup.choices == {} and 'Explicitly' in view.status.value
    view.stream.SetSelection(1)
    view.apply()
    assert view.setup.frozen_choices()[0].stream_index == 2


def test_pause_and_stop_target_only_current_running_media_job(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.current_job = 'a' * 32
    view.session.busy, view.session.kind = True, 'prepare'
    view.pause()
    assert view.service.calls == []
    view.session.kind = 'run'
    view.pause()
    view.session.stop()
    assert view.service.calls == [('control', 'a' * 32, 'pause'), ('control', 'a' * 32, 'cancel')]
    assert view.session.cancel.is_set() and not view.buttons['stop'].enabled


def test_modal_close_waits_for_settled_operation_and_discards_late_callbacks(tmp_path):
    view, finish = view_fixture(tmp_path)
    prepare_view(view, finish, tmp_path / 'selected.wav')
    view.start()
    view.close()
    assert view.session.busy and view.session.close_requested and not view.session.closed
    view.session.close_dialog.assert_not_called()
    finish()
    assert view.session.closed
    view.session.close_dialog.assert_called_once()
    assert view.summary is None


def test_changed_library_settlement_closes_even_when_shared_frame_suppresses_callback(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.refresh()
    view.frame.library = SimpleNamespace(root=tmp_path / 'different')
    finish(deliver=False)
    assert view.session.closed
    view.session.close_dialog.assert_called_once()


def test_saved_job_refresh_preserves_exact_identity_or_clears_it_without_neighbor_control(tmp_path):
    view, finish = view_fixture(tmp_path)
    first = dict(id='a' * 32, title='First', status='prepared', inputs=1, completed=0)
    second = dict(id='b' * 32, title='Second', status='prepared', inputs=1, completed=0)
    view.service.saved_rows = (first, second)
    view.refresh(); finish()
    assert view.jobs_list.GetSelection() == -1
    view.jobs_list.SetSelection(1)
    view.service.saved_rows = (second, first)
    view.refresh(); finish()
    assert view.jobs_list.GetSelection() == 0
    view.service.saved_rows = (first,)
    view.refresh(); finish()
    assert view.jobs_list.GetSelection() == -1 and not view.buttons['load'].enabled


def test_readonly_output_verification_never_requires_runtime_selection(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.current_job = 'a' * 32
    view.verify()
    finish()
    assert view.service.ffmpeg is None and view.service.calls == [('verify', 'a' * 32)]
    assert view.summary['outputs'][0]['path'] == '000001-Україна.flac'


def test_stale_progress_cannot_touch_settled_or_replaced_operation(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.status.value = 'Current'
    view._progress(0, 1, 1, 'old')
    assert view.status.value == 'Current'
    view.session.busy, view.session.generation = True, 2
    view._progress(1, 1, 1, 'previous')
    assert view.status.value == 'Current'


def test_folder_selection_is_direct_bounded_and_does_not_follow_playlist_references(tmp_path):
    folder = tmp_path / 'media'
    folder.mkdir()
    source = folder / 'Україна.wav'
    source.write_bytes(b'selection-only; decoder admission happens later')
    (folder / 'playlist.m3u').write_text('../external.wav', encoding='utf-8')
    child = folder / 'subfolder'
    child.mkdir()
    (child / 'not-selected.wav').write_bytes(b'not selected')
    assert ui.folder_inputs(folder) == (str(source),)
    with pytest.raises(InterruptedError):
        ui.folder_inputs(folder, cancelled=lambda: True)


def test_folder_selection_refuses_hardlinked_original(tmp_path):
    source = tmp_path / 'source.wav'
    source.write_bytes(b'selection')
    os.link(source, tmp_path / 'alias.wav')
    with pytest.raises(ValueError):
        ui.folder_inputs(tmp_path)


@pytest.mark.parametrize('installed', [False, True], ids=['before-keyboard', 'after-keyboard'])
def test_installer_registers_one_shared_command_with_no_default_collision(monkeypatch, installed):
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec
    registry = CommandRegistry((CommandSpec('library.existing', 'Existing', 'main', 'Ctrl+J'),))
    overrides = {'library.existing': 'Ctrl+Shift+J'}
    menu = SimpleNamespace(items=[])
    def append(identifier, caption):
        item = SimpleNamespace(identifier=100 + len(menu.items), caption=caption)
        menu.items.append(item)
        return item
    menu.Append = append
    frame = SimpleNamespace(menu_bar=SimpleNamespace(GetMenuCount=lambda: 1,
        GetMenuLabel=lambda index: '&Tools', GetMenu=lambda index: menu), Bind=Mock())
    registered = []
    if installed:
        def register(spec, item):
            registry.register(spec)
            registry.validate(overrides)
            registered.append((spec, item))
        frame.keyboard_commands = SimpleNamespace(register_menu_command=register)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU=object()))
    open_dialog = Mock()
    monkeypatch.setattr(ui, 'show_media_conversion', open_dialog)
    actions = ui.install_media_actions(frame)
    assert ui.install_media_actions(frame) is actions
    rows = registered if installed else frame._pending_keyboard_commands
    assert len(rows) == len(menu.items) == 1
    assert rows[0][0].command_id == 'conversion.audio' and rows[0][0].default_shortcut == ''
    assert overrides == {'library.existing': 'Ctrl+Shift+J'}
    open_dialog.assert_not_called()
    actions.open()
    open_dialog.assert_called_once_with(frame)
