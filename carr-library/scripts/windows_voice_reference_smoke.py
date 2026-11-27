"""Actual Windows reference-package controls with synthetic PCM and owner protocol.

The source/installed application performs its real review and publication.
No model, human voice, NVDA speech or native file-picker qualification is claimed.
"""
import argparse
import ast
import faulthandler
from hashlib import sha256
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback
import wave
import zipfile

def _installed_package_root(distributions, package_file, checkout):
    """Bind installed acceptance to the imported package's actual wheel record.

    A checkout egg-info entry may precede the wheel in metadata discovery.
    Its project-relative locate_file('carr_library') is not package evidence.
    Source acceptance never consults installed distribution metadata.
    """
    actual = Path(package_file).resolve(strict=True)
    checkout = Path(checkout).resolve(strict=True)
    if actual.is_relative_to(checkout):
        raise RuntimeError('Installed acceptance imported the checkout package.')
    matches = []
    for distribution in distributions:
        for member in distribution.files or ():
            if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                continue
            try:
                declared = Path(distribution.locate_file(member)).resolve(strict=True)
            except FileNotFoundError:
                continue  # An unrelated stale metadata entry is not this package.
            if declared == actual:
                matches.append(declared.parent)
    if len(matches) != 1:
        raise RuntimeError('Exactly one installed wheel record must own the imported package.')
    return matches[0]



def _constants(path, names):
    values = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    values[target.id] = ast.literal_eval(node.value)
    if set(values) != set(names):
        raise ValueError('The exact synthetic owner protocol fixture is incomplete.')
    return values


def _write_report(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + '\n',
                    encoding='utf-8')


def _run_probe(args, report, checkpoint):
    import wx
    import carr_library
    from carr_library.audio import model_voices
    from carr_library.audio.local_runtime import open_local_audio_runtime
    from carr_library.audio.voice_packages import VoicePackageManager
    from carr_library.audio.voice_settings import VoiceSettingsStore
    from carr_library.ui.voice_packages import VoicePackageDialog

    checkout = Path(__file__).resolve().parents[1]
    expected = checkout / 'src' / 'carr_library'
    if args.expect_installed:
        assert sys.flags.isolated, 'Installed acceptance requires Python -I.'
        package_root = _installed_package_root(importlib.metadata.distributions(name='universal-research-library'),
                                               carr_library.__file__, checkout)
    else:
        package_root = Path(carr_library.__file__).resolve().parent
        assert package_root == expected.resolve(), 'The source probe imported another package.'
    origins = {}
    for name in ('audio.voice_reference', 'audio.voice_packages', 'audio.voice_lifecycle',
                 'audio.package_publication', 'audio.model_voices', 'audio.dsp',
                 'ui.voice_reference', 'ui.voice_packages'):
        module = importlib.import_module('carr_library.' + name)
        imported = Path(module.__file__).resolve(strict=True)
        relative = Path(*name.split('.')).with_suffix('.py')
        assert imported == (package_root / relative).resolve(strict=True)
        actual, wanted = imported.read_bytes(), (expected / relative).read_bytes()
        assert sha256(actual).digest() == sha256(wanted).digest(), name
        origins[name] = {'path': str(imported), 'sha256': sha256(actual).hexdigest()}
    report['module_origins'] = origins
    report['acceptance_mode'] = 'installed' if args.expect_installed else 'source'
    checkpoint()

    constants = _constants(checkout / 'tests/audio/test_voice_packages.py',
                           {'POLICY', 'PACKAGES', 'ASSETS', 'WORKER'})
    create = _constants(checkout / 'tests/audio/test_voice_reference.py', {'_CREATE'})['_CREATE']
    original_run, original_picker = model_voices._run, wx.FileDialog
    active, child, failures = [None], [None], []
    started = monotonic()
    def failed():
        detail = traceback.format_exc()
        failures.append(detail)
        report['failed_stage'] = report.get('stage', 'unknown')
        report['failures'] = list(failures)
        print(detail, file=sys.stderr, flush=True)
        checkpoint()
        # End these test-owned modals directly; do not provoke further actions.
        for item in (child[0], active[0]):
            if item is None:
                continue
            try:
                if item.dialog and item.dialog.IsModal():
                    item.dialog.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failed()
            return False

    app = ProbeApp(False)
    def guarded(action):
        if failures:
            return
        try:
            if monotonic() - started > 110:
                raise TimeoutError('The native reference control scenario exceeded its deadline.')
            action()
        except BaseException:
            failed()
    def later(action):
        wx.CallLater(50, lambda: guarded(action))
    def wait(predicate, complete):
        if predicate():
            complete()
        else:
            later(lambda: wait(predicate, complete))
    def emit(control, event_type):
        assert control.IsShown() and control.IsEnabled()
        event = wx.CommandEvent(event_type.typeId, control.GetId())
        event.SetEventObject(control)
        assert control.GetEventHandler().ProcessEvent(event)
    def click(control):
        control.SetFocus()
        emit(control, wx.EVT_BUTTON)
    def check(label, truth):
        assert truth, label
        report['checks'].append(label)
        checkpoint()

    try:
        with TemporaryDirectory(prefix='carr-native-reference-review-') as temporary:
            base = Path(temporary)
            runtime = base / 'owner'
            runtime.mkdir()
            assets = {
                'python/python.exe': b'INERT synthetic interpreter identity; never executed',
                'nvda_worker.py': constants['WORKER'].encode(),
                'voice_policy.py': constants['POLICY'].encode(),
                'voice_packages.py': (constants['PACKAGES'] + create).encode(),
                'voice_assets.py': constants['ASSETS'].encode(),
                'models/OmniVoice/model.bin': b'INERT model identity; never loaded',
                'models/voices/catalog.json': b'{"voices":{},"languages":{"uk":"Ukrainian"}}',
            }
            for name, raw in assets.items():
                path = runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            manifest = runtime / 'manifest.json'
            manifest.write_text(json.dumps({
                'schema_version': 1, 'engine': 'dordiai-voices',
                'model_id': 'native-synthetic-reference-api', 'revision': 'f' * 40,
                'license': 'Purpose-written synthetic protocol only; no voice assets',
                'files': {name: sha256(raw).hexdigest() for name, raw in assets.items()},
                'settings': {'protocol': 'nvda-worker-jsonl-pcm24k-v1', 'profile_languages': {}},
                'license_accepted': True, 'trusted_weights': True, 'personal_noncommercial': True,
            }), encoding='utf-8')
            calls = []
            def actual_synthetic_child(command, **kwargs):
                assert Path(command[0]).samefile(runtime / 'python/python.exe'), command[0]
                assert list(command[1:4]) == ['-I', '-B', '-u'], command[1:4]
                calls.append(tuple(map(str, command[:4])))
                return original_run([sys.executable, *command[1:]], **kwargs)
            model_voices._run = actual_synthetic_child
            library = base / 'library'
            store = VoiceSettingsStore(library)
            store.register({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
                'dordiai_manifest': str(manifest)}, expected_revision=0)
            manager = VoicePackageManager(library)
            manager.configure('omnivoice', 'models', 'models/OmniVoice', 'models/voices',
                              expected_revision=manager.store.read()['revision'])
            registration = manager.store.path.read_bytes(), manifest.read_bytes()
            source, output = base / 'synthetic.wav', base / 'selected.dordiai-voice'
            from array import array
            samples = array('h', (int(12000 * math.sin(index * .21)) for index in range(4800)))
            if sys.byteorder != 'little':
                samples.byteswap()
            with wave.open(str(source), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(samples.tobytes())
            original_audio = source.read_bytes()
            transcript = 'Purpose-written synthetic PCM; no human speaker.'
            class SelectedDestination:
                def __init__(self, *values, **options):
                    assert options['style'] == wx.FD_SAVE
                def __enter__(self): return self
                def __exit__(self, *values): return False
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(output)
            wx.FileDialog = SelectedDestination
            frame = wx.Frame(None, title='Native reference control owner')
            frame.Show()
            with open_local_audio_runtime(library, strict=True):
                controller = VoicePackageDialog(frame, library)
                active[0] = controller

                def find_reference():
                    for window in wx.GetTopLevelWindows():
                        selected = getattr(window, 'voice_reference', None)
                        if (selected is not None and not selected.closed and window.IsShown()
                                and window.IsModal() and window.GetParent() is controller.dialog):
                            child[0] = selected
                            return True
                    return False

                def ready_parent():
                    report['stage'] = 'open-reference'
                    check('existing package dialog finished its actual configuration review',
                          not controller.busy and controller.revision == manager.store.read()['revision'])
                    later(lambda: wait(find_reference, fill_reference))
                    click(controller.create_reference_button)
                    if failures:
                        return
                    report['stage'] = 'parent-read-only-handoff'
                    wait(lambda: not controller.busy, parent_reviewed)

                def fill_reference():
                    report['stage'] = 'explicit-reference-input'
                    selected = child[0]
                    values = dict(source=str(source), profile_id='native-reference', version='1.0.0',
                        name='Synthetic native reference', language='uk',
                        provenance_source='Purpose-written native control test.',
                        rights_notice='Synthetic PCM; no person, model or commercial voice.')
                    for name, value in values.items():
                        selected.fields[name].ChangeValue(value)
                    selected.transcript.ChangeValue(transcript)
                    check('permission and quality review start unchecked',
                          not selected.consent.GetValue() and not selected.quality.GetValue())
                    check('creation is disabled before a real input review', not selected.create_button.IsEnabled())
                    selected.consent.SetValue(True)
                    emit(selected.consent, wx.EVT_CHECKBOX)
                    click(selected.review_button)
                    wait(lambda: not selected.busy, reviewed_reference)

                def reviewed_reference():
                    report['stage'] = 'reviewed-real-pcm'
                    selected = child[0]
                    check('actual worker returned the captured reference review', selected.review is not None)
                    check('actual FFprobe and PCM frame measurements agree',
                          selected.review.pcm['sample_rate'] == 16000 and selected.review.pcm['frames'] == 4800)
                    check('the original source remains byte-identical', source.read_bytes() == original_audio)
                    check('review still requires separate human confirmation',
                          not selected.quality.GetValue() and not selected.create_button.IsEnabled())
                    # A real text-control event must revoke the old review.
                    selected.fields['name'].SetValue('Synthetic native reference revised')
                    wait(lambda: selected.review is None and not selected.consent.GetValue(), edited_review_reset)

                def edited_review_reset():
                    selected = child[0]
                    check('changed native fields revoke review and permission',
                          selected.review is None and not selected.consent.GetValue())
                    selected.consent.SetValue(True)
                    emit(selected.consent, wx.EVT_CHECKBOX)
                    click(selected.review_button)
                    wait(lambda: not selected.busy, create_reviewed)

                def create_reviewed():
                    report['stage'] = 'create-reviewed-local-package'
                    selected = child[0]
                    check('changed fields received a new exact review',
                          selected.review is not None and selected.review.description.name.endswith('revised'))
                    selected.quality.SetValue(True)
                    emit(selected.quality, wx.EVT_CHECKBOX)
                    check('explicit confirmation enables the native create control', selected.create_button.IsEnabled())
                    click(selected.create_button)
                    wait(lambda: not selected.busy, package_created)

                def package_created():
                    report['stage'] = 'verify-created-package'
                    selected = child[0]
                    check('actual owner child and shared publication returned a completed receipt',
                          selected.result is not None and output.is_file())
                    check('receipt binds the saved actual bytes',
                          selected.result.sha256 == sha256(output.read_bytes()).hexdigest())
                    with zipfile.ZipFile(output) as archive:
                        check('package contains only its data manifest, PCM and transcript',
                              set(archive.namelist()) == {'voice-package.json', 'reference.wav', 'reference.txt'})
                        check('saved PCM and transcript match the reviewed exact inputs',
                              archive.read('reference.wav') == original_audio
                              and archive.read('reference.txt') == transcript.encode('utf-8'))
                    check('creation did not change registration or install references',
                          registration == (manager.store.path.read_bytes(), manifest.read_bytes())
                          and not (runtime / 'models/voices/packages/native-reference').exists())
                    check('the native status clearly labels the unencrypted local result',
                          'UNENCRYPTED' in selected.status.GetValue())
                    click(selected.close_button)

                def parent_reviewed():
                    report['stage'] = 'separate-parent-permission'
                    check('parent receives the saved path through its actual inspect worker',
                          controller.review is not None and controller.review.document['id'] == 'native-reference')
                    check('parent inspection does not grant rights or languages',
                          not controller.rights.GetValue() and controller.supported.GetValue() == '')
                    check('source stays unchanged after publication and parent inspection',
                          source.read_bytes() == original_audio)
                    check('the isolated synthetic owner actually ran', bool(calls))
                    click(controller.close_button)

                later(lambda: wait(lambda: not controller.busy, ready_parent))
                try:
                    controller.dialog.ShowModal()
                finally:
                    controller.close()
                    controller.executor.shutdown(wait=True, cancel_futures=True)
                    if child[0] is not None and not child[0].destroyed:
                        child[0].dialog.Destroy()
                    if not controller.destroyed:
                        controller.dialog.Destroy()
                    frame.Destroy()
                    active[0] = child[0] = None
            assert not failures, '\n'.join(failures)
            assert len(report['checks']) == 20
    finally:
        model_voices._run = original_run
        wx.FileDialog = original_picker
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source', type=Path)
    mode.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    report = {'status': 'FAIL', 'stage': 'startup', 'checks': [],
              'model_inference': 'NOT_RUN', 'human_voice_quality': 'NOT_RUN',
              'nvda_speech': 'NOT_RUN', 'native_file_picker': 'NOT_RUN',
              'recorder_handoff': 'covered separately by existing recorder and focused adapter tests'}
    args.report = args.report.absolute()
    checkpoint = lambda: _write_report(args.report, report)
    checkpoint()
    faulthandler.enable()
    faulthandler.dump_traceback_later(180, exit=True)
    try:
        if sys.platform != 'win32':
            raise RuntimeError('This exact native qualification requires Windows; no skip is accepted.')
        if args.source is not None:
            selected = args.source.resolve(strict=True)
            expected = Path(__file__).resolve().parents[1] / 'src'
            if selected != expected.resolve(strict=True):
                raise RuntimeError('Choose this exact source tree.')
            sys.path.insert(0, str(selected))
        _run_probe(args, report, checkpoint)
        report['status'] = 'PASS'
        report['stage'] = 'complete'
        return 0
    except BaseException:
        report['traceback'] = traceback.format_exc()
        print(report['traceback'], file=sys.stderr, flush=True)
        return 1
    finally:
        checkpoint()
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
