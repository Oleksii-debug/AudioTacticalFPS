"""A native review/run surface for deterministic local tools and optional planners."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..assistant.workflows import LocalAssistant, Scope, guided_goal, WorkflowPlan


class AssistantDialog:
    def __init__(self,frame):
        import wx
        self.wx,self.frame=wx,frame
        self.library=frame.library
        try:
            self.workspace=frame._workspace()
        except ValueError:
            self.workspace=None
        self.dialog=wx.Dialog(frame,title='Local assistant — review and run',size=(900,760),
                              style=wx.DEFAULT_DIALOG_STYLE|wx.RESIZE_BORDER)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='local-assistant')
        self.cancelled=threading.Event()
        self.closed=self.busy=False
        self.plan=self.assistant=None
        self.output_root=None
        layout=wx.BoxSizer(wx.VERTICAL)
        intro=wx.TextCtrl(self.dialog,value='Describe audiobook production or search of the selected library. '
            'This offline guide uses typed local tools. It does not run shell commands or upload your books. '
            'Preview the exact plan, then choose Run. General language-model planning requires a separately configured local model.',
            style=wx.TE_MULTILINE|wx.TE_READONLY)
        intro.SetName('Assistant capabilities and local processing scope')
        intro.SetMinSize((-1,75))
        layout.Add(intro,0,wx.EXPAND|wx.ALL,8)
        layout.Add(wx.StaticText(self.dialog,label='Goal, for example: озвуч сценарій; or find railway'),0,wx.ALL,6)
        self.goal=wx.TextCtrl(self.dialog)
        self.goal.SetName('Goal for local assistant')
        layout.Add(self.goal,0,wx.EXPAND|wx.LEFT|wx.RIGHT,8)
        layout.Add(wx.StaticText(self.dialog,label='Authored narration script; empty for search'),0,wx.ALL,6)
        self.script=wx.TextCtrl(self.dialog,style=wx.TE_MULTILINE)
        self.script.SetName('Narration script provided by the author')
        layout.Add(self.script,1,wx.EXPAND|wx.LEFT|wx.RIGHT,8)
        language_row=wx.BoxSizer(wx.HORIZONTAL)
        language_row.Add(wx.StaticText(self.dialog,label='Narration language code'),0,wx.ALIGN_CENTER_VERTICAL|wx.RIGHT,6)
        self.language=wx.TextCtrl(self.dialog,value='en',size=(100,-1))
        self.language.SetName('Narration language code')
        language_row.Add(self.language,0,wx.RIGHT,8)
        choose=wx.Button(self.dialog,label='Choose output &folder...')
        choose.Bind(wx.EVT_BUTTON,self.choose_output)
        language_row.Add(choose)
        layout.Add(language_row,0,wx.ALL,8)
        self.preview=wx.TextCtrl(self.dialog,style=wx.TE_MULTILINE|wx.TE_READONLY)
        self.preview.SetName('Exact workflow preview and verified tool results')
        layout.Add(self.preview,1,wx.EXPAND|wx.ALL,8)
        buttons=wx.WrapSizer(wx.HORIZONTAL)
        build=wx.Button(self.dialog,label='&Preview plan')
        build.Bind(wx.EVT_BUTTON,self.prepare)
        self.run_button=wx.Button(self.dialog,label='&Run reviewed local plan')
        self.run_button.Bind(wx.EVT_BUTTON,self.run)
        self.run_button.Disable()
        self.cancel_button=wx.Button(self.dialog,label='&Pause workflow')
        self.cancel_button.Bind(wx.EVT_BUTTON,lambda event:self.cancelled.set())
        self.cancel_button.Disable()
        close=wx.Button(self.dialog,label='Close')
        close.Bind(wx.EVT_BUTTON,self.close)
        for button in (build,self.run_button,self.cancel_button,close):
            buttons.Add(button,0,wx.ALL,4)
        layout.Add(buttons,0,wx.ALL,4)
        self.status=wx.TextCtrl(self.dialog,style=wx.TE_MULTILINE|wx.TE_READONLY)
        self.status.SetName('Assistant status and errors')
        self.status.SetMinSize((-1,65))
        layout.Add(self.status,0,wx.EXPAND|wx.ALL,8)
        self.dialog.SetSizer(layout)
        self.dialog.Bind(wx.EVT_CLOSE,self.close)
        self.controls=(build,choose,self.goal,self.script,self.language)
        for control in (self.goal,self.script,self.language):
            control.Bind(wx.EVT_TEXT,self.changed)
        self.goal.SetFocus()

    def changed(self,event=None):
        self.plan=None
        self.run_button.Disable()
        if event:
            event.Skip()

    def say(self,message,focus=False):
        if self.closed:
            return
        self.status.ChangeValue(message)
        if focus:
            self.status.SetFocus()

    def choose_output(self,event=None):
        dialog=self.wx.DirDialog(self.dialog,'Select the folder where this workflow may create a new output folder')
        try:
            if dialog.ShowModal()==self.wx.ID_OK:
                self.output_root=Path(dialog.GetPath()).resolve()
                self.changed()
                self.say('Selected output scope: '+str(self.output_root),True)
        finally:
            dialog.Destroy()

    def prepare(self,event=None):
        try:
            if self.frame.library is not self.library:
                raise ValueError('The library folder changed. Reopen the assistant.')
            scope=Scope(workspaces=(self.workspace,) if self.workspace else (),output_root=self.output_root)
            self.assistant=LocalAssistant(self.library,scope)
            output=self.output_root/('assistant-audio-'+uuid4().hex[:8]) if self.output_root else None
            self.plan=guided_goal(self.goal.GetValue(),script=self.script.GetValue(),output=output,
                                  workspace=self.workspace,language=self.language.GetValue())
            self.assistant.prepare(self.plan)
            details=['Local-only workflow: '+self.plan.title,
                'The selected library and output folder are the only authorized scope.']
            for index,step in enumerate(self.plan.steps,1):
                args={key:(f'{len(value)} script characters' if key=='script' else value) for key,value in step.arguments.items()}
                details.append(f'{index}. {step.tool}\n'+json.dumps(args,ensure_ascii=False,indent=2))
            self.preview.ChangeValue('\n\n'.join(details))
            self.run_button.Enable()
            self.say('Plan saved for review. Run executes these actual local tools.',True)
        except Exception as exc:
            self.plan=None
            self.run_button.Disable()
            self.say('Plan not ready: '+str(exc),True)

    def run(self,event=None):
        if self.busy or self.plan is None:
            return
        if self.frame.library is not self.library:
            self.say('The library folder changed. Review a new plan.',True)
            return
        plan,assistant=self.plan,self.assistant
        self.busy=True
        self.cancelled.clear()
        for control in (*self.controls,self.run_button):
            control.Disable()
        self.cancel_button.Enable()
        def progress(value):
            if not self.closed:
                self.wx.CallAfter(self.say,f'Step {value["step"]} of {value["total"]}: {value["tool"]}')
        future=self.executor.submit(lambda:assistant.run(plan.id,approved_fingerprint=plan.fingerprint,
                                   cancelled=self.cancelled.is_set,progress=progress))
        future.add_done_callback(lambda done:self.wx.CallAfter(self.finished,done) if not self.closed else None)

    def finished(self,future):
        if self.closed:
            return
        self.busy=False
        for control in self.controls:
            control.Enable()
        self.cancel_button.Disable()
        try:
            result=future.result()
            summaries={key:value for key,value in result['steps'].items()}
            self.preview.ChangeValue(json.dumps(summaries,ensure_ascii=False,indent=2))
            self.say('Workflow complete. The panel contains actual saved tool results and output locations.',True)
        except Exception as exc:
            self.say('Workflow paused or needs attention: '+str(exc),True)
            self.run_button.Enable()

    def close(self,event=None):
        if self.busy:
            self.cancelled.set()
            self.say('Pausing the workflow. Close again after the worker stops.',True)
            if event and hasattr(event,'Veto'):
                event.Veto()
            return
        self.closed=True
        self.executor.shutdown(wait=False,cancel_futures=True)
        self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self):
        try:
            return self.dialog.ShowModal()
        finally:
            self.closed=True
            self.cancelled.set()
            self.executor.shutdown(wait=False,cancel_futures=True)
            self.dialog.Destroy()


def install_local_assistant(frame):
    import wx
    menu=wx.Menu()
    item=menu.Append(wx.ID_ANY,'&Local assistant...\tCtrl+Alt+A')
    frame.Bind(wx.EVT_MENU,lambda event:AssistantDialog(frame).show(),item)
    frame.menu_bar.Insert(max(0,frame.menu_bar.GetMenuCount()-1),menu,'&Tools')
