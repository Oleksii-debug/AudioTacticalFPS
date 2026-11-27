"""Native selection and explicit review for the existing local voice adapters."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from ..audio.voice_settings import prepare_profile, session_for


def open_local_voice_settings(parent, root, *, session=None, on_ready=None):
    import wx
    session = session or session_for(root)
    store = session.store
    dialog = wx.Dialog(parent, title='Local voice profiles', size=(850, 660),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    panel = wx.ScrolledWindow(dialog)
    panel.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)
    message = ('Select an existing reviewed local installation. Saving a profile does not enable it. '
               'Enable it explicitly for this session after reviewing its runtime and model license. '
               'Restored profiles require selecting their local paths again.')
    intro = wx.StaticText(panel, label=message)
    intro.Wrap(790)
    layout.Add(intro, 0, wx.EXPAND | wx.ALL, 7)
    profiles = wx.ListBox(panel)
    profiles.SetName('Saved local voice profiles; connection and session status')
    profiles.SetMinSize((-1, 110))
    layout.Add(profiles, 0, wx.EXPAND | wx.ALL, 7)
    engine = wx.Choice(panel, choices=['Piper', 'dordiai-voices'])
    engine.SetName('Existing local voice adapter')
    engine.SetSelection(0)
    layout.Add(wx.StaticText(panel, label='Local voice adapter'), 0, wx.LEFT | wx.TOP, 7)
    layout.Add(engine, 0, wx.EXPAND | wx.ALL, 7)

    def field(label, value=''):
        layout.Add(wx.StaticText(panel, label=label), 0, wx.LEFT | wx.TOP, 7)
        result = wx.TextCtrl(panel, value=value)
        result.SetName(label)
        layout.Add(result, 0, wx.EXPAND | wx.ALL, 7)
        return result
    name = field('Profile name')
    manifest = field('Reviewed model manifest JSON')
    executable = field('Piper executable; unused for dordiai-voices')
    pickers = wx.WrapSizer(wx.HORIZONTAL)
    layout.Add(pickers, 0, wx.ALL, 4)
    buttons = wx.WrapSizer(wx.HORIZONTAL)
    layout.Add(buttons, 0, wx.ALL, 4)
    status = wx.TextCtrl(panel, value=message, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Local voice review and save status')
    status.SetMinSize((-1, 110))
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 7)
    panel.SetSizer(layout)
    outer = wx.BoxSizer(wx.VERTICAL)
    outer.Add(panel, 1, wx.EXPAND)
    dialog.SetSizer(outer)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-voice-review')
    state = {'closed': False, 'busy': False, 'rows': (), 'selected': None, 'close_requested': False}
    cancel = threading.Event()
    controls = [profiles, engine, name, manifest, executable]

    def say(value):
        status.ChangeValue(str(value))
        status.SetInsertionPoint(0)

    def selection(event=None):
        index = profiles.GetSelection()
        row = state['rows'][index] if 0 <= index < len(state['rows']) else None
        state['selected'] = row
        if row is not None:
            name.ChangeValue(row.name)
            engine.SetSelection(0 if row.engine == 'piper' else 1)
            manifest.ChangeValue(row.manifest or '')
            executable.ChangeValue(row.executable or '')

    def refresh(selected=None):
        rows, active = store.load(), session.active_ids()
        state['rows'] = rows
        profiles.SetItems([row.name + ' — ' + row.engine + ' — ' +
            ('enabled this session' if row.id in active else
             'saved, not enabled' if row.connected else 'restored; select local paths')
            for row in rows])
        index = next((i for i, row in enumerate(rows) if row.id == selected), -1)
        profiles.SetSelection(index)
        state['selected'] = rows[index] if index >= 0 else None

    def finish(future, done):
        if state['closed']:
            return
        state['busy'] = False
        for control in controls:
            control.Enable()
        cancel_button.Disable()
        try:
            result = future.result()
            done(result)
        except Exception as exc:
            say(str(exc))
        if state['close_requested']:
            close()

    def run(operation, done):
        if state['busy'] or state['closed']:
            return
        state['busy'] = True
        cancel.clear()
        for control in controls:
            control.Disable()
        cancel_button.Enable()
        say('Reviewing local file identities and license declarations. Cancel remains available.')
        future = pool.submit(operation)
        future.add_done_callback(lambda value: wx.CallAfter(finish, value, done))

    def browse(target, title, wildcard):
        picker = wx.FileDialog(dialog, title, wildcard=wildcard,
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                target.ChangeValue(picker.GetPath())
        finally:
            picker.Destroy()

    def add_button(sizer, label, handler, *, busy_safe=False):
        value = wx.Button(panel, label=label)
        value.Bind(wx.EVT_BUTTON, handler)
        value.SetName(label)
        sizer.Add(value, 0, wx.ALL, 4)
        if not busy_safe:
            controls.append(value)
        return value

    add_button(pickers, 'Choose manifest', lambda event: browse(
        manifest, 'Select reviewed local model manifest', 'JSON (*.json)|*.json'))
    add_button(pickers, 'Choose Piper executable', lambda event: browse(
        executable, 'Select existing Piper executable', 'All files (*.*)|*.*'))

    def new(event=None):
        state['selected'] = None
        profiles.SetSelection(wx.NOT_FOUND)
        name.ChangeValue('')
        manifest.ChangeValue('')
        executable.ChangeValue('')
        say('Enter a name and select existing local files. No download or execution is performed by saving.')

    def save(event=None):
        previous = state['selected']
        captured = (name.GetValue().strip(), 'piper' if engine.GetSelection() == 0 else 'dordiai-voices',
                    manifest.GetValue(), executable.GetValue())
        def operation():
            profile, facts = prepare_profile(captured[0], captured[1], captured[2],
                executable=captured[3] if captured[1] == 'piper' else None,
                identity=previous.id if previous else None, cancelled=cancel.is_set)
            if cancel.is_set():
                raise InterruptedError('Local voice review cancelled before saving.')
            store.save(profile, expected=previous)
            return profile, facts
        def saved(value):
            profile, facts = value
            session.disable(profile.id)
            refresh(profile.id)
            say('Profile saved, not enabled. Model: ' + facts['model'] +
                '; license: ' + facts['license'] + '; verified files: ' + str(facts['files']) +
                '. Choose Review and enable to permit this exact installation for the current session.')
        run(operation, saved)

    def enable(event=None):
        profile = state['selected']
        if profile is None:
            say('Select a saved profile first.')
            return
        if not profile.connected:
            say('This restored profile has no paths. Select its installation and verify/save it before enabling.')
            return
        review = wx.MessageDialog(dialog,
            'Permit this exact local installation for the current app session?\n\n' +
            profile.name + ' (' + profile.engine + ')\nManifest: ' + profile.manifest +
            ('\nExecutable: ' + profile.executable if profile.executable else '') +
            '\nManifest SHA256: ' + profile.manifest_sha256 +
            '\n\nThe selected runtime is executable software. Its file hashes and declared model license '
            'will be rechecked. This does not install software or verify language/acoustic quality.',
            'Review local voice execution', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            accepted = review.ShowModal() == wx.ID_YES
        finally:
            review.Destroy()
        if accepted:
            def enabled(facts):
                refresh(profile.id)
                say('Enabled for this session after file verification. Reload local voices and explicitly choose '
                    'an advertised voice/language. No language quality qualification was inferred.')
            run(lambda: session.enable(profile.id, owner_trusted=True, cancelled=cancel.is_set), enabled)

    def disable(event=None):
        profile = state['selected']
        if profile is not None:
            session.disable(profile.id)
            refresh(profile.id)
            say('Disabled for this session. Its saved selection remains available for later review.')

    def remove(event=None):
        profile = state['selected']
        if profile is not None:
            try:
                store.remove(profile.id, expected=profile)
                session.disable(profile.id)
                refresh()
                say('Saved selection removed. External model and runtime files remain at their original location.')
            except Exception as exc:
                say(str(exc))

    def close(event=None):
        if state['busy']:
            state['close_requested'] = True
            cancel.set()
            say('Cancelling file review before closing. Waiting for its worker to return.')
            if event is not None and hasattr(event, 'Veto'):
                event.Veto()
            return
        state['closed'] = True
        dialog.EndModal(wx.ID_OK)

    new_button = add_button(buttons, 'New selection', new)
    save_button = add_button(buttons, 'Verify and save profile', save)
    enable_button = add_button(buttons, 'Review and enable for this session', enable)
    disable_button = add_button(buttons, 'Disable for this session', disable)
    remove_button = add_button(buttons, 'Remove saved selection', remove)
    cancel_button = add_button(buttons, 'Cancel file review', lambda event: cancel.set(), busy_safe=True)
    cancel_button.Disable()
    close_button = add_button(buttons, 'Close', close, busy_safe=True)
    dialog.SetEscapeId(close_button.GetId())
    dialog.Bind(wx.EVT_CLOSE, close)
    profiles.Bind(wx.EVT_LISTBOX, selection)
    try:
        refresh()
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog, session,
                dict(profiles=profiles, engine=engine, name=name, manifest=manifest,
                     executable=executable, status=status, new=new_button, save=save_button,
                     enable=enable_button, disable=disable_button, remove=remove_button,
                     cancel=cancel_button, close=close_button))
        dialog.ShowModal()
    finally:
        state['closed'] = True
        cancel.set()
        pool.shutdown(wait=False, cancel_futures=True)
        dialog.Destroy()
    return session
