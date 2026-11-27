"""A native review/run surface for deterministic local tools and optional planners."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..assistant.workflows import LocalAssistant, Scope, guided_goal, WorkflowPlan
from ..assistant.planner import available_local_planners, propose_local_plan
from .provider_settings import profile_label


class AssistantDialog:
    def __init__(self,frame):
        import wx
        self.wx,self.frame=wx,frame
        self.library=frame.library
        self.planner_load_error=None
        try:
            self.planner_profiles=available_local_planners(self.library.root)
        except Exception:
            self.planner_profiles=()
            self.planner_load_error='Connection settings could not be read. The deterministic guide remains available.'
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
        layout.Add(wx.StaticText(self.dialog,label='Plan preparation'),0,wx.LEFT|wx.RIGHT,8)
        self.planner_choice=wx.Choice(self.dialog,choices=['Deterministic guide — no model connection']+
                                      [profile_label(profile) for profile in self.planner_profiles])
        self.planner_choice.SetName('Plan preparation: deterministic guide or explicitly selected local chat profile')
        self.planner_choice.SetSelection(0)
        self.planner_choice.Bind(wx.EVT_CHOICE,self.planner_changed)
        layout.Add(self.planner_choice,0,wx.EXPAND|wx.ALL,8)
        self.planner_details=wx.TextCtrl(self.dialog,style=wx.TE_MULTILINE|wx.TE_READONLY)
        self.planner_details.SetName('Selected local planner destination and transmitted data scope')
        self.planner_details.SetMinSize((-1,65))
        layout.Add(self.planner_details,0,wx.EXPAND|wx.LEFT|wx.RIGHT,8)
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
        self.controls=(build,choose,self.goal,self.script,self.language,self.planner_choice)
        for control in (self.goal,self.script,self.language):
            control.Bind(wx.EVT_TEXT,self.changed)
        self.planner_changed()
        if self.planner_load_error:
            self.say(self.planner_load_error)
        self.goal.SetFocus()

    def planner_changed(self,event=None):
        self.changed()
        index=self.planner_choice.GetSelection()
        if index==0:
            details='Deterministic guide selected. Preview prepares local typed steps without contacting a model. '
            details+='Optional loopback profiles can be added under Tools > API and local model connections.'
        elif 1<=index<=len(self.planner_profiles):
            profile=self.planner_profiles[index-1]
            details=f'LOCAL_ONLY configured endpoint: {profile.endpoint}\nModel: {profile.model}; account ID: {profile.id}. '
            details+='Preview sends your goal and selected workspace/folder identifiers to this local server. '
            details+='The authored script is represented by its length and hash, not uploaded to the planner. '
            details+='No tool runs before the separate Run action. The server is user-managed; its own network behavior is not an OS sandbox.'
        else:
            details='Select the deterministic guide or a saved local chat profile.'
        self.planner_details.ChangeValue(details)
        if event:
            self.planner_details.SetFocus()

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
        if self.busy:
            return
        try:
            if self.frame.library is not self.library:
                raise ValueError('The library folder changed. Reopen the assistant.')
            scope=Scope(workspaces=(self.workspace,) if self.workspace else (),output_root=self.output_root)
            self.assistant=LocalAssistant(self.library,scope)
            output=self.output_root/('assistant-audio-'+uuid4().hex[:8]) if self.output_root else None
            index=self.planner_choice.GetSelection()
            if index!=0:
                if not 1<=index<=len(self.planner_profiles):
                    raise ValueError('Select a saved local chat profile or the deterministic guide.')
                self._prepare_model(self.assistant,self.planner_profiles[index-1],output)
                return
            self.plan=guided_goal(self.goal.GetValue(),script=self.script.GetValue(),output=output,
                                  workspace=self.workspace,language=self.language.GetValue())
            self.assistant.prepare(self.plan)
            self._display_plan()
        except Exception as exc:
            self.plan=None
            self.run_button.Disable()
            self.say('Plan not ready: '+str(exc),True)

    def _display_plan(self,*,from_model=False):
        details=[('Validated local-model proposal: ' if from_model else 'Local-only workflow: ')+self.plan.title,
            'The selected library and output folder are the only authorized scope. No tool has run.']
        for index,step in enumerate(self.plan.steps,1):
            args={key:(f'{len(value)} authored script characters; original text remains visible in the script field'
                if key=='script' and (not from_model or value==self.script.GetValue()) else value)
                for key,value in step.arguments.items()}
            details.append(f'{index}. {step.tool}\n'+json.dumps(args,ensure_ascii=False,indent=2))
        self.preview.ChangeValue('\n\n'.join(details))
        self.run_button.Enable()
        self.say('Proposal saved for review. Read the exact typed actions, then Run to execute them. '
                 'Model text is a proposal, not evidence that work is complete.' if from_model else
                 'Plan saved for review. Run executes these actual local tools.',True)

    def _prepare_model(self,assistant,profile,output):
        goal,script,language=self.goal.GetValue(),self.script.GetValue(),self.language.GetValue()
        self.plan=None
        self.busy=True
        self.cancelled.clear()
        for control in (*self.controls,self.run_button):
            control.Disable()
        self.cancel_button.SetLabel('Cancel plan request')
        self.cancel_button.Enable()
        self.preview.ChangeValue('Waiting for the explicitly selected local planner. No tool operations have run.')
        self.say('Requesting one plan from the configured loopback endpoint; scope and tools will be validated before review.',True)
        try:
            future=self.executor.submit(lambda:propose_local_plan(assistant,profile,goal,script=script,
                language=language,output=output,cancelled=self.cancelled.is_set))
        except Exception:
            self.busy=False
            for control in self.controls:
                control.Enable()
            self.cancel_button.Disable()
            self.cancel_button.SetLabel('&Pause workflow')
            raise
        def finish(future):
            if self.closed:
                return
            self.busy=False
            for control in self.controls:
                control.Enable()
            self.cancel_button.Disable()
            self.cancel_button.SetLabel('&Pause workflow')
            try:
                plan=future.result()
                if self.cancelled.is_set():
                    raise InterruptedError('Local planning was cancelled; no workflow was authorized.')
                if self.frame.library is not self.library:
                    raise ValueError('The library folder changed. Prepare a new plan.')
                assistant.prepare(plan)
                self.plan,self.assistant=plan,assistant
                self._display_plan(from_model=True)
            except Exception as exc:
                self.plan=None
                self.run_button.Disable()
                self.say('Local model plan not ready: '+str(exc),True)
        future.add_done_callback(lambda done:self.wx.CallAfter(finish,done) if not self.closed else None)

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
    saved = menu.Append(wx.ID_ANY, 'Saved tool plans and jobs...\tCtrl+Alt+J')
    def show_saved(event):
        if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
            frame.report('Wait for the current operation or choose Cancel current operation.')
            return
        from .assistant_dialog import show_assistant
        try:
            show_assistant(frame)
        except Exception as exc:
            frame.report('Saved tool plans could not open: ' + str(exc))
    frame.Bind(wx.EVT_MENU, show_saved, saved)
    frame.menu_bar.Insert(max(0,frame.menu_bar.GetMenuCount()-1),menu,'&Tools')
