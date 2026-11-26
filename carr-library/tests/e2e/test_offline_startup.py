"""Offline initialization order using fresh interpreters and fake native modules.

Children run with -I -S: no installed third-party packages, site hooks, or real
ONNX/FastEmbed binaries can load. An import finder records the environment at
each synthetic dependency boundary and rejects unexpected dependency imports.
"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


SOURCE = Path(os.environ.get('CARR_TEST_SOURCE',
                            str(Path(__file__).resolve().parents[2] / 'src'))).resolve()

PRELUDE = r'''
import importlib
import importlib.abc
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import runpy
import sys
import types

source = Path(sys.argv[1])
sys.path.insert(0, str(source))
expected = {
    'ORT_DISABLE_TELEMETRY': '1',
    'HF_HUB_DISABLE_TELEMETRY': '1',
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
    'DO_NOT_TRACK': '1',
}
events = []
allow_model_imports = False

def check_policy():
    assert {key: os.environ.get(key) for key in expected} == expected

class Tokenizer:
    def no_truncation(self):
        pass
    def no_padding(self):
        pass

class NativeSentinel(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    packages = {'onnxruntime': False, 'fastembed': True,
                'fastembed.common': True,
                'fastembed.common.preprocessor_utils': False}

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] not in ('onnxruntime', 'fastembed'):
            return None
        assert allow_model_imports, 'Optional inference dependency imported at package/CLI startup'
        assert fullname in self.packages, 'Unexpected inference dependency import: ' + fullname
        check_policy()
        events.append('import:' + fullname)
        return importlib.util.spec_from_loader(fullname, self,
                                               is_package=self.packages[fullname])

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        check_policy()
        if module.__name__ == 'onnxruntime':
            module.__version__ = '1.32.0'
            def disable_telemetry_events():
                check_policy()
                events.append('disable_api')
            module.disable_telemetry_events = disable_telemetry_events
        elif module.__name__ == 'fastembed':
            assert 'disable_api' in events, 'Legacy opt-out API must precede FastEmbed import'
            class TextEmbedding:
                def __init__(self, **options):
                    check_policy()
                    assert options['local_files_only'] is True
                    assert options['providers'] == ['CPUExecutionProvider']
                    assert 'disable_api' in events
                    events.append('engine_created')
                    self.model = types.SimpleNamespace(tokenizer=Tokenizer())
                def embed(self, *args, **kwargs):
                    raise AssertionError('This startup test must never run inference')
            module.TextEmbedding = TextEmbedding
        elif module.__name__ == 'fastembed.common.preprocessor_utils':
            module.load_tokenizer = lambda root: (Tokenizer(), {})

sys.meta_path.insert(0, NativeSentinel())
# Override even a parent's safe settings: the application must enforce its
# offline contract against inherited values which would otherwise enable I/O.
os.environ.update({key: '0' for key in expected})
'''


def run_isolated(body):
    assert (SOURCE / 'carr_library').is_dir(), 'Set CARR_TEST_SOURCE to the candidate src directory'
    result = subprocess.run([sys.executable, '-I', '-S', '-c',
                             PRELUDE + '\n' + textwrap.dedent(body), str(SOURCE)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('entry', ['package', 'cli'])
def test_package_and_cli_set_policy_without_importing_native_dependencies(entry):
    run_isolated(f'''
        if {entry!r} == 'package':
            import carr_library
        else:
            sys.argv = ['carr_library', '--version']
            try:
                runpy.run_module('carr_library', run_name='__main__')
            except SystemExit as exc:
                assert exc.code == 0
            else:
                raise AssertionError('Version entrypoint did not exit')
        check_policy()
        assert events == []
        assert 'onnxruntime' not in sys.modules and 'fastembed' not in sys.modules
    ''')


def test_ranker_reasserts_policy_before_native_import_and_engine_creation():
    run_isolated('''
        from carr_library import semantic
        versions = {'fastembed': '0.9.1', 'onnxruntime': '1.32.0'}
        importlib.metadata.version = lambda name: versions[name]
        semantic.verify_model_bundle = lambda *args: Path('/synthetic-approved-model')
        # Settings may be changed after package import but before model use.
        os.environ.update({key: '0' for key in expected})
        allow_model_imports = True
        semantic.OfflineSemanticRanker('/synthetic-approved-model')
        check_policy()
        assert events.index('import:onnxruntime') < events.index('disable_api')
        assert events.index('disable_api') < events.index('import:fastembed')
        assert events.index('import:fastembed') < events.index('engine_created')
        assert events.count('engine_created') == 1
    ''')


@pytest.mark.parametrize('when', ['before_package', 'after_package'])
def test_preloaded_runtime_without_optout_remains_rejected(when):
    run_isolated(f'''
        if {when!r} == 'after_package':
            import carr_library
            os.environ.pop('ORT_DISABLE_TELEMETRY', None)
        runtime = types.ModuleType('onnxruntime')
        runtime.disable_telemetry_events = lambda: events.append('unsafe_api_call')
        sys.modules['onnxruntime'] = runtime
        from carr_library.offline_policy import disable_native_telemetry
        for attempt in range(2):
            try:
                disable_native_telemetry()
            except RuntimeError as exc:
                assert 'restart' in str(exc).lower()
            else:
                raise AssertionError('A late opt-out was accepted as safe initialization')
        assert events == []
    ''')


def test_unapproved_runtime_version_is_rejected_before_native_import():
    run_isolated('''
        from carr_library.offline_policy import disable_native_telemetry
        importlib.metadata.version = lambda name: '1.31.0'
        allow_model_imports = True
        try:
            disable_native_telemetry()
        except ValueError as exc:
            assert '1.32.0' in str(exc)
        else:
            raise AssertionError('Unapproved runtime version was accepted')
        assert events == [] and 'onnxruntime' not in sys.modules
    ''')


def test_legacy_model_probe_uses_same_preinitialization_policy():
    run_isolated('''
        sys.path.insert(0, str(source.parent))
        from bootstrap.semantic_compat import probe
        probe.verified_bundle = lambda *args: '/synthetic-approved-model'
        importlib.metadata.version = lambda name: '1.32.0'
        allow_model_imports = True
        probe.load_engine('/synthetic-approved-model', {})
        check_policy()
        assert events.index('import:onnxruntime') < events.index('disable_api')
        assert events.index('disable_api') < events.index('import:fastembed')
        assert events.index('import:fastembed') < events.index('engine_created')
    ''')
