"""Actual installed native review and passive canonical reader-session recovery."""
from argparse import ArgumentParser
from collections import Counter
from contextlib import ExitStack
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback

MODULES = (
    'carr_library.reader.legacy_reading', 'carr_library.reader.narration',
    'carr_library.ui.reading_import', 'carr_library.library_backup',
    'carr_library.reader.reading_desk', 'carr_library.audio.jobs',
)


def identity():
    if not sys.flags.isolated:
        raise ValueError('Use the isolated installed interpreter.')
    prefix, result = Path(sys.prefix).resolve(strict=True), {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('Historical reader implementation escaped the installed wheel.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def run_native():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup, verify_backup
    from carr_library.reader import legacy_reading as legacy
    from carr_library.reader.narration import NarrationStore
    from carr_library.ui import reading_import

    checks, errors, timers = [], [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    application = App(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    frame = wx.Frame(None, title='Historical reader-session recovery qualification')
    frame.Show()
    application.SetTopWindow(frame)
    try:
        with TemporaryDirectory(prefix='carr-reader-session-') as folder:
            root = Path(folder)
            app = LibraryApp(root / 'library')
            app.create_workspace('Synthetic retained reader session')
            source = 'Київ — pôvodný text.'
            document = dict(schema='carr.reading-copy.v1', title='Історія — čítanie',
                source_text=source, source_sha256=sha256(source.encode()).hexdigest(),
                edits=[dict(start=0, end=4, replacement='Львів', label='Історична правка'),
                       dict(start=0, end=5, replacement='Київ', label='Збережене повернення')], revision=1)
            envelope = dict(schema='carr.reader-session.v1', document=document,
                voice_id='sapi5-32:old-unavailable-token', rate=-3, volume=67,
                cursor=5, audio_seconds=12.75)
            original = (json.dumps(envelope, ensure_ascii=False, indent=2) + '\n').encode()
            path = root / 'earlier.reader.json'
            path.write_bytes(original)
            snapshot = legacy.read_snapshot(path)
            dialogs = reading_import.ReadingImportDialogs()
            check('actual Windows historical reader controls', wx.Platform == '__WXMSW__')
            check('exact original reader envelope retained in review',
                  snapshot.raw == original and snapshot.archive_sha256 == sha256(original).hexdigest())
            check('old voice and audio position remain passive observations',
                  snapshot.session.voice_id == envelope['voice_id'] and snapshot.session.audio_seconds == 12.75)
            check('session review preserves complete undo and redo history',
                  snapshot.saved_revision == 1 and len(snapshot.edits) == 2 and snapshot.at(2).text == source)
            check('review alone creates no retained project or reading settings',
                  not (app.root / legacy.DIRECTORY).exists()
                  and not (app.root / 'narration-projects').exists()
                  and not (app.root / 'state' / 'reading-speech.json').exists())

            def owned():
                return [window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.Dialog) and window.IsModal() and window.GetParent() is frame]
            def children(dialog):
                pending, result = list(dialog.GetChildren()), []
                while pending:
                    item = pending.pop()
                    result.append(item)
                    if len(result) > 80:
                        raise AssertionError('Unexpected native history control inventory.')
                    pending.extend(item.GetChildren())
                return result
            def drive(call, phase):
                state = {'active': True, 'done': False}
                deadline = monotonic() + 12
                def poll():
                    if not state['active']:
                        return
                    try:
                        if monotonic() >= deadline:
                            raise TimeoutError('Native historical reader dialog did not settle.')
                        windows = owned()
                        if not windows:
                            timers.append(wx.CallLater(30, poll))
                            return
                        check('one owned native ' + phase + ' dialog', len(windows) == 1)
                        dialog = windows[0]
                        controls = children(dialog)
                        if phase == 'revision':
                            lists = [item for item in controls if isinstance(item, wx.ListBox)]
                            check('native revision list preserves Unicode and redo',
                                  isinstance(dialog, wx.SingleChoiceDialog) and len(lists) == 1
                                  and tuple(lists[0].GetStrings()) == snapshot.revision_labels)
                            dialog.SetSelection(2)
                            check('native revision list selects exact earlier redo', lists[0].GetSelection() == 2)
                        else:
                            fields = {item.GetName(): item for item in controls if isinstance(item, wx.TextCtrl)}
                            check('named native ' + phase + ' review fields',
                                  set(fields) == {'New reading-copy title',
                                      'Retained source positions and historical revision', 'Historical reading text preview'})
                            history = fields['Retained source positions and historical revision']
                            body = fields['Historical reading text preview']
                            title = fields['New reading-copy title']
                            check('read-only native ' + phase + ' historical session observations',
                                  not history.IsEditable() and not body.IsEditable()
                                  and history.GetValue() == legacy.history_description(snapshot.at(2))
                                  and 'not applied' in history.GetValue()
                                  and envelope['voice_id'] in history.GetValue()
                                  and '12.75 seconds' in history.GetValue() and body.GetValue() == source)
                            check('editable native ' + phase + ' new copy title', title.IsEditable())
                            title.SetValue('Нова копія — čítanie')
                        button = dialog.FindWindow(wx.ID_CANCEL if phase == 'cancel' else wx.ID_OK)
                        check('enabled native ' + phase + ' decision button',
                              isinstance(button, wx.Button) and button.IsEnabled())
                        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                        event.SetEventObject(button)
                        state['done'] = True
                        check('dispatched native ' + phase + ' decision event',
                              button.GetEventHandler().ProcessEvent(event))
                    except Exception:
                        errors.append(traceback.format_exc())
                        state['active'] = False
                        for dialog in owned():
                            dialog.EndModal(wx.ID_CANCEL)
                timers.append(wx.CallLater(30, poll))
                try:
                    result = call()
                    if errors:
                        raise AssertionError(errors[-1][-3000:])
                    check('settled native ' + phase + ' dialog', state['done'] and not owned())
                    return result
                finally:
                    state['active'] = False
                    for timer in timers:
                        timer.Stop()

            selected = drive(lambda: dialogs.choose(frame, 'Choose historical reader revision',
                'Saved session observations remain inert.', snapshot.revision_labels), 'revision')
            check('native selection returns exact retained revision', selected == 2)
            cancelled = drive(lambda: dialogs.preview(frame, snapshot.at(selected)), 'cancel')
            check('native Cancel leaves original and current stores unchanged',
                  cancelled is None and path.read_bytes() == original
                  and not (app.root / legacy.DIRECTORY).exists()
                  and not (app.root / 'narration-projects').exists())
            title = drive(lambda: dialogs.preview(frame, snapshot.at(selected)), 'accept')
            check('native accept returns explicitly reviewed Unicode title', title == 'Нова копія — čítanie')
            legacy.LegacyReadingStore(app.root).retain(original)
            project = legacy.project_from_snapshot(snapshot.at(selected), title=title)
            digest = NarrationStore(app.root).save(project)
            check('accepted session becomes a new current derivative bound to the full envelope',
                  project.current_text == source and project.legacy_origin.selected_revision == 2
                  and project.legacy_origin.archive_sha256 == snapshot.archive_sha256
                  and NarrationStore(app.root).load(project.id) == (project, digest))
            backup = root / 'backup'
            expected = app.backup(backup)
            verify_backup(backup, expected)
            restored = LibraryApp(restore_backup(backup, root / 'restored', expected))
            recovered = legacy.LegacyReadingStore(restored.root).load(snapshot.archive_sha256, revision=2)
            check('complete backup restores exact original envelope and session observations',
                  recovered.raw == original and recovered.session == snapshot.session
                  and NarrationStore(restored.root).load(project.id)[0] == project)
            check('restored reader history does not create active speech settings',
                  not (restored.root / 'state' / 'reading-speech.json').exists())
            second = root / 'second'
            second_digest = restored.backup(second)
            verify_backup(second, second_digest)
            again = LibraryApp(restore_backup(second, root / 'again', second_digest))
            check('restored reader envelope supports exact full rebackup',
                  legacy.LegacyReadingStore(again.root).load(snapshot.archive_sha256).raw == original)
            check('external original session bytes remain unchanged', path.read_bytes() == original)
            return {'checks': checks, 'archive_sha256': snapshot.archive_sha256,
                    'restored_archive_sha256': sha256(recovered.raw).hexdigest(),
                    'historical_cursor': recovered.session.cursor,
                    'historical_audio_seconds': recovered.session.audio_seconds,
                    'selected_revision': selected, 'saved_revision': snapshot.saved_revision,
                    'speech_settings_created': False}
    finally:
        for timer in timers:
            timer.Stop()
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        frame.Destroy()
        application.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit Windows native execution is required.'}))
        return 77
    done = Event()
    def watchdog():
        if not done.wait(180):
            print(json.dumps({'status': 'FAIL', 'reason': 'Actual native reader-session probe timed out.'}), flush=True)
            os._exit(1)
    Thread(target=watchdog, daemon=True, name='legacy-reader-session-watchdog').start()
    try:
        with ExitStack() as stack:
            from carr_library.native_runtime import activate_bundled_runtime
            handle = activate_bundled_runtime()
            if handle is not None:
                stack.callback(handle.close)
            before = identity()
            result = run_native()
            if identity() != before:
                raise ValueError('Actual installed reader code changed during native review.')
            result.update(status='PASS', installed_mode=True, isolated=bool(sys.flags.isolated),
                native_controls='EXECUTED', module_origins=before,
                source_commit=os.environ.get('CARR_EXPECTED_SOURCE_COMMIT') or os.environ['GITHUB_SHA'],
                nvda='NOT_RUN', physical_playback='NOT_RUN', engine_execution='NOT_RUN')
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            return 0
    except BaseException as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
        print(json.dumps({'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error)[:1800]}), flush=True)
        return 1
    finally:
        done.set()


if __name__ == '__main__':
    raise SystemExit(main())
