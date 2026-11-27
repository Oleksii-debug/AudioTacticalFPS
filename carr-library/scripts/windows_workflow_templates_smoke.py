"""Actual semantic template key, parameter review and typed project controls."""
import argparse
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    faulthandler.enable(all_threads=True)
    try:
        return _native(args.installed)
    finally:
        if handle is not None: handle.close()


def _native(installed):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Scope
    from carr_library.assistant.templates import TemplateStore, WorkflowTemplate, instantiate_plan
    from carr_library.studio.project import parse_script
    from carr_library.ui.assistant_dialog import install_assistant_actions
    from carr_library.ui.keyboard_settings import install_keyboard_settings
    from carr_library.ui.workflow_templates import edit_template
    import carr_library.assistant.templates as templates_module
    import carr_library.assistant.template_bindings as bindings_module
    import carr_library.assistant.planner as planner_module
    import carr_library.ui.workflow_templates as controls_module
    import carr_library.ui.assistant_dialog as assistant_module

    checks, failures, identities = [], [], {}
    def check(label, condition):
        if not condition: raise AssertionError(label)
        if label not in checks:
            checks.append(label); print('PASS:', label, flush=True)
    checkout = Path(__file__).resolve().parents[1]
    installed_root = Path(os.environ.get('CARR_PROGRAM_DIR') or sysconfig.get_path('purelib')).resolve()
    for module in (templates_module, bindings_module, planner_module, controls_module, assistant_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('exact source: ' + module.__name__, digest == sha256(expected.read_bytes()).hexdigest())
        if installed:
            check('installed origin: ' + module.__name__,
                  actual.is_relative_to(installed_root) and not actual.is_relative_to(checkout))
        identities[module.__name__] = digest
    check('actual wxMSW controls', wx.Platform == '__WXMSW__')
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc()); return True
    app = ProbeApp(False); app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    with TemporaryDirectory(prefix='resonance-template-native-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native template review')
        store = TemplateStore(library.root)
        recipe = WorkflowTemplate.define('Native parameter macro', [
            {'tool': 'library.search', 'arguments': {'query': {'$parameter': 'query'}}}],
            {'query': {'kind': 'text', 'label': 'Query', 'example': 'Example only'}},
            kind='keyboard-macro')
        store.save(recipe, expected_token=None)
        frame = wx.Frame(None, title='Native semantic template acceptance', size=(660, 390))
        frame.library, frame.active, frame._closing = library, (workspace,), False
        frame.report = lambda value: print('FRAME:', value, flush=True)
        frame.menu_bar = wx.MenuBar(); frame.SetMenuBar(frame.menu_bar)
        install_assistant_actions(frame)
        manager = install_keyboard_settings(frame)
        check('macro registered once with no default key',
              manager.registry.get(recipe.command_id).default_shortcut == '')
        manager.apply_overrides({recipe.command_id: 'Ctrl+Alt+F9'})
        target = wx.Button(frame, label='Native keyboard focus target')
        frame.Show(); app.SetTopWindow(frame)
        simulator = wx.UIActionSimulator()
        phase, deadline, held_dialog = ['start'], monotonic() + 80, [None]
        timer = wx.Timer(frame)

        def click(button):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            button.GetEventHandler().ProcessEvent(event)

        def button(dialog, caption):
            def children(window):
                for child in window.GetChildren():
                    yield child
                    yield from children(child)
            return next(item for item in children(dialog) if isinstance(item, wx.Button)
                        and item.GetLabel().replace('&', '') == caption)

        def dialogs(title):
            return next((window for window in wx.GetTopLevelWindows()
                if isinstance(window, wx.Dialog) and window.IsShown() and window.GetTitle() == title), None)

        def abort(detail):
            failures.append(detail); print('FAIL:', detail, flush=True)
            timer.Stop()
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            wx.CallAfter(frame.Destroy)

        def project_editor():
            try:
                selected = parse_script('Original project template words.', project_id='old-native-project')
                raw = selected.to_dict(); raw['scenes'][0]['lines'][0]['source_ref'] = 'old-page-ref'
                from carr_library.studio.project import Project
                selected = Project.from_dict(raw)
                initial = WorkflowTemplate.from_project('Native project capture', selected, tool='audio.render')
                phase[0] = 'project'
                result = edit_template(frame, initial)
                if failures: return
                check('native typed project settings accepted', result is not None and 'words' in result.parameters)
                output = root / 'chosen-output'; output.mkdir()
                plan = instantiate_plan(result, Scope(output_root=output), {'words': 'New reviewed performance words.'})
                script = plan.steps[0].arguments['script']
                check('project reuse has fresh identity and no old source claim',
                    script['id'] not in ('template', selected.id) and script['revision'] == 1
                    and script['scenes'][0]['lines'][0]['source_ref'] == ''
                    and script['scenes'][0]['lines'][0]['text'] == 'New reviewed performance words.')
                check('project and template controls execute no job',
                      selected.scenes[0].lines[0].source_ref == 'old-page-ref'
                      and AssistantService(library).jobs() == ())
                phase[0] = 'done'; timer.Stop(); wx.CallAfter(frame.Destroy)
            except Exception:
                abort(traceback.format_exc())

        def tick(event):
            try:
                if monotonic() > deadline: raise TimeoutError('Native workflow controls did not finish.')
                if phase[0] == 'start':
                    frame.Raise(); target.SetFocus()
                    if wx.Window.FindFocus() is not target: return
                    phase[0] = 'macro'
                    check('actual semantic key dispatched', simulator.Char(wx.WXK_F9, wx.MOD_CONTROL | wx.MOD_ALT))
                    return
                if phase[0] == 'macro':
                    dialog = dialogs('Review workflow values and batch items')
                    if dialog is None: return
                    held_dialog[0] = dialog
                    fields = dialog.workflow_fields
                    check('macro opens empty fresh value review', fields['values'].GetValue() == '{}'
                          and AssistantService(library).jobs() == ())
                    fields['values'].ChangeValue('{"query":"base"}')
                    fields['items'].ChangeValue('[{"query":"First"},{"query":"Second"}]')
                    phase[0] = 'assistant'
                    click(button(dialog, 'Load new review')); return
                if phase[0] == 'assistant':
                    dialog = dialogs('Local assistant — review and run a bounded plan')
                    if dialog is None: return
                    controls = dialog.assistant_controls
                    check('loaded macro retains no folder approvals and no Run approval',
                        controls['input'].GetValue() == '' and controls['output'].GetValue() == ''
                        and not controls['full_backup'].GetValue() and not controls['buttons']['run'].IsEnabled())
                    click(controls['buttons']['preview'])
                    check('separate native Preview validates both explicit items without executing',
                        controls['buttons']['run'].IsEnabled()
                        and len(json.loads(controls['instruction'].GetValue())['items']) == 2
                        and dialog.assistant_service.jobs() == ())
                    phase[0] = 'closed-assistant'
                    click(controls['buttons']['close'])
                    return
                if phase[0] == 'closed-assistant':
                    if dialogs('Local assistant — review and run a bounded plan') is not None: return
                    phase[0] = 'opening-project'
                    wx.CallAfter(project_editor); return
                if phase[0] == 'project':
                    dialog = dialogs('Typed workflow template settings')
                    if dialog is None: return
                    steps = json.loads(dialog.workflow_fields['steps'].GetValue())
                    check('native project editor strips stored source references',
                        steps[0]['arguments']['script']['scenes'][0]['lines'][0]['source_ref'] == '')
                    steps[0]['arguments']['script']['scenes'][0]['lines'][0]['text'] = {'$parameter': 'words'}
                    dialog.workflow_fields['steps'].ChangeValue(json.dumps(steps))
                    dialog.workflow_fields['parameters'].ChangeValue(json.dumps({
                        'words': {'kind': 'text', 'label': 'New words', 'example': 'Explicit example'}}))
                    phase[0] = 'accepted-project'
                    click(button(dialog, 'Use these template settings'))
            except Exception:
                abort(traceback.format_exc())
        frame.Bind(wx.EVT_TIMER, tick, timer); timer.Start(80)
        app.MainLoop()
        report = {'result': 'PASS' if not failures and phase[0] == 'done' else 'FAIL',
                  'checks': checks, 'errors': failures, 'source_sha256': identities,
                  'manual_nvda': 'NOT_RUN', 'acoustic_quality': 'NOT_RUN'}
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
