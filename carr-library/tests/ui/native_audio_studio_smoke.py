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
    from carr_library.audio.director import ProductionDirector
    from carr_library.audio.render import probe_audio
    from carr_library.ui.audio_studio import StudioController,StudioDialog,prepare_reading_studio
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.local_assistant import AssistantDialog
    errors,checks=[],[]
    effects_receipt={}
    gui=wx.App(False)

    def pump(predicate,timeout=90):
        deadline=monotonic()+timeout
        while not predicate():
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
                    pump(lambda:not form.busy,20)
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

    def project_effect(form, selected, kind, controls):
        """Drive the real scope picker and existing typed chain/number dialogs."""
        timers=[]
        def dialog_named(title):
            matches=[window for window in wx.GetTopLevelWindows()
                     if isinstance(window,wx.Dialog) and window.IsModal() and window.GetTitle()==title]
            assert len(matches)==1,(title,len(matches))
            return matches[0]
        def click(dialog,name):
            button=wx.FindWindowByName(name,dialog)
            assert button and button.IsEnabled() and button.AcceptsFocusFromKeyboard(),name
            event=wx.CommandEvent(wx.EVT_BUTTON.typeId,button.GetId())
            assert button.GetEventHandler().ProcessEvent(event),name
        def schedule(action):
            def guarded():
                try:
                    action()
                except BaseException:
                    errors.append(traceback.format_exc())
                    for window in reversed(wx.GetTopLevelWindows()):
                        if isinstance(window,wx.Dialog) and window is not form.dialog and window.IsModal():
                            window.EndModal(wx.ID_CANCEL)
            timers.append(wx.CallLater(40,guarded))
        def accept_chain():
            dialog=dialog_named('Ordered audio effects')
            ordered=wx.FindWindowByName('Ordered audio effects',dialog)
            assert ordered and ordered.GetCount()==1
            assert kind in ordered.GetString(0)
            click(dialog,'Apply effect chain')
        def numbers():
            dialog=dialog_named('Edit '+kind+' effect')
            pending=list(dialog.GetChildren())
            fields=[]
            while pending:
                window=pending.pop()
                assert len(fields)+len(pending)<512
                if isinstance(window,wx.TextCtrl):
                    fields.append(window)
                pending.extend(window.GetChildren())
            for name,value in controls.items():
                candidates=[field for field in fields if field.GetName().startswith(name.replace('_',' ')+' (')]
                assert len(candidates)==1,(name,[field.GetName() for field in fields])
                assert candidates[0].AcceptsFocusFromKeyboard()
                candidates[0].ChangeValue(str(value))
            schedule(accept_chain)
            click(dialog,'Apply effect')
        def chain():
            dialog=dialog_named('Ordered audio effects')
            kinds=wx.FindWindowByName('Effect type to add',dialog)
            assert kinds and kinds.SetStringSelection(kind)
            assert 'tempo' not in [kinds.GetString(index) for index in range(kinds.GetCount())]
            click(dialog,'Add effect')
            schedule(numbers)
            click(dialog,'Edit selected effect')
        def scope():
            picker=dialog_named('Role, scene and master effects')
            picker.SetSelection(selected)
            assert picker.GetSelection()==selected
            schedule(chain)
            picker.EndModal(wx.ID_OK)
        schedule(scope)
        try:
            form.edit_effects()
        finally:
            for timer in timers:
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
                    effect_button=next(button for button in form.buttons
                                       if button.GetLabel()=='Role, scene and master effects')
                    assert effect_button.AcceptsFocusFromKeyboard() and effect_button.IsEnabled()
                    project_effect(form,1,'equalizer',{'frequency':800,'gain_db':-6,'q':1})
                    project_effect(form,1+len(controller.project.roles),'reverb',
                                   {'room_ms':30,'decay':.4,'mix':.2})
                    project_effect(form,0,'compressor',{'threshold_db':-20,'ratio':2})
                    saved=controller.store.load(controller.project.id)
                    assert saved==controller.project and saved.script==script and saved.script_sha256==expected
                    assert saved.roles[0].effects[0].kind=='equalizer'
                    assert saved.roles[0].effects[0].parameters['gain_db']==-6
                    assert saved.scenes[0].effects[0].kind=='reverb' and not saved.scenes[1].effects
                    assert saved.effects[0].kind=='compressor'
                    assert 'effects, in order:' in form.plan.GetValue()
                    checks.extend(['native Director role/scene/master scope picker',
                                   'typed EQ room and dynamics controls persist without changing source'])
                    director=ProductionDirector(library.root,registry=controller.registry)
                    def render_with_diagnostic():
                        try:
                            return director.render(controller.project.id,root/'actual output',
                                cancelled=form.cancelled.is_set,progress=form.progress)
                        except Exception:
                            # The production UI reports a short user-facing
                            # error; this synthetic CI probe retains its real
                            # worker traceback to identify the failing layer.
                            print('NATIVE_AUDIO_RENDER_TRACEBACK\n'+traceback.format_exc(),flush=True)
                            raise
                    form.start(render_with_diagnostic)
                    assert form.busy and not form.script.IsEnabled() and form.stop.IsEnabled()
                    pump(lambda:not form.busy)
                    assert form.result and form.result['state']=='complete',form.status.GetValue()
                    assert probe_audio(form.result['master']).duration_seconds>1
                    assert form.script.GetValue()==script and controller.store.load(controller.project.id).script_sha256==expected
                    assert (Path(form.result['output_dir'])/'narration.srt').is_file()
                    manifest=json.loads((Path(form.result['output_dir'])/'production.json').read_text(encoding='utf-8'))
                    with wave.open(form.result['master'],'rb') as master:
                        frame_bytes=master.getnchannels()*master.getsampwidth()
                        master_frames=master.getnframes()
                        accepted=master.readframes(master_frames)
                    chunks=[]
                    for index,row in enumerate(manifest['chapters'],1):
                        chapter=Path(form.result['output_dir'])/f'chapter-{index:04}.wav'
                        with wave.open(str(chapter),'rb') as audio_chapter:
                            assert audio_chapter.getnframes()==row['frames']
                            data=audio_chapter.readframes(row['frames'])
                        start=row['start_frame']*frame_bytes
                        assert data==accepted[start:start+row['frames']*frame_bytes]
                        assert sha256(chapter.read_bytes()).hexdigest()==row['sha256']
                        chunks.append(data)
                    assert b''.join(chunks)==accepted
                    assert sum(row['frames'] for row in manifest['chapters'])==master_frames
                    assert manifest['effects']['master']==[effect.to_dict() for effect in saved.effects]
                    effects_receipt.update(project_fingerprint=saved.fingerprint,
                        original_script_sha256=expected,master_sha256=sha256(Path(form.result['master']).read_bytes()).hexdigest(),
                        frames=master_frames,sample_rate=saved.settings.sample_rate,
                        effects=manifest['effects'],chapters=manifest['chapters'],
                        source_unchanged=True,master_chapters_exact_pcm=True,
                        acoustic_quality='NOT_RUN',manual_nvda='NOT_RUN')
                    checks.extend(['actual Director effect render retains exact master chapter samples',
                                   'processed manifest binds typed chains and immutable script'])
                    assert form.script.IsEnabled() and not form.stop.IsEnabled()
                    checks.extend(['named native studio controls','unsaved draft recovery','Ctrl+S handler saves a revision',
                                   'three native voice assignments','background speech and music production',
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
                # Literal search returns the authenticated matching span.
                # Validate the complete retained source independently.
                match=library.search(workspace,'railway')[0]
                assert match.quote=='railway' and match.edition_id==imported['edition']
                retained=ReadingDesk(library,workspace,imported['edition']).page(1)
                assert retained.raw_text==source
                assert retained.location.text_sha256==sha256(source.encode('utf-8')).hexdigest()
                checks.append('literal quotation and retained source remain unchanged')
            finally:
                frame.Close()
                gui.Yield()
        print(json.dumps({'status':'PASS','checks':checks,'director_effects':effects_receipt,'nvda':'NOT_RUN',
                          'audible_playback':'NOT_CLAIMED','external_services':'NOT_USED'},ensure_ascii=False))
        return 0
    finally:
        if audio is not None:
            audio.close()
        if native is not None:
            native.close()


if __name__=='__main__':
    raise SystemExit(main())
