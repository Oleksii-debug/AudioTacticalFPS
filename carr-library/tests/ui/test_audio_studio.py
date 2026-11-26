"""Audio controller against the desktop's real worker protocol, without wx/NVDA.

The native view is a thread-checking double. Project and output files are real;
the speech engine is substituted so these tests require no installed voice.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.ui.audio_studio import (
    AudioDraft, AudioStudioActions, AudioStudioController, NativeAudioStudio,
    install_audio_studio_actions, progress_text,
)


class Control:
    def Enable(self, value=True):
        self.enabled = value

    def Disable(self):
        self.enabled = False


class Executor:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1)

    def submit(self, operation):
        self.future = self.pool.submit(operation)
        return self.future


class View:
    def __init__(self, draft):
        self.draft = draft
        self.main_thread = threading.get_ident()
        self.status, self.outputs, self.catalog = [], [], ()
        self.replace_answer = self.discard_answer = False
        self.replaced = self.discarded = 0
        self.busy = False

    def check_ui(self):
        assert threading.get_ident() == self.main_thread, 'A worker touched the native UI'

    def values(self):
        self.check_ui()
        return self.draft

    def set_draft(self, draft):
        self.check_ui()
        self.draft = draft

    def set_voices(self, voices, selected):
        self.check_ui()
        self.catalog = voices
        self.draft = replace(self.draft, voice_id=selected or (voices[0].id if voices else ''))

    def set_busy(self, busy):
        self.check_ui()
        self.busy = busy

    def report(self, text, *, focus=False):
        self.check_ui()
        self.status.append(text)

    def show_output(self, path):
        self.check_ui()
        self.outputs.append(path)

    def confirm_replace(self):
        self.check_ui()
        self.replaced += 1
        return self.replace_answer

    def confirm_discard(self):
        self.check_ui()
        self.discarded += 1
        return self.discard_answer


class LocalBackend:
    """Minimal local project backend; no executable is run by this substitute."""
    def __init__(self):
        self.calls, self.threads = [], []
        self.available = (
            SimpleNamespace(id='voice-uk', name='Olena', language='uk-UA', engine='sapi5'),
            SimpleNamespace(id='voice-en', name='Alex', language='en-GB', engine='sapi5'),
        )

    def record(self, name, *args):
        self.calls.append((name, args))
        self.threads.append(threading.get_ident())

    def voices(self, *, cancelled):
        self.record('voices')
        if cancelled():
            raise InterruptedError('Voice discovery cancelled')
        return self.available

    def plan(self, text, voice_id, *, title, rate, volume):
        self.record('plan', text, voice_id)
        if not text.strip():
            raise ValueError('Narration text is empty')
        return SimpleNamespace(source_text=text, title=title,
            roles=(SimpleNamespace(name='Narrator', voice_id=voice_id, rate=rate, volume=volume),))

    def save(self, plan, project):
        self.record('save', project)
        project.mkdir(exist_ok=True)
        result = project / 'narration.json'
        result.write_text(json.dumps({'text': plan.source_text, 'title': plan.title,
            'roles': [vars(role) for role in plan.roles]}, ensure_ascii=False), encoding='utf-8')
        return result

    def load(self, project):
        self.record('load', project)
        data = json.loads((project / 'narration.json').read_text(encoding='utf-8'))
        return SimpleNamespace(source_text=data['text'], title=data['title'],
                               roles=tuple(SimpleNamespace(**role) for role in data['roles']))

    def render(self, plan, project, *, ffmpeg, cancelled, progress):
        self.record('render', project, ffmpeg)
        self.save(plan, project)
        progress({'state': 'synthesizing', 'completed': 0, 'total': 1})
        if cancelled():
            raise InterruptedError('Speech cancelled')
        master = project / 'master.wav'
        with wave.open(str(master), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b'\x01\x00' * 800)
        manifest, captions = project / 'production.json', project / 'captions.srt'
        manifest.write_text('{"state":"complete"}', encoding='utf-8')
        captions.write_text('1\n00:00:00,000 --> 00:00:00,100\n' + plan.source_text, encoding='utf-8')
        progress({'state': 'complete', 'completed': 1, 'total': 1})
        return SimpleNamespace(master=master, manifest=manifest, captions=captions,
            project=project, duration_ms=100, generated_chunks=1, reused_chunks=0)


@pytest.fixture
def app(tmp_path):
    """Use DesktopFrame.run_job and its native callback ordering unchanged."""
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    desktop = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    methods = [node for node in desktop.body if isinstance(node, ast.FunctionDef)
               and node.name in {'run_job', '_finish_job_controls'}]
    callbacks = Queue()
    env = {'wx': SimpleNamespace(CallAfter=lambda fn, *args: callbacks.put((fn, args)))}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), 'exec'), env)
    frame = SimpleNamespace(working=False, busy=False, _closing=False,
        library=SimpleNamespace(root=tmp_path / 'private-library'), cancel_event=threading.Event(),
        executor=Executor(), book=None, active=None, cancel_button=Control(), choice=Control(),
        menu_bar=SimpleNamespace(GetMenuCount=lambda: 0), status=[])
    frame.library.root.mkdir()
    frame.report = frame.status.append
    frame.run_job = env['run_job'].__get__(frame)
    frame._finish_job_controls = env['_finish_job_controls'].__get__(frame)
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'fixture: never execute')
    view = View(AudioDraft(text='Тихий ранок.\nA quiet morning.', title='Chapter 1',
        ffmpeg=str(ffmpeg), project=str(tmp_path / 'narration')))
    backend = LocalBackend()
    controller = AudioStudioController(frame, view, voice_loader=backend.voices,
        planner=backend.plan, renderer=backend.render, saver=backend.save, loader=backend.load,
        post=lambda fn, *args: callbacks.put((fn, args)))

    def settle():
        while frame.working:
            fn, args = callbacks.get(timeout=5)
            fn(*args)
        while True:
            try:
                fn, args = callbacks.get_nowait()
            except Empty:
                break
            fn(*args)

    def discover():
        controller.reload_voices()
        settle()

    result = SimpleNamespace(frame=frame, view=view, backend=backend, controller=controller,
                             callbacks=callbacks, settle=settle, discover=discover, root=tmp_path)
    yield result
    frame.cancel_event.set()
    frame.executor.pool.shutdown(wait=True)


def test_voice_discovery_uses_worker_and_keeps_text_and_selected_identity(app):
    draft = replace(app.view.draft, voice_id='voice-en')
    app.view.draft = draft
    app.discover()
    assert app.view.draft == draft
    assert [voice.id for voice in app.view.catalog] == ['voice-uk', 'voice-en']
    assert all(thread != threading.get_ident() for thread in app.backend.threads)
    app.backend.available = tuple(reversed(app.backend.available))
    app.discover()
    assert app.view.draft.voice_id == 'voice-en'
    assert '2 installed local voices' in app.view.status[-1]


def test_no_voices_is_actionable_and_preserves_editable_draft(app):
    app.backend.available = ()
    draft = app.view.draft
    app.discover()
    assert app.view.draft == draft and not app.view.busy
    assert 'No enabled local SAPI 5 voices' in app.view.status[-1]
    app.controller.generate()
    assert 'Choose an available local voice' in app.view.status[-1]
    assert not any(name == 'render' for name, _ in app.backend.calls)


def test_discovery_failure_does_not_destroy_previous_voice_choice(app):
    app.discover()
    original = app.view.draft
    def broken(**kwargs):
        raise RuntimeError('The local speech service is unavailable')
    app.controller.voice_loader = broken
    app.discover()
    assert app.view.draft == original and len(app.controller.voices) == 2
    assert 'speech service is unavailable' in app.view.status[-1]
    assert not app.view.busy


def test_removed_voice_is_never_silently_replaced(app):
    app.discover()
    selected = app.view.draft.voice_id
    app.backend.available = tuple(voice for voice in app.backend.available if voice.id != selected)
    app.discover()
    app.controller.generate()
    assert app.view.draft.voice_id == selected
    assert not any(name == 'render' for name, _ in app.backend.calls)
    assert 'Choose an available local voice' in app.view.status[-1]


def test_save_and_open_roundtrip_preserves_source_and_narration_settings(app):
    app.discover()
    original = replace(app.view.draft, rate=-3, volume=77)
    app.view.draft = original
    app.controller.save()
    app.settle()
    assert not app.controller.dirty()
    assert 'Narration project saved:' in app.view.status[-1]
    saved = Path(original.project) / 'narration.json'
    assert json.loads(saved.read_text(encoding='utf-8'))['text'] == original.text
    app.view.draft = replace(original, text='temporary draft', rate=5)
    app.view.replace_answer = True
    app.controller.open_project(original.project)
    app.settle()
    assert app.view.draft == original
    assert not app.controller.dirty()
    assert all(thread != threading.get_ident() for thread in app.backend.threads)


def test_erasing_saved_text_remains_dirty_and_cancel_close_keeps_it(app):
    app.discover()
    app.controller.save()
    app.settle()
    app.view.draft = replace(app.view.draft, text='')
    assert app.controller.dirty()
    assert not app.controller.request_close()
    assert app.view.discarded == 1
    assert app.view.draft.text == ''


def test_save_rejects_empty_text_without_clobbering_saved_json(app):
    app.discover()
    app.controller.save()
    app.settle()
    saved = Path(app.view.draft.project) / 'narration.json'
    original = saved.read_bytes()
    app.view.draft = replace(app.view.draft, text='   ')
    app.controller.save()
    app.settle()
    assert saved.read_bytes() == original
    assert 'Narration text is empty' in app.view.status[-1]
    assert app.controller.dirty()


def test_invalid_save_result_is_never_reported_saved(app):
    app.discover()
    app.controller.saver = lambda plan, root: root / 'missing.json'
    app.controller.save()
    app.settle()
    assert app.controller.saved is None
    assert not any(text.startswith('Narration project saved:') for text in app.view.status)
    assert 'did not complete' in app.view.status[-1]


def test_failed_save_keeps_editor_and_restores_controls(app):
    app.discover()
    original = app.view.draft
    def denied(*args):
        raise PermissionError('Project folder is read-only')
    app.controller.saver = denied
    app.controller.save()
    app.settle()
    assert app.view.draft == original and not app.view.busy
    assert 'read-only' in app.view.status[-1]
    assert app.controller.saved is None


def test_generate_publishes_real_audio_caption_and_manifest_paths(app):
    app.discover()
    app.controller.generate()
    app.settle()
    assert app.view.outputs == [str(Path(app.view.draft.project) / 'master.wav')]
    with wave.open(app.view.outputs[0], 'rb') as output:
        assert output.getnframes() == 800 and output.getframerate() == 8000
    assert any('Generating speech: 0 of 1' in text for text in app.view.status)
    assert 'Duration: 0.10 seconds' in app.view.status[-1]
    assert 'Captions:' in app.view.status[-1] and 'Production record:' in app.view.status[-1]
    assert not app.view.busy and not app.controller.dirty()
    assert all(thread != threading.get_ident() for thread in app.backend.threads)


@pytest.mark.parametrize('bad_ffmpeg', ['', 'missing', 'other.exe'])
def test_missing_or_wrong_executable_never_calls_renderer(app, bad_ffmpeg):
    app.discover()
    if bad_ffmpeg == 'other.exe':
        (app.root / bad_ffmpeg).write_bytes(b'not ffmpeg')
    value = str(app.root / bad_ffmpeg) if bad_ffmpeg else ''
    app.view.draft = replace(app.view.draft, ffmpeg=value)
    draft = app.view.draft
    app.controller.generate()
    app.settle()
    assert app.view.draft == draft and not app.view.outputs
    assert not any(name == 'render' for name, _ in app.backend.calls)
    assert not Path(draft.project).exists()
    assert 'did not complete' in app.view.status[-1]


@pytest.mark.parametrize('kind', ['private', 'ancestor', 'relative', 'traversal', 'network', 'link'])
def test_project_containment_is_checked_before_any_project_write(app, kind):
    app.discover()
    value = {
        'private': str(app.frame.library.root / 'audio'), 'ancestor': str(app.root),
        'relative': 'audio', 'traversal': str(app.root / '..' / 'other'),
        'network': '//server/share/audio', 'link': str(app.root / 'link' / 'audio'),
    }[kind]
    if kind == 'link':
        (app.root / 'real').mkdir()
        (app.root / 'link').symlink_to(app.root / 'real', target_is_directory=True)
    app.view.draft = replace(app.view.draft, project=value)
    app.controller.save()
    app.settle()
    assert not any(name == 'save' for name, _ in app.backend.calls)
    assert 'did not complete' in app.view.status[-1]
    assert list(app.frame.library.root.iterdir()) == []


@pytest.mark.parametrize('bad_output', ['outside', 'missing', 'empty', 'symlink', 'wrong-project', 'zero-duration'])
def test_unverified_render_completion_never_surfaces_success(app, bad_output):
    app.discover()
    def render(*args, **kwargs):
        result = app.backend.render(*args, **kwargs)
        if bad_output == 'outside':
            outside = app.root / 'elsewhere.wav'
            outside.write_bytes(result.master.read_bytes())
            result.master = outside
        elif bad_output == 'missing':
            result.captions.unlink()
        elif bad_output == 'empty':
            result.manifest.write_bytes(b'')
        elif bad_output == 'symlink':
            link = result.project / 'link.wav'
            link.symlink_to(result.master)
            result.master = link
        elif bad_output == 'wrong-project':
            result.project = app.root
        else:
            result.duration_ms = 0
        return result
    app.controller.renderer = render
    app.controller.generate()
    app.settle()
    assert not app.view.outputs and app.controller.saved is None
    assert 'did not complete' in app.view.status[-1]
    assert not any(text.startswith('Audio saved:') for text in app.view.status)


def test_cancel_retains_draft_and_completed_chunk_then_allows_resume(app):
    app.discover()
    entered, proceed = threading.Event(), threading.Event()
    original = app.view.draft
    def interrupted(plan, project, *, cancelled, **kwargs):
        project.mkdir()
        (project / 'completed-chunk.wav').write_bytes(b'verified chunk fixture')
        entered.set()
        assert proceed.wait(3)
        if cancelled():
            raise InterruptedError('Narration cancelled')
        raise AssertionError('Cancellation was not delivered')
    app.controller.renderer = interrupted
    app.controller.generate()
    assert entered.wait(3)
    assert not app.controller.request_close()
    proceed.set()
    app.settle()
    assert app.view.draft == original and not app.view.busy
    assert (Path(original.project) / 'completed-chunk.wav').read_bytes() == b'verified chunk fixture'
    assert 'Narration cancelled' in app.view.status[-1]
    app.controller.renderer = app.backend.render
    app.controller.generate()
    app.settle()
    assert app.view.outputs and 'Audio saved:' in app.view.status[-1]


def test_progress_after_dispose_or_a_new_job_cannot_replace_status(app):
    app.discover()
    old = app.controller.epoch
    app.controller.generate()
    app.settle()
    before = list(app.view.status)
    app.controller._progress(old, 'late progress')
    app.controller.dispose()
    app.controller._progress(app.controller.epoch, 'disposed progress')
    assert app.view.status == before


def test_library_switch_suppresses_completion_without_overwriting_draft(app):
    app.discover()
    entered, proceed = threading.Event(), threading.Event()
    def render(*args, **kwargs):
        entered.set()
        assert proceed.wait(3)
        return app.backend.render(*args, **kwargs)
    app.controller.renderer = render
    app.controller.generate()
    assert entered.wait(3)
    app.frame.library = SimpleNamespace(root=app.root / 'different')
    proceed.set()
    app.settle()
    assert not app.view.outputs
    assert app.controller.saved is None
    assert not app.view.busy


def test_open_declined_does_not_read_or_replace_project(app):
    original = app.view.draft
    app.controller.open_project(str(app.root / 'unknown'))
    assert app.view.replaced == 1 and app.view.draft == original
    assert not app.backend.calls


def test_invalid_project_json_does_not_replace_existing_text(app):
    app.view.replace_answer = True
    project = app.root / 'bad-project'
    project.mkdir()
    (project / 'narration.json').write_text('{broken', encoding='utf-8')
    original = app.view.draft
    app.controller.open_project(str(project))
    app.settle()
    assert app.view.draft == original
    assert app.controller.saved is None
    assert 'did not complete' in app.view.status[-1]


def source_panel(app):
    source = app.root / 'original-source.txt'
    source.write_text('Exact excerpt, with punctuation.\n', encoding='utf-8')
    hit = SimpleNamespace(workspace='books', ref='segment-one', revision='sealed-revision', title='Source title')
    panel = SimpleNamespace(bound_library=app.frame.library, bound_workspace='books', bound_topic=None,
                            _context_epoch=1, selected=lambda: hit)
    calls = []
    def verified_copy(workspace, ref, revision, field):
        assert threading.get_ident() != app.view.main_thread
        calls.append((workspace, ref, revision, field))
        return source.read_text(encoding='utf-8')
    panel.verified_copy_service = verified_copy
    app.frame.book = SimpleNamespace(GetPage=lambda _: panel, Disable=lambda: None, Enable=lambda: None)
    app.frame.active = ('books', None)
    app.view.replace_answer = True
    return source, hit, panel, calls


def test_selected_excerpt_is_verified_in_its_scope_and_source_is_untouched(app):
    source, hit, panel, calls = source_panel(app)
    original = source.read_bytes()
    app.controller.use_source()
    app.settle()
    assert calls == [('books', 'segment-one', 'sealed-revision', 'quote')]
    assert app.view.draft.text == original.decode('utf-8')
    assert app.view.draft.title == 'Source title'
    app.view.draft = replace(app.view.draft, text='edited narration derivative')
    assert source.read_bytes() == original


def test_stale_topic_binding_does_not_read_a_different_context(app):
    source, hit, panel, calls = source_panel(app)
    panel.bound_topic = 'previous-topic'
    original = app.view.draft
    app.controller.use_source()
    assert calls == [] and app.view.draft == original
    assert 'Source text not loaded' in app.view.status[-1]


def test_changed_source_selection_during_verify_cannot_replace_editor(app):
    source, hit, panel, calls = source_panel(app)
    original = app.view.draft
    def changed(*args):
        panel.selected = lambda: SimpleNamespace(workspace='books', ref='other', revision='r2')
        return 'old selection'
    panel.verified_copy_service = changed
    app.controller.use_source()
    app.settle()
    assert app.view.draft == original
    assert 'source or draft changed' in app.view.status[-1]


def test_controller_does_not_start_another_job_while_the_host_is_busy(app):
    app.frame.working = True
    app.controller.reload_voices()
    app.controller.save()
    app.controller.generate()
    assert not app.backend.calls
    assert not app.view.busy and not app.controller.running
    assert 'Wait for the current operation' in app.view.status[-1]
    app.frame.working = False


def test_progress_never_echoes_source_text_or_arbitrary_tool_messages():
    assert progress_text({'state': 'synthesizing', 'completed': 2, 'total': 4,
                          'text': 'private text', 'message': '<script>'}) == 'Generating speech: 2 of 4.'
    assert 'private' not in progress_text({'state': 'private text'})
    assert 'True' not in progress_text({'completed': True, 'total': 1})


def test_dialog_reopen_retains_draft_without_writing_source_or_disk(app):
    seen = []
    def factory(frame, draft):
        seen.append(draft)
        return SimpleNamespace(show=lambda: None, last_draft=app.view.draft)
    actions = AudioStudioActions(app.frame, factory)
    actions.open()
    actions.open()
    assert seen == [None, app.view.draft]
    assert not Path(app.view.draft.project).exists()


def test_menu_installs_once_and_uses_no_global_ctrl_s_shortcut(app, monkeypatch):
    entries, menus, bindings = [], [], []
    class Menu:
        def Append(self, identity, label):
            entries.append(label)
            return label
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU='menu'))
    app.frame.Bind = lambda *args: bindings.append(args)
    app.frame.menu_bar = SimpleNamespace(Append=lambda menu, label: menus.append(label))
    first = install_audio_studio_actions(app.frame)
    assert install_audio_studio_actions(app.frame) is first
    assert menus == ['A&udio'] and entries == ['&Narration studio...']
    assert len(bindings) == 1
    assert not any('\t' in label for label in entries)


def test_native_voice_choice_keeps_identity_when_catalog_reorders():
    class Choice:
        def SetItems(self, items): self.items = items
        def SetSelection(self, selected): self.selected = selected
    view = object.__new__(NativeAudioStudio)
    view.voice = Choice()
    voices = LocalBackend().available
    view.set_voices(voices, 'voice-en')
    assert view.voice.selected == 1
    view.set_voices(tuple(reversed(voices)), 'voice-en')
    assert view.voice.selected == 0
    view.set_voices((voices[0],), 'voice-en')
    assert view.voice.selected == -1 and view.requested_voice == 'voice-en'
