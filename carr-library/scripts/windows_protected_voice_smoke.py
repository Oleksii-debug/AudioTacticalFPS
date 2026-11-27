"""Actual native protected-profile review; synthetic owner, real authenticated crypto.

No neural model, real voice sample, network authority or OS file-picker
qualification is involved. The existing owner child and package UI are real.
"""
from argparse import ArgumentParser
import ast
import faulthandler
from hashlib import sha256
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback
import zipfile

PASSWORD = 'Native protected fixture password 42!'
MODULES = (
    ('audio/voice_lifecycle.py', 'carr_library.audio.voice_lifecycle'),
    ('audio/voice_packages.py', 'carr_library.audio.voice_packages'),
    ('audio/package_publication.py', 'carr_library.audio.package_publication'),
    ('audio/cleanup.py', 'carr_library.audio.cleanup'),
    ('audio/model_voices.py', 'carr_library.audio.model_voices'),
    ('sync/crypto.py', 'carr_library.sync.crypto'),
    ('ui/voice_packages.py', 'carr_library.ui.voice_packages'),
)


def proof_helpers():
    path = Path(__file__).resolve().with_name('windows_clipboard_reader_smoke.py')
    with path.open('rb') as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise RuntimeError('The shared provenance helper exceeded its bound.')
    scope = {'__name__': '_protected_voice_proof', '__file__': str(path)}
    exec(compile(raw.decode('utf-8'), str(path), 'exec'), scope)
    return scope['_checkpoint'], scope['_module_evidence']


def installed_owner(package_file, checkout):
    actual = Path(package_file).resolve(strict=True)
    if actual.is_relative_to(checkout):
        raise RuntimeError('Installed acceptance imported the checkout package.')
    matches = []
    for distribution in importlib.metadata.distributions(name='universal-research-library'):
        for member in distribution.files or ():
            if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                continue
            try:
                declared = Path(distribution.locate_file(member)).resolve(strict=True)
            except FileNotFoundError:
                continue
            if declared == actual:
                matches.append(distribution)
    if len(matches) != 1:
        raise RuntimeError('Exactly one installed wheel RECORD must own the imported package.')
    return matches[0]


def run_native(report, phase):
    import wx
    from carr_library.audio import model_voices
    from carr_library.audio.voice_lifecycle import ProtectedVoicePackageReview
    from carr_library.audio.voice_packages import VoicePackageManager
    from carr_library.audio.voice_settings import VoiceSettingsStore
    from carr_library.ui.voice_packages import VoicePackageDialog

    fixture = Path(__file__).resolve().parents[1] / 'tests/audio/test_voice_packages.py'
    with fixture.open('rb') as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise RuntimeError('Synthetic owner fixture source exceeds its bound.')
    constants = {}
    for node in ast.parse(raw.decode('utf-8')).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {'POLICY', 'PACKAGES', 'ASSETS', 'WORKER'}:
                    constants[target.id] = ast.literal_eval(node.value)
    if set(constants) != {'POLICY', 'PACKAGES', 'ASSETS', 'WORKER'}:
        raise RuntimeError('The exact existing synthetic owner fixture is incomplete.')

    app, simulator = wx.App(False), wx.UIActionSimulator()
    failures, timers, controllers, password_entries = [], [], [], []
    original_run, original_picker = model_voices._run, wx.FileDialog

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        report['checks'].append(label)
        phase(label)

    def pump(predicate, timeout=30):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            app.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def click(button):
        if not button.IsEnabled():
            raise AssertionError('Native protected-profile control is disabled: ' + button.GetName())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('Native protected-profile event was not handled.')

    def later(callback):
        timers.append(wx.CallLater(30, callback))

    def enter_password(parent, password, count):
        deadline = monotonic() + 15
        def enter():
            try:
                windows = [window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.GetParent() is parent
                    and window.GetTitle() == 'Protected voice package password'
                    and window.IsModal() and window.IsShown()]
                if not windows:
                    if monotonic() < deadline:
                        later(enter)
                        return
                    raise AssertionError('The actual native password dialog did not open.')
                window, = windows
                fields = [control for control in window.GetChildren()
                    if isinstance(control, wx.TextCtrl)
                    and control.GetName().endswith('protected voice operation only')]
                check('native password entry has the explicit requested field count', len(fields) == count)
                for field in fields:
                    check('native password field is initially empty and masked',
                          field.GetValue() == '' and bool(field.GetWindowStyleFlag() & wx.TE_PASSWORD))
                    field.SetFocus()
                    check('native password field accepts keyboard focus',
                          pump(lambda: wx.Window.FindFocus() is field, timeout=3))
                    check('native password text keys were delivered', simulator.Text(password))
                    if not pump(lambda: field.GetValue() == password, timeout=3):
                        raise AssertionError('Native password input was not received exactly.')
                password_entries.append(count)
                check('native Return confirms the password entry', simulator.Char(wx.WXK_RETURN))
            except BaseException as error:
                failures.append(error)
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.GetParent() is parent and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
        later(enter)

    with TemporaryDirectory(prefix='protected-voice-native-') as temporary:
        base = Path(temporary)
        runtime = base / 'runtime'
        runtime.mkdir()
        assets = {
            'python/python.exe': b'INERT interpreter marker; actual test interpreter only',
            'nvda_worker.py': constants['WORKER'].encode(),
            'voice_policy.py': constants['POLICY'].encode(),
            'voice_packages.py': constants['PACKAGES'].encode(),
            'voice_assets.py': constants['ASSETS'].encode(),
            'models/Holos/model.bin': b'INERT synthetic model',
            'models/OmniVoice/model.bin': b'INERT synthetic model',
            'models/voices/catalog.json': b'{"voices":{},"languages":{"uk":"Ukrainian","sk":"Slovak"}}',
        }
        for name, data in assets.items():
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        manifest = runtime / 'manifest.json'
        manifest.write_bytes(json.dumps({
            'schema_version': 1, 'engine': 'dordiai-voices',
            'model_id': 'protected-native-synthetic-owner', 'revision': 'f' * 40,
            'license': 'Synthetic local protocol fixture; no voice assets',
            'files': {name: sha256(data).hexdigest() for name, data in assets.items()},
            'settings': {'protocol': 'nvda-worker-jsonl-pcm24k-v1', 'profile_languages': {}},
            'license_accepted': True, 'trusted_weights': True, 'personal_noncommercial': True,
        }).encode())
        library = base / 'library'
        VoiceSettingsStore(library).register({
            'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
            'dordiai_manifest': str(manifest)}, expected_revision=0)
        def child(command, **kwargs):
            if not Path(command[0]).samefile(runtime / 'python/python.exe'):
                raise AssertionError('Synthetic owner received an unselected interpreter.')
            if command[1:4] != ['-I', '-B', '-u']:
                raise AssertionError('The owner child lost its isolation flags.')
            return original_run([sys.executable, *command[1:]], **kwargs)
        model_voices._run = child
        data = {'sample.wav': b'INERT reference fixture', 'sample.txt': b'INERT exact reference text'}
        document = {
            'format': 'dordiai-voice-package', 'format_version': 1,
            'id': 'fixture', 'version': '1.0.0', 'name': 'Protected native synthetic profile',
            'engine': {'id': 'omnivoice', 'compatibility_version': 1},
            'languages': ['uk', 'sk'], 'styles': ['neutral'],
            'provenance': {'source': 'fixture:local', 'license': 'Synthetic fixture only'},
            'files': {name: {'sha256': sha256(content).hexdigest(), 'size_bytes': len(content),
                            'role': 'reference_audio' if name.endswith('.wav') else 'reference_text'}
                      for name, content in data.items()},
        }
        archive = base / 'input.dordiai-voice'
        with zipfile.ZipFile(archive, 'w') as writer:
            writer.writestr('voice-package.json', json.dumps(document).encode())
            for name, content in data.items():
                writer.writestr(name, content)
        manager = VoicePackageManager(library)
        configuration = manager.configuration()
        manager.configure('omnivoice', 'models', 'models/Holos', 'models/voices',
                          expected_revision=configuration['revision'])
        manager.apply(manager.inspect(archive), rights_approved=True, supported_languages=['uk'])
        baseline = manager.store.read()['revision']
        target = base / 'selected.carr-voice.enc'
        picker_calls = []
        class Picker:
            def __init__(self, *args, **kwargs):
                picker_calls.append(kwargs)
                self.dead = False
            def __bool__(self): return not self.dead
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(target)
            def Destroy(self): self.dead = True
        wx.FileDialog = Picker
        frame = wx.Frame(None, title='Protected reference package native fixture', size=(1050, 980))
        frame.Show()
        release = threading.Event()
        try:
            control = VoicePackageDialog(frame, library)
            controllers.append(control)
            def scenario():
                try:
                    check('native protected-profile setup completes on its existing worker',
                          pump(lambda: not control.busy))
                    control.profile.ChangeValue('fixture')
                    control.version.ChangeValue('1.0.0')
                    click(control.review_button)
                    check('actual owner version review completes', pump(lambda: not control.busy))
                    check('protected export rights start unapproved',
                          control.review.operation == 'activate' and not control.protected_export_rights.GetValue())
                    count = len(picker_calls)
                    click(control.protected_export_button)
                    check('an unchecked protected export never opens a destination picker',
                          len(picker_calls) == count and not target.exists())
                    control.protected_export_rights.SetValue(True)
                    enter_password(control.dialog, PASSWORD, 2)
                    click(control.protected_export_button)
                    check('actual encrypted owner export settles', pump(lambda: not control.busy))
                    if failures:
                        raise failures[0]
                    check('protected export publishes authenticated ciphertext only',
                          target.is_file() and not zipfile.is_zipfile(target)
                          and 'Ciphertext SHA-256' in control.status.GetValue())
                    ciphertext = target.read_bytes()
                    enter_password(control.dialog, PASSWORD, 1)
                    click(control.protected_inspect_button)
                    check('actual native decrypt and owner inspection settle', pump(lambda: not control.busy))
                    if failures:
                        raise failures[0]
                    check('authenticated inspection remains a fresh unapproved review',
                          type(control.review) is ProtectedVoicePackageReview
                          and not control.rights.GetValue() and control.supported.GetValue() == ''
                          and manager.store.read()['revision'] == baseline)
                    check('inspection leaves no plaintext stage or resource lease',
                          not list(base.glob('.voice-protected-import-*'))
                          and control.lifecycle.broker.snapshot()['leases'] == [])
                    prompts = len(password_entries)
                    click(control.apply_button)
                    check('unchecked fresh rights cannot start apply or another secret prompt',
                          len(password_entries) == prompts and manager.store.read()['revision'] == baseline)
                    control.rights.SetValue(True)
                    control.supported.ChangeValue('uk')
                    enter_password(control.dialog, PASSWORD, 1)
                    click(control.apply_button)
                    check('fresh actual protected apply settles on the same worker', pump(lambda: not control.busy))
                    if failures:
                        raise failures[0]
                    check('fresh apply changes only the reviewed registration and preserves ciphertext',
                          manager.store.read()['revision'] == baseline + 1
                          and target.read_bytes() == ciphertext and control.review is None)
                    enter_password(control.dialog, PASSWORD + 'wrong', 1)
                    click(control.protected_inspect_button)
                    check('wrong-password attempt settles without owner approval', pump(lambda: not control.busy))
                    if failures:
                        raise failures[0]
                    check('wrong password cannot create installation authority',
                          control.review is None and not control.rights.GetValue()
                          and manager.store.read()['revision'] == baseline + 1
                          and not list(base.glob('.voice-protected-import-*')))
                    check('the secret is absent from library JSON and UI status',
                          PASSWORD not in control.status.GetValue()
                          and all(PASSWORD.encode() not in path.read_bytes() for path in library.rglob('*.json')))
                    click(control.close_button)
                except BaseException as error:
                    failures.append(error)
                    if not control.destroyed and control.dialog.IsModal():
                        control.dialog.EndModal(wx.ID_CANCEL)
            later(scenario)
            control.dialog.ShowModal()
            control.close()
            control.executor.shutdown(wait=True, cancel_futures=True)
            if not control.destroyed:
                control.dialog.Destroy()
            check('the first native package owner completes its captured-ID destruction',
                  pump(lambda: control.destroyed))
            if failures:
                raise failures[0]

            closing = VoicePackageDialog(frame, library)
            controllers.append(closing)
            prepared, finished = threading.Event(), threading.Event()
            inspect = closing.lifecycle.inspect_protected
            def delayed(*args, **kwargs):
                result = inspect(*args, **kwargs)
                prepared.set()
                if not release.wait(10):
                    raise TimeoutError('The native closing fixture did not release its completed worker.')
                finished.set()
                return result
            closing.lifecycle.inspect_protected = delayed
            def destroy_scenario():
                try:
                    check('the second native package window finishes its own startup',
                          pump(lambda: not closing.busy))
                    enter_password(closing.dialog, PASSWORD, 1)
                    click(closing.protected_inspect_button)
                    check('actual inspection reaches a queued-result destruction boundary',
                          pump(prepared.is_set))
                    closing.dialog.Destroy()
                    release.set()
                    check('the original native owner destruction revokes GUI adoption',
                          pump(lambda: closing.destroyed and closing.closed))
                except BaseException as error:
                    failures.append(error)
                    release.set()
                    if not closing.destroyed and closing.dialog.IsModal():
                        closing.dialog.EndModal(wx.ID_CANCEL)
            later(destroy_scenario)
            closing.dialog.ShowModal()
            release.set()
            closing.executor.shutdown(wait=True, cancel_futures=True)
            app.Yield()
            if failures:
                raise failures[0]
            check('the completed worker cannot revive a destroyed review or retain plaintext',
                  finished.is_set() and closing.review is None
                  and not list(base.glob('.voice-protected-import-*'))
                  and closing.lifecycle.broker.snapshot()['leases'] == [])
            report.update(native_password_entry='wx.TextCtrl TE_PASSWORD + actual keyboard Text/Return',
                owner_api='Existing isolated synthetic owner child', crypto='cryptography 50.0.2',
                file_picker='NOT_RUN', speech_inference='NOT_RUN', voice_quality='NOT_RUN',
                manual_nvda='NOT_RUN', wx=wx.version())
        finally:
            release.set()
            for controller in controllers:
                controller.close()
                controller.executor.shutdown(wait=True, cancel_futures=True)
                if not controller.destroyed and controller.dialog:
                    controller.dialog.Destroy()
            frame.Destroy()
            app.Yield()
            model_voices._run, wx.FileDialog = original_run, original_picker


def main():
    parser = ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source', type=Path)
    mode.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    checkpoint, evidence = proof_helpers()
    report = dict(status='FAIL', phase='starting', checks=[], python=sys.version,
                  source_commit=os.environ.get('GITHUB_SHA'), installed=args.expect_installed,
                  file_picker='NOT_RUN', speech_inference='NOT_RUN', voice_quality='NOT_RUN',
                  manual_nvda='NOT_RUN')
    def phase(name):
        report['phase'] = name
        checkpoint(args.report, report)
    phase('starting')
    handle = None
    faulthandler.enable()
    faulthandler.dump_traceback_later(180, exit=True)
    try:
        if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
            raise RuntimeError('Run this actual Windows probe with Python -I -B.')
        checkout = Path(__file__).resolve().parents[1]
        if args.source is not None:
            selected = args.source.resolve(strict=True)
            if selected != checkout:
                raise RuntimeError('Choose the exact checkout for source acceptance.')
            sys.path.insert(0, str(selected / 'src'))
        import carr_library
        from carr_library.native_runtime import activate_bundled_runtime
        package_root = Path(carr_library.__file__).resolve(strict=True).parent
        distribution = None if args.source is not None else installed_owner(carr_library.__file__, checkout)
        modules = tuple((name, importlib.import_module(module)) for name, module in MODULES)
        report['modules'] = evidence(checkout, package_root, modules,
            source_mode=args.source is not None, distribution=distribution)
        handle = activate_bundled_runtime()
        phase('native-protected-profile-controls')
        run_native(report, phase)
        report['status'] = 'PASS'
        phase('completed')
        return 0
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:2000]}
        report['traceback'] = traceback.format_exc()[-12000:]
        phase('failed')
        raise
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
