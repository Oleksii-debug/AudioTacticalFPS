"""Required installed Windows Studio controls and real SAPI5 file synthesis.

Run with the installed interpreter: python -I -B scripts/windows_audio_studio_smoke.py
No voices/models are downloaded. Missing native controls or installed SAPI5 voice
is a failure on Windows, never a synthetic pass. Hardware playback, listening,
manual file pickers and NVDA are separate NOT_RUN acceptance items.
"""
from array import array
from hashlib import sha256
import importlib.resources
import json
import os
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status':'NOT_RUN','reason':'Native Windows is required.',
                          'nvda':'NOT_RUN','hardware_playback':'NOT_RUN'}),flush=True)
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_runtime = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.project import ProjectStore
    from carr_library.audio.voices import Sapi5Provider
    from carr_library.ui import audio_studio
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors = [], []
    def check(label,condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:',label,flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            return False

    gui=ProbeApp(False)
    def pump(predicate,label,timeout=10):
        end=monotonic()+timeout
        while monotonic()<end:
            gui.Yield()
            if errors:
                raise RuntimeError('\n'.join(errors))
            if predicate():
                return
            sleep(.01)
        raise TimeoutError(label)

    def focused(control):
        actual=wx.Window.FindFocus()
        while actual is not None:
            if actual is control:
                return True
            actual=actual.GetParent()
        return False

    installed=Path(audio_studio.__file__).resolve()
    check('Studio loads from the installed distribution',
          'site-packages' in {part.casefold() for part in installed.parts})
    check('actual native Windows wx controls are active',wx.Platform=='__WXMSW__')
    resource=importlib.resources.files('carr_library.audio').joinpath('sapi5.ps1')
    check('installed package includes the fixed SAPI5 worker',resource.is_file())
    worker_bytes=resource.read_bytes()
    check('packaged SAPI5 worker is executable implementation, not an empty resource',
          b'SetOutputToWaveFile' in worker_bytes and b'ConvertFrom-Json' in worker_bytes)

    frame=studio=None
    with TemporaryDirectory(prefix='carr-native-audio-') as temporary:
        root=Path(temporary)
        provider=Sapi5Provider()
        voices=provider.list_voices()
        check('real enabled installed SAPI5 voices were discovered',bool(voices))
        selected=next((v for v in voices if any(lang.lower().startswith('en') for lang in v.languages)),voices[0])
        original='Native audio studio verification. The original script stays unchanged.'
        result=provider.synthesize(original,selected.id,root/'native-sapi5.wav',sample_rate=24000,volume=.8)
        import wave
        with wave.open(str(result.path),'rb') as pcm:
            check('SAPI5 produced actual 24 kHz mono PCM16 audio',
                  (pcm.getnchannels(),pcm.getsampwidth(),pcm.getframerate(),pcm.getcomptype())==(1,2,24000,'NONE'))
            signal=array('h',pcm.readframes(pcm.getnframes()))
        check('actual SAPI5 output has nonzero waveform and meaningful duration',
              result.frames>24000 and len(signal)==result.frames and max(map(abs,signal))>100)
        check('selected voice, original text and output checksum are retained',
              result.voice_id==selected.id and result.provider=='sapi5'
              and result.text_sha256==sha256(original.encode('utf-8')).hexdigest()
              and result.sha256==sha256(result.path.read_bytes()).hexdigest())
        try:
            library=LibraryApp(root/'library')
            frame=DesktopFrame(library)
            gui.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            bar=frame.GetMenuBar()
            menu_index=bar.FindMenu('Audio Studio')
            check('existing desktop exposes the Audio Studio menu',menu_index>=0)
            labels=[item.GetItemLabel() for index in range(bar.GetMenuCount())
                    for item in bar.GetMenu(index).GetMenuItems()]
            check('desktop audio accelerator is unique',
                  sum('Ctrl+Alt+A' in label for label in labels)==1)
            studio=audio_studio.build_audio_studio(frame,base_directory=root,auto_discover=False)
            # SAPI enumeration above is real. FFmpeg discovery is deliberately
            # separate: this native UI proof needs no additional downloaded tool.
            studio.session.voices=voices
            studio.voice.SetItems([f'{voice.name}; {voice.provider}' for voice in voices])
            studio.Show()
            studio.Raise()
            check('native accessible editor, timeline and parameters exist',
                  isinstance(studio.script,wx.TextCtrl) and isinstance(studio.timeline,wx.ListBox)
                  and isinstance(studio.pan,wx.SpinCtrlDouble) and isinstance(studio.rate,wx.SpinCtrlDouble))
            check('native controls have distinct meaningful names',
                  studio.script.GetName()=='Author script'
                  and studio.timeline.GetName()=='Chapter and speaker timeline'
                  and studio.pan.GetName()=='Speaker pan, minus one left to one right'
                  and studio.status.GetName()=='Audio studio status and output location')
            check('status is a native read-only multiline reader',
                  bool(studio.status.GetWindowStyleFlag() & wx.TE_READONLY)
                  and bool(studio.status.GetWindowStyleFlag() & wx.TE_MULTILINE))
            studio.title_text.SetFocus()
            pump(lambda: focused(studio.title_text),'Project title never received native focus.')
            check('native Tab traversal is handled',studio.title_text.Navigate())
            pump(lambda: focused(studio.ffmpeg),'Native Tab did not reach the next named field.')
            check('native Shift+Tab traversal is handled',
                  studio.ffmpeg.Navigate(flags=wx.NavigationKeyEvent.IsBackward))
            pump(lambda: focused(studio.title_text),'Native Shift+Tab did not restore the title focus.')
            script='# Native chapter\nNarrator: Exact original narration.\nGuest: A separate speaker line.'
            studio.title_text.SetValue('Native saved audio project')
            studio.script.SetValue(script)
            studio.syntax.SetValue(True)
            check('native preparation executes real scene parsing',studio.prepare())
            check('native list controls contain prepared chapters and roles',
                  studio.timeline.GetCount()==1 and studio.speaker.GetCount()==2
                  and studio.session.project.interpret_speaker_syntax is True)
            studio.speaker.SetSelection(1)
            event=wx.CommandEvent(wx.EVT_CHOICE.typeId,studio.speaker.GetId())
            event.SetEventObject(studio.speaker)
            check('native speaker selection dispatches its bound handler',studio.speaker.GetEventHandler().ProcessEvent(event))
            studio.voice.SetSelection(voices.index(selected))
            studio.pan.SetValue(.5)
            studio.rate.SetValue(1.1)
            studio.volume.SetValue(.7)
            event=wx.CommandEvent(wx.EVT_BUTTON.typeId,studio.apply_voice.GetId())
            event.SetEventObject(studio.apply_voice)
            check('native Apply voice dispatches its bound handler',studio.apply_voice.GetEventHandler().ProcessEvent(event))
            role=studio.session.project.roles[1]
            check('native typed controls preserve assigned voice and performance',
                  role.provider=='sapi5' and role.voice==selected.id
                  and role.pan==.5 and role.rate==1.1 and role.volume==.7)
            project_root=root/'saved audio project'
            # Explicit synthetic destination replaces only the manual file-picker
            # answer. Actual accelerator routing, save service and disk I/O run.
            studio.choose_new_folder=lambda: project_root
            studio.script.SetFocus()
            pump(lambda: focused(studio.script),'Editor did not receive native focus for Ctrl+S.')
            simulator=wx.UIActionSimulator()
            try:
                check('native Ctrl+S key-down sequence accepted',
                      simulator.KeyDown(wx.WXK_CONTROL) and simulator.KeyDown(ord('S')))
            finally:
                simulator.KeyUp(ord('S'))
                simulator.KeyUp(wx.WXK_CONTROL)
            pump(lambda: (project_root/'project.json').is_file() and not studio.session.dirty,
                 'Native Ctrl+S did not save the current project.')
            loaded=ProjectStore(project_root).load().project
            check('native Ctrl+S saved exact author text and speaker choices',
                  loaded.source_text==script and loaded.roles[1]==role)
            pump(lambda: focused(studio.status),'Save confirmation did not receive accessible focus.')
            check('save status states actual persisted success','Project saved:' in studio.status.GetValue())
            changed=script+'\nGuest: A second saved version.'
            studio.script.SetValue(changed)
            check('editing marks the native project dirty',studio.session.dirty)
            check('native Save button persists the next revision',studio.save())
            reopened=ProjectStore(project_root).load().project
            check('reopening sees the saved revision without source rewriting',
                  reopened.source_text==changed and not studio.session.dirty
                  and reopened.roles[1]==role)
            check('native validation focuses a readable failure and preserves the script',
                  _validate_preserves(studio,script))
            studio.script.ChangeValue(changed)
            studio.session.dirty=False
            print(json.dumps({'status':'PASS','checks':len(checks),'platform':platform.system(),
                'wx':wx.version(),'studio_sha256':sha256(installed.read_bytes()).hexdigest(),
                'sapi5_worker_sha256':sha256(worker_bytes).hexdigest(),
                'sapi5_waveform':{'status':'PASS','voice_identity':selected.identity,
                                  'sample_rate':result.sample_rate,'frames':result.frames,'sha256':result.sha256},
                'native_keyboard':'Tab/Shift+Tab navigation events and Ctrl+S simulated key events',
                'manual_keyboard':'NOT_RUN','manual_file_picker':'NOT_RUN','hardware_playback':'NOT_RUN',
                'listening':'NOT_RUN','nvda':'NOT_RUN'},sort_keys=True),flush=True)
        finally:
            if studio is not None and studio.alive:
                studio.alive=False
                studio.Destroy()
            if frame is not None:
                frame._closing=True
                frame.executor.shutdown(wait=True,cancel_futures=True)
                frame.Destroy()
            gui.Yield()
            if native_runtime is not None:
                native_runtime.close()
    return 0


def _validate_preserves(studio,original):
    # This invokes the real UI failure path with no modal confirmation.
    studio.script.ChangeValue(original)
    studio.title_text.ChangeValue('')
    success=studio.prepare()
    studio.title_text.ChangeValue('Native saved audio project')
    import wx
    focus=wx.Window.FindFocus()
    return success is False and studio.script.GetValue()==original and focus is studio.status and bool(studio.status.GetValue())


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'status':'FAIL','error':traceback.format_exc(),
                          'nvda':'NOT_RUN','hardware_playback':'NOT_RUN'}),flush=True)
        raise SystemExit(1)
