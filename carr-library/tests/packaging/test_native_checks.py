"""Installation must load native extensions and honor optional bundle flags."""
import json
import os
from types import SimpleNamespace

import pytest

from carr_library import native_checks


def bundle(tmp_path, *, model=False, ocr=False):
    manifest = {'schema': 1, 'target': native_checks.TARGET, 'bundled_python': True,
                'bundled_model': model, 'bundled_ocr': ocr,
                'source_commit': 'a' * 40, 'files': {'README.txt': 'b' * 64}}
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


def controlled_imports(monkeypatch, events, *, failure=None, telemetry_failure=False,
                       ocr_status='PASS', native_handle=None):
    def activate(*, bundle_root):
        events.append(('activate_bundled_runtime', str(bundle_root)))
        return native_handle

    def telemetry():
        events.append('disable_native_telemetry')
        if telemetry_failure:
            raise RuntimeError('The model runtime was loaded before its offline policy.')

    def ocr(*, timeout):
        events.append(('canonical_ocr_check', timeout, os.environ.get('CARR_OCR_DIR')))
        return {'status': ocr_status, 'runtime': 'portable', 'synthetic_recognition': 'RESEARCH'}

    modules = {name: SimpleNamespace() for _, name in native_checks.NATIVE_MODULES}
    modules.update({'carr_library.native_runtime': SimpleNamespace(activate_bundled_runtime=activate),
                    'carr_library.offline_policy': SimpleNamespace(disable_native_telemetry=telemetry),
                    'tokenizers.tokenizers': SimpleNamespace(),
                    'carr_library.extract.ocr_runtime': SimpleNamespace(check_runtime=ocr)})

    def load(name):
        events.append(name)
        if name == failure:
            raise ImportError('DLL load failed: MSVCP140.dll was not found')
        if name not in modules:
            pytest.fail('Unexpected dependency import: ' + name)
        return modules[name]

    monkeypatch.setattr(native_checks, 'import_module', load)


def test_base_native_extensions_are_loaded_without_optional_engines(tmp_path, monkeypatch):
    bundle(tmp_path)
    events = []
    controlled_imports(monkeypatch, events)
    monkeypatch.setattr(native_checks.sys, 'platform', 'linux')
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'PASS'
    assert events == ['carr_library.native_runtime', ('activate_bundled_runtime', str(tmp_path)),
                      *[name for _, name in native_checks.NATIVE_MODULES]]
    assert report['platform'] == 'linux'
    assert report['windows_execution'] == 'NOT_RUN'
    assert report['windows_nvda'] == report['native_gui'] == 'NOT_RUN'
    assert report['semantic']['status'] == report['ocr']['status'] == 'NOT_INCLUDED'


def _native_manifest(tmp_path, monkeypatch):
    import hashlib
    from carr_library import native_launcher
    payload = b'Controlled native identity fixture; never executed.'
    monkeypatch.setattr(native_launcher, 'LAUNCHER_BYTES', len(payload))
    monkeypatch.setattr(native_launcher, 'LAUNCHER_SHA256', hashlib.sha256(payload).hexdigest())
    manifest = bundle(tmp_path)
    manifest['native_launcher'] = native_launcher.identity_record()
    manifest['files']['ResearchLibrary.exe'] = native_launcher.LAUNCHER_SHA256
    (tmp_path / 'ResearchLibrary.exe').write_bytes(payload)
    return manifest


def test_native_entry_hash_is_checked_without_executing_it(tmp_path, monkeypatch):
    manifest = _native_manifest(tmp_path, monkeypatch)
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    events = []
    controlled_imports(monkeypatch, events)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'PASS'
    assert report['source_commit'] == 'a' * 40
    assert report['native_launcher'] == {
        'status': 'PASS', 'sha256': manifest['files']['ResearchLibrary.exe'],
        'authenticode_signed': False, 'execution': 'NOT_RUN', 'windows_nvda': 'NOT_RUN'}
    assert report['checks'][0] == 'native launch entry identity'
    (tmp_path / 'ResearchLibrary.exe').unlink()
    events.clear()
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == report['native_launcher']['status'] == 'FAIL'
    assert report['failed_component'] == 'native launch entry identity'
    assert not events


def test_current_launcher_seal_never_inherits_historical_execution_acceptance(tmp_path, monkeypatch):
    import hashlib
    payload = b'Current separately qualified launcher fixture; never executed.'
    manifest = bundle(tmp_path)
    manifest['files']['ResearchLibrary.exe'] = hashlib.sha256(payload).hexdigest()
    (tmp_path / 'ResearchLibrary.exe').write_bytes(payload)
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    events = []
    controlled_imports(monkeypatch, events)
    report = native_checks.check_native_dependencies(tmp_path)
    identity = report['native_launcher']
    assert report['status'] == identity['status'] == 'PASS'
    assert identity['sha256'] == hashlib.sha256(payload).hexdigest()
    assert identity['source_commit'] == manifest['source_commit']
    assert identity['identity_scope'] == 'source-bound sealed bytes'
    assert identity['historical_native_acceptance'] == 'NOT_CLAIMED'
    assert identity['authenticode_signed'] is False
    assert identity['execution'] == identity['native_gui'] == identity['windows_nvda'] == 'NOT_RUN'
    assert 'windows_process_acceptance' not in identity
    (tmp_path / 'ResearchLibrary.exe').write_bytes(b'X' + payload[1:])
    events.clear()
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == report['native_launcher']['status'] == 'FAIL'
    assert report['failed_component'] == 'native launch entry identity'
    assert not events


@pytest.mark.parametrize('damage', ['source_missing', 'source_not_exact', 'hash_missing', 'hash_invalid', 'python_missing'])
def test_current_launcher_requires_complete_source_and_file_identity(tmp_path, monkeypatch, damage):
    manifest = bundle(tmp_path)
    manifest['files']['ResearchLibrary.exe'] = 'b' * 64
    if damage == 'source_missing':
        del manifest['source_commit']
    elif damage == 'source_not_exact':
        manifest['source_commit'] = 'HEAD'
    elif damage == 'hash_missing':
        manifest['files']['ResearchLibrary.exe'] = None
    elif damage == 'hash_invalid':
        manifest['files']['ResearchLibrary.exe'] = 'c' * 63
    else:
        manifest['bundled_python'] = False
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    events = []
    controlled_imports(monkeypatch, events)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'FAIL'
    assert report['failed_component'] == 'installation manifest'
    assert not events


@pytest.mark.parametrize('damage', ['missing_identity_and_source', 'missing_file', 'null_identity',
                                   'boolean_schema', 'claim_signature', 'claim_gui',
                                   'different_hash', 'extra_field', 'missing_python', 'missing_source'])
def test_native_identity_cannot_claim_unverified_acceptance(tmp_path, monkeypatch, damage):
    manifest = _native_manifest(tmp_path, monkeypatch)
    identity = manifest['native_launcher']
    if damage == 'missing_identity_and_source':
        del manifest['native_launcher']
        del manifest['source_commit']
    elif damage == 'missing_file':
        del manifest['files']['ResearchLibrary.exe']
    elif damage == 'null_identity':
        manifest['native_launcher'] = None
    elif damage == 'boolean_schema':
        identity['schema'] = True
    elif damage == 'claim_signature':
        identity['authenticode_signed'] = True
    elif damage == 'claim_gui':
        identity['gui_verified'] = True
    elif damage == 'different_hash':
        identity['sha256'] = manifest['files']['ResearchLibrary.exe'] = 'f' * 64
    elif damage == 'extra_field':
        identity['approved_by_filename'] = True
    elif damage == 'missing_python':
        manifest['bundled_python'] = False
    elif damage == 'missing_source':
        del manifest['source_commit']
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    events = []
    controlled_imports(monkeypatch, events)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'FAIL'
    assert report['failed_component'] == 'installation manifest'
    assert not events


@pytest.mark.parametrize('failure', [None, 'pypdfium2.raw'])
def test_loader_handle_survives_imports_and_closes_on_success_or_failure(
        tmp_path, monkeypatch, failure):
    bundle(tmp_path)
    events = []
    handle = SimpleNamespace(close=lambda: events.append('close_native_handle'))
    controlled_imports(monkeypatch, events, failure=failure, native_handle=handle)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == ('FAIL' if failure else 'PASS')
    assert events[-1] == 'close_native_handle'
    assert events.count('close_native_handle') == 1
    assert events.index('pypdfium2.raw') < events.index('close_native_handle')


@pytest.mark.parametrize('module', [name for _, name in native_checks.NATIVE_MODULES])
def test_missing_native_dll_is_failure_json_and_nonzero(tmp_path, monkeypatch, capsys, module):
    bundle(tmp_path)
    controlled_imports(monkeypatch, [], failure=module)
    monkeypatch.setattr(native_checks.sys, 'platform', 'win32')
    assert native_checks.main(['--bundle-root', str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == report['windows_execution'] == 'FAIL'
    assert 'MSVCP140.dll' in report['error']
    assert report['windows_nvda'] == 'NOT_RUN'


def test_runtime_loader_failure_prevents_native_imports(tmp_path, monkeypatch):
    bundle(tmp_path, model=True, ocr=True)
    events = []
    controlled_imports(monkeypatch, events, failure='carr_library.native_runtime')
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'FAIL'
    assert report['failed_component'] == 'bundled native runtime'
    assert report['semantic']['status'] == report['ocr']['status'] == 'NOT_RUN'
    assert events == ['carr_library.native_runtime']


@pytest.mark.parametrize('flag', [False, True])
def test_native_runtime_flag_is_delegated_to_canonical_loader(tmp_path, monkeypatch, flag):
    manifest = bundle(tmp_path)
    manifest['bundled_native_runtime'] = flag
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    events = []
    controlled_imports(monkeypatch, events)
    assert native_checks.check_native_dependencies(tmp_path)['status'] == 'PASS'
    assert events[1] == ('activate_bundled_runtime', str(tmp_path))


def test_optional_semantic_uses_shared_policy_before_tokenizers(tmp_path, monkeypatch):
    bundle(tmp_path, model=True)
    events = []
    controlled_imports(monkeypatch, events)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'PASS'
    assert events.index('carr_library.offline_policy') < events.index('disable_native_telemetry')
    assert events.index('disable_native_telemetry') < events.index('tokenizers.tokenizers')
    assert 'onnxruntime' not in events  # Only the canonical policy may initialize it.
    assert report['semantic'] == {'status': 'PASS', 'telemetry': 'disabled', 'inference': 'NOT_RUN'}


def test_failed_telemetry_policy_fails_before_tokenizer_or_ocr(tmp_path, monkeypatch):
    bundle(tmp_path, model=True, ocr=True)
    events = []
    controlled_imports(monkeypatch, events, telemetry_failure=True)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'FAIL'
    assert 'offline' in report['error']
    assert 'tokenizers.tokenizers' not in events
    assert 'carr_library.extract.ocr_runtime' not in events


@pytest.mark.parametrize('status', ['PASS', 'FAIL'])
def test_bundled_ocr_reuses_canonical_check_and_restores_environment(
        tmp_path, monkeypatch, status):
    bundle(tmp_path, ocr=True)
    events = []
    controlled_imports(monkeypatch, events, ocr_status=status)
    monkeypatch.setenv('CARR_OCR_DIR', '/previous-explicit-runtime')
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == status
    assert ('canonical_ocr_check', 30, str(tmp_path / 'ocr')) in events
    assert os.environ['CARR_OCR_DIR'] == '/previous-explicit-runtime'


@pytest.mark.parametrize('change', [
    'duplicate_key', 'boolean_schema', 'string_model_flag', 'missing_ocr_flag',
    'unknown_field', 'bad_commit', 'bad_hash', 'traversal', 'case_collision', 'bad_json',
    'string_native_flag', 'string_audio_flag', 'string_citation_flag', 'bad_tree',
    'bad_parent_commit', 'bad_parent_hash', 'bad_private_scope',
])
def test_invalid_manifest_rejected_before_native_imports(tmp_path, monkeypatch, change):
    manifest = bundle(tmp_path)
    if change == 'boolean_schema':
        manifest['schema'] = True
    elif change == 'string_model_flag':
        manifest['bundled_model'] = 'false'
    elif change == 'missing_ocr_flag':
        del manifest['bundled_ocr']
    elif change == 'unknown_field':
        manifest['disable_checks'] = True
    elif change == 'bad_commit':
        manifest['source_commit'] = 'HEAD'
    elif change == 'bad_hash':
        manifest['files']['README.txt'] = 'not-a-sha256'
    elif change == 'traversal':
        manifest['files']['../outside.dll'] = 'a' * 64
    elif change == 'case_collision':
        manifest['files']['readme.TXT'] = 'b' * 64
    elif change == 'string_native_flag':
        manifest['bundled_native_runtime'] = 'false'
    elif change == 'string_audio_flag':
        manifest['bundled_audio'] = 'false'
    elif change == 'string_citation_flag':
        manifest['bundled_citation_runtime'] = 'false'
    elif change == 'bad_tree':
        manifest['source_tree'] = 'HEAD^{tree}'
    elif change == 'bad_parent_commit':
        manifest['parent_source_commit'] = 'HEAD'
    elif change == 'bad_parent_hash':
        manifest['parent_manifest_sha256'] = 'not-a-sha256'
    elif change == 'bad_private_scope':
        manifest['use_scope'] = 'public-release'
    raw = json.dumps(manifest)
    if change == 'duplicate_key':
        raw = '{"bundled_model":true,' + raw[1:]
    elif change == 'bad_json':
        raw = '{'
    (tmp_path / 'bundle-manifest.json').write_text(raw, encoding='utf-8')
    events = []
    controlled_imports(monkeypatch, events)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'FAIL'
    assert report['failed_component'] == 'installation manifest'
    assert not events
