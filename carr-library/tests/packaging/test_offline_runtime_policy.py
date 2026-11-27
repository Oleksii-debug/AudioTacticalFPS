"""Verify initialization order without importing a real native model runtime.

Fresh interpreters start with explicit inherited opt-ins. Import-time fake ORT
modules fail if the application's opt-out arrives too late. These checks cover
process setup, not a claim that Python socket hooks contain native libraries.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]
OFFLINE_KEYS = (
    'ORT_DISABLE_TELEMETRY',
    'HF_HUB_DISABLE_TELEMETRY',
    'HF_HUB_OFFLINE',
    'TRANSFORMERS_OFFLINE',
    'DO_NOT_TRACK',
)


def _inherited_optins():
    env = dict(os.environ)
    env.update({key: '0' for key in OFFLINE_KEYS})
    return env


def _run_fresh(code):
    result = subprocess.run(
        [sys.executable, '-I', '-S', '-c', textwrap.dedent(code), str(ROOT / 'src')],
        env=_inherited_optins(), text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_package_overrides_optins_before_native_import_and_calls_legacy_api():
    _run_fresh('''
        import importlib.abc
        import importlib.util
        import os
        import sys

        sys.path.insert(0, sys.argv[1])
        assert os.environ['ORT_DISABLE_TELEMETRY'] == '0'

        class NativeImportProbe(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            imported = False
            disabled = False

            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'onnxruntime':
                    assert os.environ['ORT_DISABLE_TELEMETRY'] == '1'
                    return importlib.util.spec_from_loader(fullname, self)
                if fullname.startswith(('fastembed', 'huggingface_hub', 'onnxruntime.')):
                    raise AssertionError('Unexpected real optional dependency import')

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                assert os.environ['ORT_DISABLE_TELEMETRY'] == '1'
                self.imported = True
                module.disable_telemetry_events = lambda: setattr(self, 'disabled', True)

        probe = NativeImportProbe()
        sys.meta_path.insert(0, probe)
        import carr_library
        from carr_library import offline_policy

        assert not probe.imported, 'Package initialization must keep inference optional'
        for key in ('ORT_DISABLE_TELEMETRY', 'HF_HUB_DISABLE_TELEMETRY',
                    'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'DO_NOT_TRACK'):
            assert os.environ[key] == '1', key
        offline_policy.importlib.metadata.version = lambda name: '1.32.0'
        runtime = offline_policy.disable_native_telemetry()
        assert runtime is sys.modules['onnxruntime']
        assert probe.imported and probe.disabled
    ''')


def test_unprotected_preloaded_runtime_requires_restart():
    _run_fresh('''
        import os
        import sys
        import types

        sys.path.insert(0, sys.argv[1])
        assert os.environ['ORT_DISABLE_TELEMETRY'] == '0'
        runtime = types.ModuleType('onnxruntime')
        runtime.disabled = False
        runtime.disable_telemetry_events = lambda: setattr(runtime, 'disabled', True)
        sys.modules['onnxruntime'] = runtime

        import carr_library
        from carr_library import offline_policy
        offline_policy.importlib.metadata.version = lambda name: '1.32.0'
        assert os.environ['ORT_DISABLE_TELEMETRY'] == '1'
        # Updating the environment cannot undo prior native initialization.
        for attempt in range(2):
            try:
                offline_policy.disable_native_telemetry()
            except RuntimeError as exc:
                assert 'Restart' in str(exc)
            else:
                raise AssertionError('Unprotected preloaded runtime was accepted')
        assert not runtime.disabled
    ''')


def test_ci_child_opts_out_before_fake_runtime_import(tmp_path):
    for name in ('scripts', 'src', 'tests'):
        (tmp_path / name).mkdir()
    runner = tmp_path / 'scripts' / 'ci_check.py'
    shutil.copyfile(ROOT / 'scripts' / 'ci_check.py', runner)
    shutil.copyfile(ROOT / 'scripts' / 'ci_watchdog.py', runner.with_name('ci_watchdog.py'))
    # ci_check loads its exact standalone Windows owner before starting pytest.
    # Copy only that dependency, without importing or staging the application.
    owner_relative = Path('src/carr_library/audio/windows_process.py')
    owner = tmp_path / owner_relative
    owner.parent.mkdir(parents=True)
    owner_bytes = (ROOT / owner_relative).read_bytes()
    owner.write_bytes(owner_bytes)
    fake_runtime = tmp_path / 'src' / 'onnxruntime.py'
    fake_runtime.write_text(textwrap.dedent('''
        import os
        for key in ('ORT_DISABLE_TELEMETRY', 'HF_HUB_DISABLE_TELEMETRY',
                    'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'DO_NOT_TRACK'):
            assert os.environ.get(key) == '1', 'Runtime imported before opt-out: ' + key
        disabled = False
        def disable_telemetry_events():
            global disabled
            disabled = True
    '''), encoding='utf-8')
    (tmp_path / 'tests' / 'test_child.py').write_text(textwrap.dedent(f'''
        from pathlib import Path
        import onnxruntime

        def test_fake_runtime_imported_with_optout_and_api_disabled():
            assert Path(onnxruntime.__file__).resolve() == Path({str(fake_runtime)!r})
            assert onnxruntime.disabled
    '''), encoding='utf-8')
    result = subprocess.run(
        [sys.executable, '-I', '-S', str(runner), 'tests/test_child.py'],
        cwd=tmp_path, env=_inherited_optins(), text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'junit_passed=1, failed=0, skipped=0, pytest_exit=0' in result.stdout
    assert owner.read_bytes() == owner_bytes, 'The copied process owner changed during execution.'
