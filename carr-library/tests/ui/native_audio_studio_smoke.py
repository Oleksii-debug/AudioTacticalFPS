"""Real installed Windows controls, save/recovery, local production and assistant.

This probe uses actual wx, SQLite, Flite and FFmpeg. It does not assert audible
playback or manual NVDA acceptance. No mock replaces an audio or storage backend.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic,sleep
import traceback
import wave


def main():
    if os.name!='nt':
        print(json.dumps({'status':'NOT_RUN','reason':'Windows native session required','nvda':'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    from carr_library.audio.runtime import activate_bundled_audio_runtime
    native=activate_bundled_runtime()
    audio=activate_bundled_audio_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.render import probe_audio
    from carr_library.ui.audio_studio import StudioController,StudioDialog,prepare_reading_studio
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.local_assistant import AssistantDialog
    errors,checks=[],[]
    native_generate={}
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            return True
    gui=ProbeApp(False)
    original_dialogs={name:getattr(wx,name) for name in
                      ('DirDialog','FileDialog','MessageDialog','TextEntryDialog',
                       'SingleChoiceDialog','MessageBox')}
    def unexpected_dialog(*args,**kwargs):
        message='Unexpected modal dialog in unattended native acceptance.'
        errors.append(message)
        raise AssertionError(message)
    for name in original_dialogs:
        setattr(wx,name,unexpected_dialog)

    def pump(predicate,timeout=90,check_errors=True):
        deadline=monotonic()+timeout
        while not predicate():
            if check_errors and errors:
                raise AssertionError('\n'.join(errors))
            if monotonic()>deadline:
                raise TimeoutError('The native workflow did not finish before its deadline.')
            gui.Yield()
            sleep(0.02)

    def modal(form,callback):
        def perform():
            try:
                callback(form)
                form.close()
            except BaseException:
                errors.append(traceback.format_exc())
                form.cancelled.set()
                try:
                    pump(lambda:not form.busy,20,check_errors=False)
                except Exception:
                    pass
                form.dirty=False
                if form.dialog.IsModal():
                    form.dialog.EndModal(wx.ID_CANCEL)
        timer=wx.CallLater(150,perform)
        form.show()
        timer.Stop()
        if errors:
            raise AssertionError('\n'.join(errors))

    try:
        with TemporaryDirectory(prefix='carr-native-audio-') as temporary:
            root=Path(temporary)
            library=LibraryApp(root/'private library')
            workspace=library.create_workspace('Native audio verification')
            source='The original railway quotation stays unchanged.'
            imported=library.add_text(workspace,'Railway source',source)
            assert imported['state']=='complete'
            frame=DesktopFrame(library)
            if frame.active!=(workspace,None):
                frame._open_workspace(workspace)
            frame.Show()
            frame.Raise()
            try:
                controller=StudioController(library.root)
                assert {'flite:kal','flite:slt','flite:rms'}<={voice.id for voice in controller.voices}
                script='# First\n@music calm 80\nNarrator: The local library is ready.\nAlice: Welcome.\n@pause 0.25\n# Second\nBob: The source remains unchanged.\n'
                expected=sha256(script.encode('utf-8')).hexdigest()
                def studio(form):
                    assert form.dialog.IsModal()
                    for control in (form.title,form.language,form.format,form.script,form.role_list,form.voice,
                                    form.pan,form.rate,form.plan,form.status):
                        assert control.GetName() and control.AcceptsFocusFromKeyboard(),control.GetName()
                    form.title.SetValue('Native three-voice project')
                    form.script.SetValue(script)
                    form.autosave()
                    assert controller.store.new_drafts()[0]['script']==script
                    event=wx.CommandEvent(wx.EVT_MENU.typeId,int(form._ids[0]))
                    assert form.dialog.GetEventHandler().ProcessEvent(event)
                    assert controller.project.script_sha256==expected and not form.dirty
                    assert not controller.store.new_drafts()
                    for index,voice_id in enumerate(('flite:kal','flite:slt','flite:rms')):
                        form.role_list.SetSelection(index)
                        form.select_role()
                        form.voice.SetSelection([voice.id for voice in controller.voices].index(voice_id))
                        form.pan.SetSelection(index if index<2 else 2)
                        form.apply_voice()
                    assert len(controller.project.roles)==3
                    saved_project=controller.store.load(controller.project.id).to_dict()
                    output_parent=root/'chosen native output'
                    output_parent.mkdir()
                    choices=[]
                    decisions=iter((wx.ID_OK,wx.ID_CANCEL))

                    class SelectedDirectory(original_dialogs['DirDialog']):
                        """Supply only the chooser decision; production stays real."""
                        def __init__(self,parent,message,*args,**kwargs):
                            assert parent is form.dialog
                            assert message=='Choose the parent folder for a new audio output folder'
                            super().__init__(parent,message,*args,**kwargs)
                            self.SetPath(str(output_parent))
                            assert Path(self.GetPath()).samefile(output_parent)

                        def ShowModal(self):
                            decision=next(decisions)
                            choices.append(decision)
                            return decision

                    wx.DirDialog=SelectedDirectory
                    try:
                        generate=next(button for button in form.buttons
                                      if button.GetLabel().replace('&','')=='Generate audio')
                        assert generate.AcceptsFocusFromKeyboard()
                        assert not list(output_parent.iterdir()) and form.result is None
                        event=wx.CommandEvent(wx.EVT_BUTTON.typeId,generate.GetId())
                        event.SetEventObject(generate)
                        assert generate.GetEventHandler().ProcessEvent(event)
                        assert choices==[wx.ID_OK]
                        assert form.busy and not form.script.IsEnabled() and form.stop.IsEnabled()
                        pump(lambda:not form.busy)
                        assert form.result and form.result['state']=='complete',form.status.GetValue()
                        master=Path(form.result['master'])
                        output=Path(form.result['output_dir'])
                        assert output.parent.samefile(output_parent) and master.parent.samefile(output)
                        assert len(list(output_parent.iterdir()))==1
                        info=probe_audio(master)
                        assert info.duration_seconds>1 and info.frames>0
                        with wave.open(str(master),'rb') as pcm:
                            assert pcm.getcomptype()=='NONE' and pcm.getsampwidth()==2
                            assert pcm.getnframes()==info.frames
                            assert pcm.getframerate()==info.sample_rate
                            assert pcm.getnchannels()==info.channels
                            nonzero=False
                            read_frames=0
                            while block:=pcm.readframes(8192):
                                nonzero=nonzero or any(block)
                                read_frames+=len(block)//(pcm.getsampwidth()*pcm.getnchannels())
                        assert nonzero and read_frames==info.frames
                        assert form.status.GetValue().startswith('Audio completed.')
                        assert form.status.GetName() and form.status.AcceptsFocusFromKeyboard()
                        pump(lambda:wx.Window.FindFocus()==form.status,timeout=5)
                        assert form.script.IsEnabled() and not form.stop.IsEnabled()
                        assert (output/'narration.srt').is_file()
                        assert form.script.GetValue()==script
                        assert controller.store.load(controller.project.id).to_dict()==saved_project

                        # Ctrl+Enter reaches the same real Generate handler. Cancel
                        # its chooser without starting a second production.
                        completed=form.result
                        before_output=tuple(sorted(path.name for path in output_parent.iterdir()))
                        event=wx.CommandEvent(wx.EVT_MENU.typeId,int(form._ids[1]))
                        event.SetEventObject(form.dialog)
                        assert form.dialog.GetEventHandler().ProcessEvent(event)
                        assert choices==[wx.ID_OK,wx.ID_CANCEL]
                        assert not form.busy and form.result is completed
                        assert tuple(sorted(path.name for path in output_parent.iterdir()))==before_output
                        assert controller.store.load(controller.project.id).to_dict()==saved_project
                        assert not errors
                        native_generate.update(status='PASS',button_event='PASS',
                            accelerator_cancel='PASS',selected_output_parent='PASS',
                            frames=info.frames,sample_rate=info.sample_rate,channels=info.channels,
                            duration_seconds=info.duration_seconds,nonzero_pcm=True,
                            completed_accessible_status=True,source_unchanged=True,
                            unexpected_dialog_or_callback_errors=0)
                    finally:
                        wx.DirDialog=unexpected_dialog
                    checks.extend(['named native studio controls','unsaved draft recovery','Ctrl+S handler saves a revision',
                                   'three native voice assignments','Generate button dispatches actual production',
                                   'Ctrl+Enter chooser cancellation','completed named status receives focus',
                                   'background speech and music production',
                                   'actual chapter/subtitle/stem files','original script unchanged'])
                modal(StudioDialog(frame,controller),studio)

                original='Author: A source line.\r\n@pause 99\r\nThe exact original ending.'
                reference=library.add_text(workspace,'Original CRLF source',original)
                page=ReadingDesk(library,workspace,reference['edition']).page(1)
                reading=prepare_reading_studio(library.root,page,controller.registry)
                def reading_form(form):
                    assert not form.script.IsEditable()
                    assert form.save(),form.status.GetValue()
                    assert reading.project.script==original
                    assert all(cue.kind=='speech' for scene in reading.project.scenes for cue in scene.cues)
                    checks.append('native source reading copy preserves exact CRLF and treats directives as text')
                modal(StudioDialog(frame,reading,script=original,title='Reading copy'),reading_form)

                def assistant(form):
                    form.goal.SetValue('find railway')
                    form.prepare()
                    assert form.plan and form.run_button.IsEnabled(),form.status.GetValue()
                    assert 'library.search' in form.preview.GetValue()
                    form.run()
                    pump(lambda:not form.busy)
                    assert 'railway' in form.preview.GetValue().casefold(),form.status.GetValue()
                    assert 'Workflow complete' in form.status.GetValue()
                    checks.append('reviewed assistant plan executes actual selected-library search')
                modal(AssistantDialog(frame),assistant)
                # Literal search deliberately returns the authenticated matching
                # span, not the entire source page. Check both identities so a
                # shortened quote cannot conceal a changed retained original.
                match=library.search(workspace,'railway')[0]
                assert match.quote=='railway' and match.edition_id==imported['edition']
                retained=ReadingDesk(library,workspace,imported['edition']).page(1)
                assert retained.raw_text==source
                assert retained.location.text_sha256==sha256(source.encode('utf-8')).hexdigest()
                checks.append('literal quotation and retained source remain unchanged')
            finally:
                frame.Close()
                gui.Yield()
        print(json.dumps({'status':'PASS','checks':checks,'native_generate':native_generate,'nvda':'NOT_RUN',
                          'audible_playback':'NOT_CLAIMED','external_services':'NOT_USED'},ensure_ascii=False))
        return 0
    finally:
        for name,implementation in original_dialogs.items():
            setattr(wx,name,implementation)
        if audio is not None:
            audio.close()
        if native is not None:
            native.close()


if __name__=='__main__':
    raise SystemExit(main())
