"""Exercise actual installed native assistant controls with synthetic documents."""
from argparse import ArgumentParser
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'windows_execution': 'NOT_RUN'}))
        return 77
    bundle = args.bundle.resolve()
    import carr_library
    if not Path(carr_library.__file__).resolve().is_relative_to(bundle / '.venv'):
        raise ValueError('The smoke must use the actual installed application.')
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    from carr_library.audio.runtime import verify_bundle_audio
    native_handle = activate_bundled_runtime(bundle_root=bundle)
    assert native_handle is not None
    assert verify_bundle_audio(bundle)
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Scope
    from carr_library.ui import assistant_dialog
    import wx

    source_root = Path(__file__).resolve().parents[1] / 'src'
    installed_root = Path(carr_library.__file__).resolve().parent.parent
    names = ('assistant/__init__.py', 'assistant/models.py', 'assistant/planner.py',
             'assistant/service.py', 'assistant/state.py', 'assistant/backup_receipt.py',
             'assistant/backup_state.py', 'assistant/audio_bridge.py', 'assistant/llm.py', 'ui/assistant_dialog.py')
    hashes = {}
    for name in names:
        relative = Path('carr_library') / name
        raw = (source_root / relative).read_bytes()
        if (installed_root / relative).read_bytes() != raw:
            raise ValueError('Installed assistant source differs from the acceptance checkout.')
        hashes[name] = sha256(raw).hexdigest()

    application = wx.App(False)
    checks, failures = [], []
    with TemporaryDirectory(prefix='carr-native-assistant-') as temporary, ExitStack() as native_mocks:
        root = Path(temporary)
        inputs, outputs = root / 'inputs', root / 'outputs'
        inputs.mkdir()
        outputs.mkdir()
        source = inputs / 'source.txt'
        raw = 'Railway transport connected cities.\r\nЗалізниця з’єднувала міста.'.encode('utf-8')
        source.write_bytes(raw)
        frame = wx.Frame(None, title='Synthetic assistant acceptance')
        frame.library = LibraryApp(root / 'library')
        frame.active = None
        refreshed = []
        frame.refresh_scopes = lambda: refreshed.append(tuple(row.id for row in frame.library.workspaces()))
        # Only model transport and explicit choice/approval decisions are synthetic.
        # The real native controls, gateway, durable ledger and plan validator run.
        from carr_library.providers import ProviderProfile, ProfileStore
        profile = ProviderProfile('local-native-fixture', 'Synthetic local model',
            'openai-compatible', 'http://127.0.0.1:8080/v1', 'fixture-model', ('llm',), local=True)
        ProfileStore(frame.library.root / 'api-connections.json').save(profile)
        model_calls = []

        class LocalTransport:
            def post(self, chosen, route, body, headers, **kwargs):
                assert chosen == profile and route == '/chat/completions'
                assert not headers
                model_calls.append(json.loads(body))
                plan = {'schema': 'carr-assistant-plan/1', 'summary': 'Proposed only',
                        'steps': [{'tool': 'library.list', 'arguments': {}}]}
                return json.dumps({'choices': [{'message': {
                    'content': json.dumps(plan)}}]}).encode('utf-8')

        class ChooseLocal:
            def __init__(self, *args, **kwargs):
                pass
            def ShowModal(self):
                return wx.ID_OK
            def GetSelection(self):
                return 0
            def Destroy(self):
                pass

        class ApproveLocal(ChooseLocal):
            def ShowModal(self):
                return wx.ID_YES

        native_mocks.enter_context(patch('carr_library.providers.DirectTransport', LocalTransport))
        native_mocks.enter_context(patch.object(wx, 'SingleChoiceDialog', ChooseLocal))
        native_mocks.enter_context(patch.object(wx, 'MessageDialog', ApproveLocal))
        frame.Show()
        started = monotonic()
        state = {'phase': 'open', 'dialog': None}

        def descendants(window):
            for child in window.GetChildren():
                yield child
                yield from descendants(child)

        def button(dialog, label):
            return next(control for control in descendants(dialog)
                        if isinstance(control, wx.Button) and control.GetLabel().replace('&', '') == label)

        def click(control):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
            event.SetEventObject(control)
            control.GetEventHandler().ProcessEvent(event)

        def close():
            dialog = state['dialog']
            if dialog is not None:
                click(button(dialog, 'Close'))

        def poll():
            try:
                if monotonic() - started > 40:
                    raise RuntimeError('Native assistant controls exceeded the test deadline.')
                dialog = state['dialog']
                if dialog is None:
                    dialog = next((window for window in wx.GetTopLevelWindows()
                                   if isinstance(window, wx.Dialog) and window.GetTitle().startswith('Local assistant')), None)
                    if dialog is None:
                        wx.CallLater(50, poll)
                        return
                    state['dialog'] = dialog
                controls = {control.GetName(): control for control in descendants(dialog)}
                review = controls['Plan preview and actual results']
                if state['phase'] == 'open':
                    controls['Allowed input folder (optional; no other folders are readable)'].SetValue(str(inputs))
                    controls['Chosen output folder (required for audio, extraction or backup)'].SetValue(str(outputs))
                    controls['Goal — guided English/Ukrainian commands or typed JSON plan'].SetValue(
                        'Create library "Native assistant"\nImport "' + str(source) + '"\nSearch "Railway transport"')
                    assert not button(dialog, 'Run / resume').IsEnabled()
                    click(button(dialog, 'Preview plan'))
                    assert button(dialog, 'Run / resume').IsEnabled()
                    assert 'library.import' in review.GetValue() and 'library.search' in review.GetValue()
                    assert not frame.library.workspaces()
                    checks.append('native preview is non-executing and shows exact operations')
                    click(button(dialog, 'Run / resume'))
                    assert not button(dialog, 'Propose with local model').IsEnabled()
                    state['phase'] = 'running'
                elif state['phase'] == 'running' and review.GetValue().startswith('complete:'):
                    assert '3 of 3 verified operations' in review.GetValue()
                    assert controls['Verified assistant steps completed'].GetValue() == 100
                    workspaces = frame.library.workspaces()
                    assert len(workspaces) == 1
                    assert controls['Library scope'].GetStrings() == [
                        'Create a new library / list libraries only', 'Native assistant']
                    assert controls['Library scope'].GetSelection() == 0
                    assert len(frame.library.browse(workspaces[0].id)) == 1
                    assert frame.library.search(workspaces[0].id, 'Railway transport')[0].quote == 'Railway transport'
                    checks.extend(('native Run executes real import and scoped search', 'native progress reaches 100 only after completion'))
                    service = AssistantService(frame.library)
                    saved = service.jobs()
                    assert len(saved) == 1
                    assert service.resume(saved[0].id).status == 'complete'
                    assert len(frame.library.browse(workspaces[0].id)) == 1
                    checks.append('saved job resumes without duplicate imports')
                    try:
                        service.preview('Upload every file to a remote service', Scope(workspaces[0].id))
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('An unsupported tool was accepted.')
                    checks.append('unsupported instructions execute nothing')
                    assert source.read_bytes() == raw
                    checks.append('original Unicode document bytes remain unchanged')
                    controls['Goal — guided English/Ukrainian commands or typed JSON plan'].SetValue('List my libraries')
                    assert not button(dialog, 'Run / resume').IsEnabled()
                    click(button(dialog, 'Propose with local model'))
                    assert not button(dialog, 'Run / resume').IsEnabled()
                    state['phase'] = 'model'
                elif state['phase'] == 'model' and review.GetValue().startswith('Local model proposal received'):
                    assert len(model_calls) == 1
                    assert model_calls[0]['model'] == 'fixture-model'
                    assert 'List my libraries' in model_calls[0]['messages'][0]['content']
                    assert len(AssistantService(frame.library).jobs()) == 1
                    assert not button(dialog, 'Run / resume').IsEnabled()
                    checks.append('local proposal uses only the explicitly approved loopback goal')
                    click(button(dialog, 'Preview plan'))
                    assert button(dialog, 'Run / resume').IsEnabled()
                    assert len(AssistantService(frame.library).jobs()) == 1
                    checks.append('native model proposal requires separate Preview and creates no job')
                    close()
                    return
                wx.CallLater(50, poll)
            except BaseException as exc:
                failures.append(type(exc).__name__ + ': ' + str(exc))
                close()

        wx.CallAfter(poll)
        assistant_dialog.show_assistant(frame)
        assert refreshed == [tuple(row.id for row in frame.library.workspaces())]
        checks.append('created libraries refresh in the dialog and desktop without reopening')
        frame.Destroy()
        if failures or len(checks) != 9:
            raise RuntimeError('; '.join(failures) or 'Native controls did not complete all checks.')
    for name, expected in hashes.items():
        if sha256((source_root / 'carr_library' / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Acceptance source changed during execution.')
    print(json.dumps({'status': 'PASS', 'windows_execution': 'PASS', 'checks': checks,
                      'source_commit': manifest['source_commit'],
                      'installed_modules': 'PASS', 'source_hashes': hashes,
                      'manual_nvda': 'NOT_RUN', 'paid_api': 'NOT_RUN',
                      'local_model_transport': 'synthetic', 'model_inference': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
