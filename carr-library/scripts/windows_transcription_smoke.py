"""Installed native wx transcription smoke with an explicitly synthetic worker.

This validates UI, process protocol, private state and progress integration. It
is not acoustic model qualification, a manual-keyboard test or an NVDA test.
Run with the installed interpreter: python -I -B scripts/windows_transcription_smoke.py.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
from types import SimpleNamespace


SYNTHETIC_WORKER = r'''
import hashlib,json,os,pathlib,sys,uuid
root=pathlib.Path(sys.argv[sys.argv.index('--job-dir')+1])
config=json.loads((root/'job_config.json').read_text(encoding='utf-8'))
results=pathlib.Path(config['results_root']);results.mkdir(parents=True,exist_ok=True)
run_id=uuid.uuid4().hex
files=[]
for index,name in enumerate(config['files']):
    source=pathlib.Path(name);raw=source.read_bytes();artifacts=[]
    for fmt in ('txt','srt','vtt','json'):
        if not config['settings']['output_'+fmt]:continue
        data=('Synthetic native transcript '+str(index)+'\n').encode('utf-8')
        if fmt=='srt':data=b'1\n00:00:00,000 --> 00:00:01,000\nSynthetic native fixture\n'
        if fmt=='vtt':data=b'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSynthetic native fixture\n'
        if fmt=='json':data=b'{"text":"Synthetic native fixture","segments":[]}'
        path='output-'+str(index)+'.'+fmt;(results/path).write_bytes(data)
        artifacts.append({'path':path,'format':fmt,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    files.append({'source':{'path':str(source),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)},
      'state':'succeeded','cache_key':hashlib.sha256(str(index).encode()).hexdigest(),'error':None,
      'warnings':['Synthetic protocol fixture; acoustic quality is not tested.'],'artifacts':artifacts})
value={'schema_version':1,'protocol':'carr-asr-v43-hardened-1','job_id':config['job_id'],'run_id':run_id,
  'state':'succeeded','done':len(files),'failed':0,'total':len(files),'effective_parallel_files':1,
  'results_dir':str(results),'files':files,'artifacts':[a for f in files for a in f['artifacts']],'errors':[]}
(root/'job_result.json').write_text(json.dumps(value),encoding='utf-8')
(root/'job_status.json').write_text(json.dumps({'schema_version':1,'protocol':value['protocol'],'job_id':config['job_id'],
  'run_id':run_id,'phase':'terminal','result_state':'succeeded','done':len(files)}),encoding='utf-8')
'''


def main():
    if os.name != 'nt':
        print(json.dumps({'result':'NOT_RUN','reason':'A native Windows session is required.','nvda':'NOT_RUN'}),flush=True)
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library import transcription
    from carr_library.ui import transcription_dialog as ui
    from carr_library.ui.desktop_app import DesktopFrame

    errors, checks = [], []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window,wx.Dialog) and window.IsModal():window.EndModal(wx.ID_CANCEL)
            return False
    gui=ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    def check(name, condition):
        if not condition:raise AssertionError(name)
        checks.append(name);print('PASS:',name,flush=True)
    def until(predicate, timeout=15):
        deadline=monotonic()+timeout
        while not predicate():
            if errors:raise RuntimeError('\n'.join(errors))
            if monotonic()>deadline:raise TimeoutError('Native transcription operation timed out.')
            gui.Yield();sleep(.01)
    def press(button):
        top = wx.GetTopLevelParent(button)
        top.Raise(); user32.SetForegroundWindow(top.GetHandle()); button.SetFocus()
        until(lambda: wx.Window.FindFocus() is button and user32.GetForegroundWindow() == top.GetHandle())
        check('native Space down: ' + button.GetLabel(), simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield(); sleep(.04)
        finally:
            check('native Space up: ' + button.GetLabel(), simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()
    source=Path(ui.__file__).resolve()
    check('transcription UI loads from the installed package','site-packages' in {part.casefold() for part in source.parts})
    check('wx uses real Windows controls',wx.Platform=='__WXMSW__')
    admitted=transcription.REVIEWED_WORKER_SHA256
    frame=dialog=None
    with TemporaryDirectory(prefix='resonance-native-transcription-') as temporary:
        root=Path(temporary)
        try:
            library=LibraryApp(root/'library')
            workspace=library.create_workspace('Native transcription research')
            library.add_text(workspace,'Retained research original','Exact original research quotation.')
            frame=DesktopFrame(library);frame.Show();gui.Yield()
            folder = root / 'Explicit selected audio'; folder.mkdir()
            originals = {'a.wav': b'First synthetic native audio input',
                         'b.wav': b'Second synthetic native audio input'}
            for name, raw in originals.items(): (folder / name).write_bytes(raw)
            (folder / 'unsupported.txt').write_text('Disclose this skipped source', encoding='utf-8')
            (folder / 'Excluded subfolder').mkdir()
            picker_calls = []
            class SelectedFolder:
                """Test-only deterministic selection; the OS picker is not qualified."""
                def __init__(self, *args, **kwargs): picker_calls.append(True)
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(folder)
                def Destroy(self): pass
            selected_wx = SimpleNamespace(**vars(wx))
            selected_wx.DirDialog = SelectedFolder
            dialog=ui.create_transcription_dialog(frame, wx_module=selected_wx)
            check('missing engine is visibly waiting for input','waiting-input' in dialog.profile_status.GetLabel())
            check('transcript editor is a named native TextCtrl',isinstance(dialog.editor,wx.TextCtrl)
                  and 'derived revision' in dialog.editor.GetName())
            check('pause and export are unavailable before a job',not dialog.buttons['pause'].IsEnabled()
                  and not dialog.buttons['export'].IsEnabled())
            worker=root/'synthetic_worker.py';worker.write_text(SYNTHETIC_WORKER,encoding='utf-8')
            digest=sha256(worker.read_bytes()).hexdigest()
            # Test-only admission of exactly this synthetic fixture; reset below.
            transcription.REVIEWED_WORKER_SHA256=admitted|{digest}
            models=root/'synthetic_models';(models/'tiny.en').mkdir(parents=True)
            (models/'tiny.en'/'model.bin').write_bytes(b'Synthetic fixture; no acoustic weights')
            spec=transcription.EngineSpec(str(Path(sys.executable).resolve()),str(worker),digest,str(models))
            dialog.service.configure_engine(spec);dialog.refresh_capability()
            selected_before=frame.active
            scenario_errors=[]
            def scenario():
                try:
                    check('folder selection has named native controls and recursion starts off',
                          isinstance(dialog.buttons['folder'], wx.Button) and not dialog.recursive.GetValue()
                          and dialog.recursive.GetName() == 'Include subfolders when choosing a folder')
                    press(dialog.buttons['folder'])
                    until(lambda: bool(picker_calls) and not frame.working and not dialog.busy)
                    check('native folder action reviews only the selected finite sorted list',
                          [path.name for path in dialog.files] == ['a.wav', 'b.wav']
                          and dialog.current_job is None and dialog.service.list() == [])
                    check('unsupported sources and excluded subfolders are disclosed',
                          'unsupported.txt' in dialog.status.GetValue() and 'Subfolder excluded' in dialog.status.GetValue())
                    press(dialog.buttons['run'])
                    until(lambda: dialog.current_job is not None and not frame.working and not dialog.busy)
                    job=dialog.service.get(dialog.current_job)
                    check('native shared folder job completes only after eight verified exports',job['state']=='succeeded'
                          and len(job['inputs']) == 2 and len(job['receipt']['artifacts'])==8)
                    check('shared OperationProgress confirms durable completion',frame.operation_progress.event.state=='succeeded'
                          and frame.operation_progress.event.percent==100)
                    check('source audio bytes and research scope remain unchanged',
                          all((folder / name).read_bytes() == raw for name, raw in originals.items())
                          and frame.active==selected_before)
                    check('worker warning and serial restart policy are readable','Synthetic protocol fixture' in dialog.status.GetValue()
                          and 'One file' in dialog.status.GetValue() and '24-hour' in dialog.status.GetValue())
                    item=dialog.artifacts[dialog.artifact_choice.GetSelection()]
                    transcript=dialog.editor.GetValue()
                    dialog.editor.SetValue('Edited native derived transcript.')
                    dialog.on_revision();until(lambda:not frame.working and not dialog.busy)
                    check('native edit creates a new revision without changing worker output',bool(dialog.revision)
                          and dialog.service.read_transcript(dialog.current_job,item['path'])==transcript)
                    exported=dialog.service.export(dialog.current_job,item['path'],root/'new-derived.txt',revision_id=dialog.revision)
                    check('new export contains the selected saved revision',exported.read_text(encoding='utf-8')=='Edited native derived transcript.')
                    try:
                        dialog.service.export(dialog.current_job,item['path'],exported)
                    except FileExistsError:
                        pass
                    else:
                        raise AssertionError('existing export was overwritten')
                    check('existing export remains unchanged',exported.read_text(encoding='utf-8')=='Edited native derived transcript.')
                except Exception:
                    scenario_errors.append(traceback.format_exc())
                finally:
                    if dialog.IsModal():dialog.EndModal(wx.ID_OK if not scenario_errors else wx.ID_CANCEL)
            watchdog=wx.CallLater(45000,lambda:dialog.EndModal(wx.ID_CANCEL) if dialog.IsModal() else None)
            wx.CallAfter(scenario)
            try:result=dialog.ShowModal()
            finally:watchdog.Stop()
            if scenario_errors or errors:raise RuntimeError('\n'.join(scenario_errors+errors))
            check('native modal closes normally',result==wx.ID_OK)
            print(json.dumps({'result':'PASS','checks':len(checks),'platform':platform.system(),'wx':wx.version(),
                'installed_ui_sha256':sha256(source.read_bytes()).hexdigest(),'worker':'explicitly synthetic protocol fixture',
                'folder_selection':'native Space action, deterministic fixture picker and real background scan',
                'acoustic_quality':'NOT_TESTED','nvda':'NOT_RUN','manual_keyboard':'NOT_RUN','native_file_pickers':'NOT_RUN'},sort_keys=True),flush=True)
        finally:
            transcription.REVIEWED_WORKER_SHA256=admitted
            if dialog is not None:
                dialog.cancel.set();dialog.alive=False;dialog.Destroy()
            if frame is not None:
                frame.cancel_event.set();frame._closing=True
                frame.executor.shutdown(wait=True,cancel_futures=True);frame.Destroy()
            gui.Yield()
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print(json.dumps({'result':'FAIL','error':traceback.format_exc(),'nvda':'NOT_RUN'}),flush=True)
        raise SystemExit(1)
