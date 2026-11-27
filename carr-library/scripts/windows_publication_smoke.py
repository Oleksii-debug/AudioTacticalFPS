"""Actual native metadata controls; portable FFmpeg publication tests are separate."""
import json
import sys
from time import monotonic
import traceback


def drive_metadata(wx, show_options, frame, book_format, record, *, cancel=False):
    """Separate native edits, validation and focus across ordinary event turns."""
    state = {'active': True, 'phase': 'waiting-for-modal', 'ready': None, 'submitted': False}
    timer = [None]
    errors = []
    deadline = monotonic() + 10

    def ready(dialog, fields, status):
        if state['active']:
            state['ready'] = (dialog, fields, status)

    def schedule():
        if state['active']:
            timer[0] = wx.CallLater(25, poll)

    def press(dialog, identifier):
        button = dialog.FindWindowById(identifier)
        assert isinstance(button, wx.Button) and button.IsEnabled(), 'Expected an enabled native button.'
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), 'The native button event was not handled.'

    def poll():
        if not state['active']:
            return
        dialog = state['ready'][0] if state['ready'] is not None else None
        try:
            if monotonic() >= deadline:
                raise TimeoutError(f'Native metadata timed out: {book_format}/{state["phase"]}.')
            if dialog is None or not dialog.IsModal() or not dialog.IsShown():
                schedule()
                return
            assert dialog.GetParent() is frame, 'The metadata modal has another owner.'
            _, fields, status = state['ready']
            if state['phase'] == 'waiting-for-modal':
                assert fields['language'].GetName() == 'Publication language code'
                assert fields['creator'].GetName() == 'Creator name (optional)'
                record(book_format + ': named controls in shown owned modal')
                fields['language'].SetValue('uk')
                fields['creator'].SetValue('Автор 🙂')
                if cancel:
                    state['phase'] = 'submit-cancel'
                    press(dialog, wx.ID_CANCEL)
                    assert not dialog.IsShown(), 'Cancel did not hide the native metadata modal.'
                    state['submitted'] = True
                    record('native cancel')
                    return
                if book_format == 'daisy-2.02':
                    assert fields['publisher'].GetName() == 'Publisher (required for DAISY)'
                    press(dialog, wx.ID_OK)
                    assert dialog.IsShown() and not fields['publisher'].GetValue()
                    assert 'Publisher' in status.GetValue()
                    record('publisher required')
                    fields['publisher'].SetValue('Видавець')
                fields['language'].SetValue('not a language')
                press(dialog, wx.ID_OK)
                state['phase'] = 'invalid-language'
                schedule()
                return
            if state['phase'] == 'invalid-language':
                assert dialog.IsShown() and 'language' in status.GetValue().lower()
                record('invalid language retains modal')
                fields['language'].SetValue('uk')
                fields['creator'].SetFocus()
                state['phase'] = 'creator-focus'
                schedule()
                return
            assert state['phase'] == 'creator-focus', 'Unexpected metadata probe phase.'
            assert wx.Window.FindFocus() is fields['creator'], 'Creator field did not receive native focus.'
            record('creator field retains native focus on the next event turn')
            state['phase'] = 'submit-ok'
            press(dialog, wx.ID_OK)
            assert not dialog.IsShown(), f'Metadata was not accepted: {status.GetValue()[:512]}'
            state['submitted'] = True
            record('native OK')
        except BaseException:
            errors.append(traceback.format_exc())
            state['active'] = False
            if dialog is not None and dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    schedule()
    try:
        result = show_options(frame, book_format, on_ready=ready)
        if errors:
            raise AssertionError(errors[-1])
        assert state['submitted'], 'The metadata modal returned before the native action was submitted.'
        if cancel:
            assert result is None
        else:
            assert result['language'] == 'uk' and result['creator'] == 'Автор 🙂'
            if book_format == 'daisy-2.02':
                assert result['publisher'] == 'Видавець'
        return result
    finally:
        state['active'] = False
        if timer[0] is not None:
            timer[0].Stop()
        state['ready'] = None


def main():
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows wx is required.'}))
        return 77
    import wx
    from carr_library.ui.audio_publication import show_publication_options

    application = wx.App(False)
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    frame = wx.Frame(None, title='Resonance publication metadata acceptance')
    application.SetTopWindow(frame)
    frame.Show()
    checks = []

    def record(label):
        checks.append(label)
        print('PASS:', label, flush=True)

    try:
        drive_metadata(wx, show_publication_options, frame, 'epub', record)
        drive_metadata(wx, show_publication_options, frame, 'daisy-2.02', record)
        drive_metadata(wx, show_publication_options, frame, 'epub', record, cancel=True)
        print(json.dumps({'status': 'PASS', 'native_metadata_controls': 'PASS', 'checks': checks,
                          'full_file_picker_flow': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
                          'third_party_reader_interoperability': 'NOT_RUN'}), flush=True)
    finally:
        frame.Destroy()
        application.ProcessPendingEvents()
        native_loop.ProcessIdle()
        application.Yield()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
