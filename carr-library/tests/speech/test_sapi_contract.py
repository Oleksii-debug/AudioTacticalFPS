"""Fixed SAPI transport, private bounded diagnostics and actual Windows schema checks."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech import runtime


@pytest.fixture
def provider(tmp_path, monkeypatch):
    executable = tmp_path / 'trusted-test-host.exe'
    executable.write_bytes(b'process double; never executed')
    # Use the ordinary local-file path on Linux; never impersonate the Windows OS
    # executable validation in the production constructor.
    monkeypatch.setattr(runtime, 'system_powershell', lambda path, **kwargs: path)
    value = runtime.SapiProvider(executable)
    monkeypatch.setattr(runtime, 'os', SimpleNamespace(name='nt'))
    return value


def test_request_keeps_exact_unicode_source_out_of_fixed_argv(provider, tmp_path, monkeypatch):
    text = '\ufeff<voice>Доброго дня.</voice>\r\n$(not code); \\path "quote" /Date(1)/ 😀'
    data = dict(operation='render', token='opaque-token', text=text, output=str(tmp_path / 'voice.wav'),
                rate=0, volume=65, punctuation=False)
    saved = []
    def run(args, **kwargs):
        saved.append(Path(args[args.index('-Request') + 1]))
        assert json.loads(saved[-1].read_bytes()) == {'version': 1, **data}
        assert args[1:5] == ['-NoLogo', '-NoProfile', '-NonInteractive', '-File']
        assert Path(args[5]).name == 'sapi_worker.ps1'
        assert all(text not in os.fspath(item) and 'opaque-token' not in os.fspath(item) for item in args)
        assert '-Command' not in args and '-ExecutionPolicy' not in args
        assert kwargs['timeout'] == 7 and kwargs['max_output'] == 8192
        assert kwargs['output_file'] == Path(data['output'])
        return b'{"version":1,"status":"rendered"}', b'not logged', 0
    monkeypatch.setattr(runtime, 'run_process', run)
    assert provider._request(data, timeout=7, output=Path(data['output']), budget=8192)['status'] == 'rendered'
    assert provider.last_request_diagnostic['outcome'] == 'complete'
    assert text not in json.dumps(provider.last_request_diagnostic)
    assert not saved[0].exists()


def test_discovery_cold_start_has_a_bounded_90_second_budget(provider, monkeypatch):
    def run(args, **kwargs):
        assert kwargs['timeout'] == 90
        return b'{"version":1,"voices":[]}', b'', 0
    monkeypatch.setattr(runtime, 'run_process', run)
    assert provider.voices() == ()


def test_timeout_keeps_only_safe_last_phase_and_cleans_private_request(provider, monkeypatch):
    paths = []
    def run(args, **kwargs):
        request = Path(args[args.index('-Request') + 1])
        trace = Path(args[args.index('-PhaseTrace') + 1])
        paths.extend((request, trace))
        trace.write_text('script-entered\t1790000000000\t0\njson-runtime-loading\t1790000000002\t2\n')
        raise TimeoutError('private engine diagnostic must not escape')
    monkeypatch.setattr(runtime, 'run_process', run)
    with pytest.raises(TimeoutError, match='90-second.*json-runtime-loading') as raised:
        provider.voices()
    assert 'private' not in str(raised.value)
    assert provider.last_request_diagnostic['outcome'] == 'timeout'
    assert provider.last_request_diagnostic['phases'][-1]['worker_ms'] == 2
    assert all(not path.exists() for path in paths)


def test_timeout_before_worker_entry_is_distinguished(provider, monkeypatch):
    def run(*args, **kwargs):
        raise TimeoutError
    monkeypatch.setattr(runtime, 'run_process', run)
    with pytest.raises(TimeoutError, match='host-startup'):
        provider.voices()
    assert provider.last_request_diagnostic['phases'] == []


def test_cancellation_remains_cancellation_and_removes_private_files(provider, monkeypatch):
    paths = []
    def run(args, **kwargs):
        paths.append(Path(args[args.index('-Request') + 1]))
        raise InterruptedError('cancelled')
    monkeypatch.setattr(runtime, 'run_process', run)
    with pytest.raises(InterruptedError):
        provider.voices()
    assert provider.last_request_diagnostic['outcome'] == 'cancelled'
    assert not paths[0].exists()


@pytest.mark.parametrize('version', [True, '1', 1.0, None, 2])
def test_response_version_has_exact_integer_type(provider, monkeypatch, version):
    monkeypatch.setattr(runtime, 'run_process', lambda *args, **kwargs:
                        (json.dumps(dict(version=version, voices=[])).encode(), b'', 0))
    with pytest.raises(ValueError, match='SAPI response'):
        provider.voices()
    assert provider.last_request_diagnostic['outcome'] == 'failed'


@pytest.mark.parametrize('trace', [b'x' * 4097, b'private speech\t1\t0\n',
    b'script-entered\t1\tsecret\n', b'script-entered\t1\t4\nrequest-read\t2\t3\n',
    b'script-entered\t1\t0\n' * 33, b'\xff'])
def test_trace_rejects_unbounded_unrecognised_or_non_numeric_data(tmp_path, trace):
    path = tmp_path / 'phases.tsv'
    path.write_bytes(trace)
    assert runtime._sapi_phase_trace(path) == []


@pytest.mark.skipif(os.name != 'nt', reason='Worker contract requires native Windows PowerShell 5.1.')
@pytest.mark.parametrize('data', [dict(operation='voices', extra='unexpected'),
                                  dict(operation='voices', version=True)])
def test_actual_windows_worker_rejects_invalid_contract_before_com(data):
    from carr_library.speech._io import windows_directory
    executable = windows_directory() / 'System32' / 'WindowsPowerShell/v1.0/powershell.exe'
    provider = runtime.SapiProvider(executable)
    with pytest.raises(RuntimeError, match='failed'):
        provider._request(data)
    phases = [item['phase'] for item in provider.last_request_diagnostic['phases']]
    assert 'json-runtime-ready' in phases and phases[-1] == 'failed'
    assert 'sapi-activating' not in phases
