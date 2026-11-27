"""Actual native metadata controls; portable FFmpeg publication tests are separate."""
import json
import sys
import traceback


def main():
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows wx is required.'}))
        return 77
    import wx
    from carr_library.ui.audio_publication import show_publication_options

    application = wx.App(False)
    frame = wx.Frame(None, title='Resonance publication metadata acceptance')
    frame.Show()
    errors, checks = [], []

    def run(book_format, *, cancel=False):
        timers = []
        def ready(dialog, fields, status):
            def failed():
                errors.append('Native publication metadata watchdog expired.')
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
            timers.append(wx.CallLater(10000, failed))
            try:
                assert fields['language'].GetName() == 'Publication language code'
                assert fields['creator'].GetName() == 'Creator name (optional)'
                fields['language'].SetValue('uk')
                fields['creator'].SetValue('Автор 🙂')
                if cancel:
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_CANCEL)
                    event.SetEventObject(dialog.FindWindowById(wx.ID_CANCEL))
                    dialog.GetEventHandler().ProcessEvent(event)
                    checks.append('native_cancel')
                    return
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK)
                event.SetEventObject(dialog.FindWindowById(wx.ID_OK))
                if book_format == 'daisy-2.02':
                    assert fields['publisher'].GetName() == 'Publisher (required for DAISY)'
                    dialog.GetEventHandler().ProcessEvent(event)
                    assert dialog.IsModal() and not fields['publisher'].GetValue()
                    assert 'Publisher' in status.GetValue()
                    checks.append('publisher_required')
                    fields['publisher'].SetValue('Видавець')
                fields['language'].SetValue('not a language')
                dialog.GetEventHandler().ProcessEvent(event)
                assert dialog.IsModal()
                assert 'language' in status.GetValue().lower()
                checks.append('invalid_language_retains_modal')
                fields['language'].SetValue('uk')
                fields['creator'].SetFocus()
                wx.YieldIfNeeded()
                assert wx.Window.FindFocus() is fields['creator']
                dialog.GetEventHandler().ProcessEvent(event)
                checks.append('native_ok')
            except BaseException:
                errors.append(traceback.format_exc())
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
        try:
            result = show_publication_options(frame, book_format, on_ready=ready)
        finally:
            for timer in timers:
                timer.Stop()
        if errors:
            raise AssertionError('\n'.join(errors))
        if cancel:
            assert result is None
        else:
            assert result['language'] == 'uk' and result['creator'] == 'Автор 🙂'
            if book_format == 'daisy-2.02':
                assert result['publisher'] == 'Видавець'
        return result

    try:
        run('epub')
        run('daisy-2.02')
        run('epub', cancel=True)
        print(json.dumps({'status': 'PASS', 'native_metadata_controls': 'PASS', 'checks': checks,
                          'full_file_picker_flow': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
                          'third_party_reader_interoperability': 'NOT_RUN'}))
    finally:
        frame.Destroy()
        application.ProcessPendingEvents()
        application.ProcessIdle()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
