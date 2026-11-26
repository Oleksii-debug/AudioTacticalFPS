"""Real audio event-handler closures with native-control doubles, not GUI proof.

The harness compiles the production handlers unchanged. Actual reading/project
models and temporary recovery files verify save claims and data retention.
"""
import ast
from concurrent.futures import Future
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.speech import ReadingDocument, Voice
from carr_library.speech.session import ReaderDraftStore
from carr_library.ui import speech_dialog, studio_dialog


class Field:
    def __init__(self, value='', selection=0):
        self.value, self.selection = value, selection
        self.enabled, self.items = True, []
        self.focused = False

    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def SetValue(self, value): self.value = value
    def GetSelection(self): return self.selection
    def SetSelection(self, selected): self.selection = selected
    def Set(self, items): self.items = list(items)
    def SetItems(self, items): self.items = list(items)
    def GetInsertionPoint(self): return 0
    def SetInsertionPoint(self, value): self.cursor = value
    def Enable(self, enabled=True): self.enabled = enabled
    def Disable(self): self.enabled = False
    def SetFocus(self): self.focused = True
    def GetStringSelection(self): return ''


def bind_handlers(module, outer_name, names, state):
    tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    outer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == outer_name)
    nodes = [node for node in outer.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    assignments = ast.parse('\n'.join(f'{name} = values[{name!r}]' for name in state)).body
    deactivate = ast.parse('def deactivate():\n    nonlocal alive\n    alive = False\n').body
    returned = ast.parse('result = {' + ', '.join(f'{name!r}: {name}' for name in names) + '}\n'
        + "result['snapshot'] = lambda: {" + ', '.join(f'{name!r}: {name}' for name in state) + '}\n'
        + "result['deactivate'] = deactivate\n"
        + 'return result').body
    factory = ast.FunctionDef(name='factory', args=ast.arguments(posonlyargs=[],
        args=[ast.arg(arg='values')], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=assignments + nodes + deactivate + returned, decorator_list=[])
    env = dict(module.__dict__)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 module.__file__, 'exec'), env)
    return SimpleNamespace(**env['factory'](state))


def reader_state(tmp_path):
    messages, queued, ended = [], [], []
    wx = SimpleNamespace(NOT_FOUND=-1, YES=1, NO=2, CANCEL=4, YES_NO=8,
        ICON_QUESTION=16, ID_CANCEL=17, MessageBox=lambda *args: 4,
        CallAfter=lambda fn, *args: queued.append((fn, args)))
    document = ReadingDocument('Original source.', title='Source')
    document.set_text('Edited reading copy.')
    state = dict(alive=True, busy=False, operation_epoch=0, pending_future=None,
        document=document, source_loaded=True, last_recovery_signature=None,
        draft_store=ReaderDraftStore(tmp_path), recovered=None, requested_voice_id='voice-b',
        save_path=None, last_audio=None, editor=Field(document.text), source_status=Field(),
        voice_choice=Field(selection=1), rate=Field(-2), volume=Field(63),
        voices=(Voice('voice-a', 'Voice A', 'local test', 'en'),
                Voice('voice-b', 'Voice B', 'local test', 'uk')),
        registry=None, player=SimpleNamespace(position_seconds=0, stop=lambda: None),
        source_loader=None, source_guard=lambda: None, recovery_notice='',
        parent=SimpleNamespace(library=SimpleNamespace(root=tmp_path)),
        dialog=SimpleNamespace(EndModal=ended.append), wx=wx,
        report=lambda message, *args, **kwargs: messages.append(message),
        cancel=threading.Event(), reload_button=Field(), capabilities=lambda: None)
    return state, messages, queued, ended


def test_reader_ctrl_s_during_source_load_cannot_claim_a_save(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    state['source_loaded'] = False
    handlers = bind_handlers(speech_dialog, 'show_speech_reader',
        ['sync_document', 'save_recovery', 'save_project'], state)
    assert handlers.save_project() is False
    assert state['document'].dirty and not state['draft_store'].path.exists()
    assert 'still loading' in messages[-1]
    assert not any('saved for recovery' in text for text in messages)


def test_reader_save_success_requires_real_recovery_commit(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    handlers = bind_handlers(speech_dialog, 'show_speech_reader',
        ['sync_document', 'save_recovery', 'save_project'], state)
    assert handlers.save_project() is True
    saved = state['draft_store'].load()
    assert saved.document.text == 'Edited reading copy.'
    assert saved.document.source_text == 'Original source.'
    assert (saved.voice_id, saved.rate, saved.volume) == ('voice-b', -2, 63)
    assert not state['document'].dirty
    assert 'saved for recovery' in messages[-1]


def test_reader_failed_recovery_write_retains_dirty_document(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    def full_disk(value):
        raise OSError('No space left')
    state['draft_store'] = SimpleNamespace(save=full_disk)
    handlers = bind_handlers(speech_dialog, 'show_speech_reader',
        ['sync_document', 'save_recovery', 'save_project'], state)
    assert handlers.save_project() is False
    assert state['document'].dirty and state['document'].text == state['editor'].value
    assert 'not saved' in messages[-1] and 'No space left' in messages[-1]


def test_reader_false_recovery_result_does_not_mark_document_saved(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    state['save_recovery'] = lambda **kwargs: False
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['sync_document', 'save_project'], state)
    assert handlers.save_project() is False
    assert state['document'].dirty
    assert 'not committed' in messages[-1]


def test_reader_empty_edit_still_prompts_and_cancel_retains_undo_history(tmp_path):
    state, messages, _, ended = reader_state(tmp_path)
    state['editor'].value = ''
    state['save_project'] = lambda: pytest.fail('Cancel must not save')
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['sync_document', 'closing'], state)
    handlers.closing(None)
    assert not ended and state['document'].dirty
    assert state['document'].text == ''
    assert state['document'].undo() and state['document'].text == 'Edited reading copy.'


@pytest.mark.parametrize('initial, final', [
    ('', 'A new draft typed before the source returned.'),
    ('An unsaved draft from a failed source load.', 'An unsaved draft from a failed source load.'),
])
def test_source_completion_preserves_an_edit_made_while_loading(tmp_path, initial, final):
    state, messages, _, _ = reader_state(tmp_path)
    state['source_loaded'] = False
    state['editor'].value = initial
    captured = []
    state['run'] = lambda work, done: captured.append((work, done))
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['reload'], state)
    handlers.reload()
    state['editor'].value = final
    source = SimpleNamespace(text='Authenticated selected source.', title='Selected chapter',
                             positions=(1,), warnings=())
    registry = SimpleNamespace(voices=lambda: state['voices'])
    captured[0][1]((registry, source))
    saved = handlers.snapshot()
    assert saved['source_loaded']
    assert saved['document'].source_text == source.text
    assert saved['document'].text == state['editor'].value
    assert saved['document'].dirty


def test_source_guard_failure_cannot_replace_voice_registry_or_editor(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    captured = []
    state['run'] = lambda work, done: captured.append(done)
    def revoked(): raise ValueError('Source scope changed')
    state['source_guard'] = revoked
    old_registry = state['registry'] = object()
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['reload'], state)
    handlers.reload()
    candidate = SimpleNamespace(voices=lambda: (SimpleNamespace(id='different'),))
    with pytest.raises(ValueError, match='scope changed'):
        captured[0]((candidate, SimpleNamespace(text='stale')))
    assert handlers.snapshot()['registry'] is old_registry
    assert handlers.snapshot()['voices'] == state['voices']
    assert state['editor'].value == 'Edited reading copy.'


def test_reader_reload_preserves_voice_identity_and_missing_voice_never_chooses_first(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    captured = []
    state['run'] = lambda work, done: captured.append(done)
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['reload'], state)
    handlers.reload()
    captured[-1]((SimpleNamespace(voices=lambda: tuple(reversed(state['voices']))), None))
    assert state['voice_choice'].selection == 0
    handlers.reload()
    captured[-1]((SimpleNamespace(voices=lambda: (state['voices'][0],)), None))
    assert state['voice_choice'].selection == -1
    handlers.reload()
    captured[-1]((SimpleNamespace(voices=lambda: (state['voices'][0],)), None))
    assert state['voice_choice'].selection == -1
    assert 'previously selected voice is unavailable' in messages[-1]


def test_reader_keyboard_read_cannot_select_last_voice_from_missing_selection(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    state['registry'] = object()
    state['voice_choice'].selection = -1
    state['selected_text'] = lambda *args: pytest.fail('Missing voice must reject before text/render work')
    state['run'] = lambda *args: pytest.fail('No render should start')
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['speak'], state)
    handlers.speak()
    assert 'Choose an installed voice explicitly' in messages[-1]


def test_reader_submit_failure_restores_idle_and_keeps_draft(tmp_path):
    state, messages, _, _ = reader_state(tmp_path)
    def reject(work): raise RuntimeError('Executor closed')
    state['executor'] = SimpleNamespace(submit=reject)
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['run'], state)
    handlers.run(lambda: None, lambda value: pytest.fail('No completion'))
    assert not handlers.snapshot()['busy'] and state['reload_button'].enabled
    assert 'could not start' in messages[-1]


def test_reader_cancelled_completion_does_not_autoplay_or_replace_draft(tmp_path):
    state, messages, queued, _ = reader_state(tmp_path)
    future = Future()
    state['executor'] = SimpleNamespace(submit=lambda work: future)
    completed = []
    handlers = bind_handlers(speech_dialog, 'show_speech_reader', ['run'], state)
    handlers.run(lambda: None, completed.append)
    state['cancel'].set()
    future.set_result('published but cancelled')
    fn, args = queued.pop()
    fn(*args)
    assert not completed and not handlers.snapshot()['busy']
    assert 'cancelled' in messages[-1]


def studio_state(tmp_path):
    messages, queued, ended = [], [], []
    controller = studio_dialog.StudioController(SimpleNamespace(root=tmp_path))
    wx = SimpleNamespace(YES=1, NO=2, CANCEL=4, YES_NO=8, ICON_QUESTION=16, NOT_FOUND=-1,
        ID_CLOSE=21, MessageBox=lambda *args: 4,
        CallAfter=lambda fn, *args: queued.append((fn, args)))
    state = dict(alive=True, busy=False, operation_epoch=0, draft_dirty=True,
        last_autosave_signature=None, controller=controller, service=controller.service,
        editor=Field('An edited production.'), title=Field('Production'), voice_choice=Field(),
        voices=(SimpleNamespace(id='voice-a'),), format_choice=Field(selection=1), stems=Field(False),
        formats=list(studio_dialog.AUDIO_FORMATS), current_job=None, wx=wx,
        report=lambda message, *args, **kwargs: messages.append(message),
        cancel=threading.Event(), controls=lambda: None,
        dialog=SimpleNamespace(EndModal=ended.append),
        timer=SimpleNamespace(Stop=lambda: None),
        executor=SimpleNamespace(shutdown=lambda **kwargs: None))
    return state, messages, queued, ended


def test_studio_autosave_keeps_draft_dirty_until_explicit_project_save(tmp_path):
    state, messages, _, _ = studio_state(tmp_path)
    handlers = bind_handlers(studio_dialog, 'show_studio',
        ['selected_voices', 'draft_signature', 'autosave', 'confirm_replace', 'save'], state)
    assert handlers.autosave() is True
    assert state['controller'].draft.exists()
    assert handlers.snapshot()['draft_dirty']
    assert handlers.confirm_replace() is False
    assert handlers.save() is not None
    assert not handlers.snapshot()['draft_dirty']
    assert handlers.confirm_replace() is True


def test_studio_close_failure_preserves_window_editor_and_dirty_state(tmp_path):
    state, messages, _, ended = studio_state(tmp_path)
    def full_disk(*args, **kwargs): raise OSError('No space left')
    state['controller'].autosave = full_disk
    handlers = bind_handlers(studio_dialog, 'show_studio',
        ['selected_voices', 'draft_signature', 'autosave', 'close'], state)
    assert handlers.close() is False
    assert not ended and handlers.snapshot()['alive'] and handlers.snapshot()['draft_dirty']
    assert state['editor'].value == 'An edited production.'
    assert 'window remains open' in messages[-1]


def test_studio_autosaved_invalid_draft_can_close_without_claiming_valid_project(tmp_path):
    state, messages, _, ended = studio_state(tmp_path)
    state['editor'].value = '[Invalid direction: keep this draft]'
    handlers = bind_handlers(studio_dialog, 'show_studio',
        ['selected_voices', 'draft_signature', 'autosave', 'close'], state)
    assert handlers.close() is True
    assert ended == [state['wx'].ID_CLOSE]
    assert not handlers.snapshot()['alive']
    assert state['controller'].project is None
    assert state['controller'].draft.exists()


def test_studio_submit_failure_restores_idle_without_success(tmp_path):
    state, messages, _, _ = studio_state(tmp_path)
    def reject(work): raise RuntimeError('Executor closed')
    state['executor'] = SimpleNamespace(submit=reject)
    handlers = bind_handlers(studio_dialog, 'show_studio', ['run'], state)
    handlers.run(lambda: None, lambda value: pytest.fail('No completion'))
    assert not handlers.snapshot()['busy']
    assert 'could not start' in messages[-1]


def test_studio_progress_and_cancelled_completion_cannot_announce_success(tmp_path):
    state, messages, queued, _ = studio_state(tmp_path)
    future, operations, completed = Future(), [], []
    state['executor'] = SimpleNamespace(submit=lambda work: operations.append(work) or future)
    handlers = bind_handlers(studio_dialog, 'show_studio', ['run'], state)
    progress_holder = []
    handlers.run(lambda progress: progress_holder.append(progress), completed.append, with_progress=True)
    operations[0]()
    progress_holder[0]('Generating one chunk')
    state['cancel'].set()
    future.set_result('complete')
    while queued:
        fn, args = queued.pop(0)
        fn(*args)
    assert not completed and 'cancelled' in messages[-1]
    before = list(messages)
    progress_holder[0]('Late completed event')
    fn, args = queued.pop()
    fn(*args)
    assert messages == before


def test_studio_keyboard_save_during_render_does_not_mutate_project(tmp_path):
    state, messages, _, _ = studio_state(tmp_path)
    state['busy'] = True
    handlers = bind_handlers(studio_dialog, 'show_studio',
        ['selected_voices', 'draft_signature', 'save'], state)
    assert handlers.save() is None
    assert not state['controller'].draft.exists()
    assert 'current operation' in messages[-1]


@pytest.mark.parametrize('kind', ['reader', 'studio'])
def test_completed_worker_does_not_queue_gui_events_after_modal_disposal(tmp_path, kind):
    if kind == 'reader':
        state, messages, queued, _ = reader_state(tmp_path)
        module, outer = speech_dialog, 'show_speech_reader'
    else:
        state, messages, queued, _ = studio_state(tmp_path)
        module, outer = studio_dialog, 'show_studio'
    future = Future()
    state['executor'] = SimpleNamespace(submit=lambda work: future)
    handlers = bind_handlers(module, outer, ['run'], state)
    handlers.run(lambda: None, lambda value: pytest.fail('A closed window cannot receive a completion'))
    handlers.deactivate()
    future.set_result('already finished')
    assert queued == []
