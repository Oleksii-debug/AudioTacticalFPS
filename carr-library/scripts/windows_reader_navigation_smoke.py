"""Actual Windows scoped speech over the existing reader, worker and player.

Only an explicitly synthetic local provider writes silent PCM. This proves
source span dispatch, native controls and lifecycle, not speech/model quality.
No default audio route is permitted; the expected endpoint must be supplied.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import traceback
import wave


def checkpoint(path, report, phase):
    report['phase'] = phase
    raw = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
    if len(raw) > 64 * 1024:
        raise RuntimeError('Native navigation receipt exceeds its bound.')
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + '.partial')
    with staged.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staged, path)
    print('READER_NAVIGATION_PHASE ' + phase, flush=True)


def module_evidence(checkout, source, package, modules):
    actual = Path(package.__file__).resolve(strict=True)
    expected = checkout / 'src' / 'carr_library'
    package_root = actual.parent
    distribution = None
    if source is not None:
        if source != checkout or package_root != expected.resolve(strict=True):
            raise RuntimeError('Navigation source override differs from the exact checkout.')
    else:
        if actual.is_relative_to(checkout):
            raise RuntimeError('Installed navigation imported the source checkout.')
        owners = []
        for candidate in importlib.metadata.distributions(name='universal-research-library'):
            for member in candidate.files or ():
                if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                    continue
                try:
                    path = Path(candidate.locate_file(member)).resolve(strict=True)
                except FileNotFoundError:
                    continue
                if path == actual:
                    owners.append(candidate)
        if len(owners) != 1:
            raise RuntimeError('Exactly one installed wheel RECORD must own the imported package.')
        distribution = owners[0]
    def digest(path):
        with path.open('rb') as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise RuntimeError('Navigation source proof exceeds its per-module bound.')
        return sha256(data).hexdigest()
    result = {}
    for name in modules:
        module = importlib.import_module(name)
        relative = Path(*name.split('.')[1:]).with_suffix('.py')
        loaded = Path(module.__file__).resolve(strict=True)
        if loaded != (package_root / relative).resolve(strict=True):
            raise RuntimeError('Navigation module escaped its actual package root.')
        if distribution is not None:
            member_name = 'carr_library/' + relative.as_posix()
            recorded = [Path(distribution.locate_file(member)).resolve(strict=True)
                        for member in distribution.files or ()
                        if str(member).replace('\\', '/') == member_name]
            if recorded != [loaded]:
                raise RuntimeError('Navigation module is absent from the actual wheel RECORD.')
        checksum = digest(loaded)
        if checksum != digest(expected / relative):
            raise RuntimeError('Navigation module differs from the exact checkout bytes.')
        result[name] = {'path': str(loaded), 'sha256': checksum}
    return result


def run_native(options, report):
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.keybindings import KeybindingStore, validate_bindings
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.research.topic_tree import TopicTree
    from carr_library.speech import Voice, VoiceRegistry
    from carr_library.speech._io import check_cancelled
    from carr_library.speech.output_devices import output_devices
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.speech.source import read_source_range
    from carr_library import runtime_settings
    from carr_library.ui import reader_navigation, speech_dialog
    from carr_library.ui.productivity import KeyboardActions, collect_commands

    checkout = Path(__file__).resolve(strict=True).parents[1]
    report['modules'] = module_evidence(checkout, options.source, carr_library, (
        'carr_library.speech.navigation', 'carr_library.ui.reader_navigation',
        'carr_library.ui.reading_tools', 'carr_library.speech.reading_tools',
        'carr_library.ui.speech_dialog', 'carr_library.ui.keyboard_settings',
        'carr_library.keybindings', 'carr_library.speech.runtime', 'carr_library.speech.player',
        'carr_library.speech.synthesis_admission'))
    report['wx'] = wx.version()
    devices = output_devices()
    selected = next((device for device in devices
                     if device.identity == options.expected_device_identity), None)
    if selected is None:
        raise RuntimeError('The explicitly expected audio endpoint is unavailable; no default route is allowed.')
    report['output_identity'] = selected.identity
    checks = report['checks']
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        checkpoint(options.output, report, label)

    gui = wx.App(False)
    gui.SetExitOnFrameDelete(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    simulator = wx.UIActionSimulator()
    main_thread = threading.get_ident()
    errors, calls, messages = [], [], []
    original_registry = runtime_settings.voice_registry
    original_locate = reader_navigation.unit_range

    def pump(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            gui.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def key(code, modifiers=wx.MOD_NONE):
        if not simulator.Char(code, modifiers):
            raise RuntimeError('Native keyboard dispatch is unavailable.')
        gui.Yield()

    def press(control):
        if not control.IsEnabled():
            raise AssertionError('Native control is disabled: ' + control.GetName())
        top = wx.GetTopLevelParent(control)
        top.Raise()
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() == control, timeout=3):
            raise AssertionError('Native control did not receive focus: ' + control.GetName())
        key(wx.WXK_SPACE)

    def find(reader, name):
        control = wx.Window.FindWindowByName(name, reader)
        if control is None:
            raise AssertionError('Missing actual native reader control: ' + name)
        return control

    class SyntheticProvider:
        name = 'Purpose-written scoped-speech PCM fixture'
        def voices(self):
            return (Voice('fixture:scoped-pcm', self.name, 'synthetic-protocol', 'uk'),)
        def fingerprint(self, voice_id):
            if voice_id != 'fixture:scoped-pcm':
                raise ValueError('Unknown synthetic fixture voice.')
            return sha256(Path(__file__).read_bytes()).hexdigest()
        def render(self, text, voice, destination, settings, cancelled):
            if threading.get_ident() == main_thread:
                raise AssertionError('Narration ran on the GUI thread.')
            check_cancelled(cancelled)
            calls.append({'text': text, 'voice': voice.id, 'worker': threading.get_ident()})
            with wave.open(str(destination), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(b'\0\0' * 32000)
            check_cancelled(cancelled)
        def close(self):
            pass

    with TemporaryDirectory(prefix='carr-native-reading-units-') as temporary:
        root = Path(temporary)
        raw = '😀 Ім’я читача. Друге речення!\nДругий рядок.\n\nОстанній абзац.'
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native scoped speech')
        topic = library.create_topic(workspace, 'Immutable evidence')
        imported = library.add_text(workspace, 'Purpose-written source', raw)
        hit = library.search(workspace, 'читача')[0]
        library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original quotation retained.')
        evidence = library.evidence(workspace, topic)
        retained = library.assets.resolve(imported['asset'])
        before_source = retained.read_bytes()
        desk = ReadingDesk(library, workspace, imported['edition'], topic=topic)
        frame = wx.Frame(None, title='Native reading-unit fixture', size=(1050, 900))
        frame.library, frame.report = library, messages.append
        bar, menu = wx.MenuBar(), wx.Menu()
        menu.Append(wx.ID_ANY, 'Fixture open\tCtrl+O')
        bar.Append(menu, '&Fixture')
        frame.SetMenuBar(bar)
        frame.menu_bar = bar
        frame.keyboard_actions = KeyboardActions(frame, collect_commands(bar))
        frame.keyboard_actions.reload()
        frame.Show()
        frame.Raise()
        allowed = [True]
        active = {}
        slot = 'native-reading-units'
        runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((SyntheticProvider(),))

        def guard():
            if not allowed[0] or frame.library is not library:
                raise ValueError('The native fixture source scope was revoked.')
            with library.read(workspace) as database:
                TopicTree(database, workspace)._visible(topic)

        def focus_editor(reader):
            editor = reader.reader_editor
            reader.Raise()
            editor.SetFocus()
            if not pump(lambda: wx.Window.FindFocus() == editor, timeout=3):
                raise AssertionError('The native reading editor did not receive focus.')
            return editor

        def stop(reader):
            focus_editor(reader)
            key(wx.WXK_F8)
            check('explicit Stop leaves the same player silent', pump(
                lambda: reader.reader_player.state == 'stopped' and not reader.reader_edit_host.busy()))

        def blocked_navigation(reader, mutation):
            host, navigation = reader.reader_edit_host, reader.reader_navigation
            entered, release = threading.Event(), threading.Event()
            prior = len(calls)
            before = host.capture().document.to_dict()
            host.select(raw.index('Ім’я') + 1, raw.index('Ім’я') + 1)
            def waiting(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Native fixture release deadline expired.')
                return original_locate(*args, **kwargs)
            reader_navigation.unit_range = waiting
            try:
                press(reader.reader_unit_buttons['word'])
                check('native unit location enters the existing reader worker', pump(entered.is_set))
                if mutation == 'cancel':
                    focus_editor(reader)
                    key(wx.WXK_F8)
                elif mutation == 'cursor':
                    host.select(len(raw), len(raw))
                elif mutation == 'scope':
                    allowed[0] = False
                else:
                    raise AssertionError('Unknown explicit native fixture mutation.')
                release.set()
                check('stale native unit worker completes without another narration',
                      pump(lambda: not host.busy()) and len(calls) == prior)
                if mutation == 'scope':
                    allowed[0] = True
                check('stale result leaves the original reading document unchanged',
                      host.capture().document.to_dict() == before)
            finally:
                allowed[0] = True
                release.set()
                reader_navigation.unit_range = original_locate

        def scenario(reader):
            host, player = reader.reader_edit_host, reader.reader_player
            check('retained source and local voice load before scoped actions', pump(
                lambda: not host.busy() and reader.reader_unit_buttons['word'].IsEnabled(), timeout=30))
            editor = focus_editor(reader)
            player.select_output(selected)
            check('same player uses only the explicitly supplied endpoint',
                  player.output_device is not None and player.output_device.identity == selected.identity)
            snapshot = host.capture()
            check('native source copy retains exact text and passive source lineage',
                  snapshot.text == raw and snapshot.document.source_text == raw
                  and snapshot.document.source_anchor['quotation_authority'] is False)
            original_document = snapshot.document.to_dict()
            check('opening a source reader never narrates automatically', not calls and player.state == 'stopped')
            position = raw.index('Ім’я') + 1
            expected = {
                'word': 'Ім’я', 'line': '😀 Ім’я читача. Друге речення!',
                'sentence': '😀 Ім’я читача.',
                'paragraph': '😀 Ім’я читача. Друге речення!\nДругий рядок.',
            }
            for unit, wanted in expected.items():
                host.select(position, position)
                prior = len(calls)
                press(reader.reader_unit_buttons[unit])
                check('actual Read current ' + unit + ' narrates only its exact retained span',
                      pump(lambda: len(calls) == prior + 1 and not host.busy() and player.state == 'playing')
                      and calls[-1]['text'] == wanted and editor.GetStringSelection() == wanted
                      and all(row['worker'] != main_thread for row in calls))
                check('current ' + unit + ' leaves source and reading history unchanged',
                      host.capture().document.to_dict() == original_document)
                stop(reader)

            host.select(position, position)
            prior = len(calls)
            press(find(reader, 'Select current unit'))
            check('passive current-word selection after a non-BMP character starts no audio',
                  pump(lambda: not host.busy()) and editor.GetStringSelection() == 'Ім’я'
                  and len(calls) == prior and player.state == 'stopped')
            press(find(reader, 'Next unit'))
            check('native next-unit action selects the next exact word without speech',
                  pump(lambda: not host.busy()) and editor.GetStringSelection() == 'читача'
                  and len(calls) == prior)
            press(find(reader, 'Previous unit'))
            check('native previous-unit action returns without wrapping or speech',
                  pump(lambda: not host.busy()) and editor.GetStringSelection() == 'Ім’я'
                  and len(calls) == prior)

            saved = KeybindingStore(library.root).load()
            frame.keyboard_actions.save({**saved, 'voice_reader.read_word': 'Ctrl+Shift+W'})
            key_bytes = KeybindingStore(library.root).path.read_bytes()
            try:
                frame.keyboard_actions.save({**saved, 'voice_reader.read_word': 'F8'})
            except ValueError:
                pass
            else:
                raise AssertionError('A scoped-read shortcut replaced Stop.')
            check('shared keymap keeps Stop and saved assignments on collision',
                  KeybindingStore(library.root).path.read_bytes() == key_bytes)
            host.select(position, position)
            focus_editor(reader)
            prior = len(calls)
            key(ord('W'), wx.MOD_CONTROL | wx.MOD_SHIFT)
            check('custom native key invokes the same scoped speech command',
                  pump(lambda: len(calls) == prior + 1 and not host.busy() and player.state == 'playing')
                  and calls[-1]['text'] == 'Ім’я')
            stop(reader)
            for mutation in ('cancel', 'cursor', 'scope'):
                blocked_navigation(reader, mutation)
            focus_editor(reader)
            key(ord('S'), wx.MOD_CONTROL)
            recovered = ReaderDraftStore(library.root, slot=slot).load()
            check('existing Save persists reading cursor and exact immutable source',
                  recovered is not None and recovered.document.text == raw
                  and recovered.document.source_text == raw
                  and recovered.document.source_anchor == snapshot.document.source_anchor)
            active['reader'], active['player'], active['navigation'] = reader, player, reader.reader_navigation
            active['host'] = host
            before = len(calls)
            entered, release = threading.Event(), threading.Event()
            def waiting(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Native close fixture release expired.')
                return original_locate(*args, **kwargs)
            reader_navigation.unit_range = waiting
            try:
                host.select(position, position)
                press(reader.reader_unit_buttons['word'])
                check('close fixture owns an actual reader-worker request', pump(entered.is_set))
                reader.Close()
                check('Close cancels this request while retaining the active modal until completion',
                      host.busy() and host.cancelled() and reader.IsModal())
                release.set()
                # EndModal hides the dialog; IsModal stays true until this
                # callback returns and the surrounding ShowModal unwinds.
                check('Close never begins the cancelled scoped narration',
                      pump(lambda: not reader.IsShown() and not host.busy()) and len(calls) == before)
            finally:
                release.set()
                reader_navigation.unit_range = original_locate

        deadline = time.monotonic() + 12
        def drive():
            candidates = [window for window in wx.GetTopLevelWindows()
                          if bool(window) and hasattr(window, 'reader_navigation')
                          and window.IsModal() and window.IsShown()
                          and window.GetParent() is frame and window.reader_edit_host.alive()]
            if not candidates:
                if time.monotonic() < deadline:
                    wx.CallLater(20, drive)
                    return
                errors.append(TimeoutError('Actual scoped Voice reader did not open.'))
                return
            reader = candidates[0]
            try:
                scenario(reader)
            except BaseException as exc:
                errors.append(exc)
                traceback.print_exc()
                if bool(reader) and reader.IsModal():
                    reader.EndModal(wx.ID_CANCEL)

        try:
            wx.CallLater(20, drive)
            speech_dialog.show_speech_reader(frame, source_guard=guard,
                recovery_slot=slot, source_loader=lambda cancelled:
                read_source_range(desk, 1, 1, guard=guard, cancelled=cancelled))
            if errors:
                raise errors[0]
            gui.Yield()
            check('the returned modal has revoked its reader host after worker completion',
                  not active['host'].alive() and not active['host'].busy()
                  and active['host'].cancelled())
            check('reader shutdown leaves no retained playback or listening owner',
                  active['player'].state == 'stopped'
                  and not getattr(frame, '_reader_playback_retained', ())
                  and not getattr(frame, '_reader_listening_retained', ()))
            check('scoped speech preserves exact original bytes and saved quotation records',
                  retained.read_bytes() == before_source
                  and library.evidence(workspace, topic) == evidence)
            check('existing shared keyboard adapters detach when the reader closes',
                  not frame.keyboard_actions._reader_adapters)
            report['scope'] = 'Actual native controls, exact source spans, one reader worker/player, synthetic silent PCM'
            report['synthetic_narrations'] = len(calls)
            report['speech_quality'] = report['manual_nvda'] = 'NOT_RUN'
        finally:
            reader_navigation.unit_range = original_locate
            runtime_settings.voice_registry = original_registry
            frame.Destroy()
            gui.Yield()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--expected-device-identity', required=True)
    options = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'starting', 'checks': [],
              'python': sys.version, 'source_commit': os.environ.get('GITHUB_SHA'),
              'speech_quality': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}
    checkpoint(options.output, report, 'starting')
    faulthandler.enable()
    faulthandler.dump_traceback_later(150, exit=True)
    runtime = None
    try:
        if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
            raise RuntimeError('Run the actual Windows navigation probe with Python -I -B.')
        if (len(options.expected_device_identity) != 64
                or any(character not in '0123456789abcdef'
                       for character in options.expected_device_identity)):
            raise ValueError('Supply one exact verified Windows output identity.')
        checkout = Path(__file__).resolve(strict=True).parents[1]
        if options.source is not None:
            if options.expect_installed:
                raise ValueError('Source and installed qualification are mutually exclusive.')
            options.source = options.source.resolve(strict=True)
            if options.source != checkout:
                raise ValueError('Source override must name this exact checkout.')
            sys.path.insert(0, str(options.source / 'src'))
        elif not options.expect_installed:
            raise ValueError('Choose an explicit source or installed qualification.')
        from carr_library.native_runtime import activate_bundled_runtime
        runtime = activate_bundled_runtime()
        run_native(options, report)
        report['status'] = 'PASS'
        checkpoint(options.output, report, 'complete')
        return 0
    except BaseException as exc:
        report['status'] = 'FAIL'
        report['failure'] = {'type': type(exc).__name__, 'message': str(exc)[:2000]}
        checkpoint(options.output, report, 'failed')
        raise
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
