"""PR197 guards on the existing flat supplied-worker runtime.

Pure inventories use inert installation records. Child cases reuse the actual
public receipts/V5 peers; the CPython bytecode proof imports a harmless marker.
No ASR inference, model download, or external network is involved.
"""
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import shutil

import pytest

from carr_library import transcription, transcription_runtime as module
from carr_library.application import LibraryApp
from carr_library.durable_state import read_json
from carr_library.library_backup import restore_backup



def _inventory(base, **kwargs):
    """Project the real host seal for common RECORD/bytecode assertions."""
    core, paths, additional, bytecode = module._runtime_inventory([base], **kwargs)
    return {**core, **additional}, {**paths, **bytecode}



def install(site, name, *, suffix='0.1', record=True):
    package = name.replace('-', '_') + '.py'
    payload = site / package
    payload.write_bytes(b'# fixture only\n')
    info = site / (name.replace('-', '_') + '-' + suffix + '.dist-info')
    info.mkdir()
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {suffix}\n', encoding='utf-8')
    if record:
        (info / 'RECORD').write_text(f'{package},,\n{info.name}/METADATA,,\n{info.name}/RECORD,,\n', encoding='utf-8')
    return payload, info


@pytest.fixture
def root(tmp_path):
    root = tmp_path / 'Синтетичний runtime'
    site = root / 'Lib' / 'site-packages'
    site.mkdir(parents=True)
    for name in module.RUNTIME_DISTRIBUTIONS:
        install(site, name)
    return root, site


def test_additional_distribution_bound(root):
    base, site = root
    payload, _ = install(site, 'tqdm')
    before, _ = _inventory(base)
    assert 'tqdm' in before
    payload.write_bytes(b'# modified only\n')
    after, _ = _inventory(base)
    assert before['tqdm'] != after['tqdm']
    assert before['faster-whisper'] == after['faster-whisper']


def test_duplicate_normalized_distribution_rejected(root):
    base, site = root
    install(site, 'Extra.Dep')
    install(site, 'extra-dep', suffix='0.2')
    with pytest.raises(ValueError, match='ambiguous installed distribution'):
        _inventory(base)


def test_additional_missing_record_rejected(root):
    base, site = root
    install(site, 'tqdm', record=False)
    with pytest.raises(ValueError, match='bounded installed file inventory'):
        _inventory(base)


def test_absent_core_distribution_remains_explicit_unqualified_data(root):
    base, site = root
    shutil.rmtree(site / 'faster_whisper-0.1.dist-info')
    inventory, _ = _inventory(base)
    assert inventory['faster-whisper'] == {'present': False}
    assert all(inventory[name]['present'] is True
               for name in module.RUNTIME_DISTRIBUTIONS if name != 'faster-whisper')


def test_total_file_budget(root, monkeypatch):
    base, _ = root
    monkeypatch.setattr(module, 'MAX_RUNTIME_FILES', 4)
    with pytest.raises(ValueError, match='total installed-file budget'):
        _inventory(base)


def test_cancellation(root):
    base, _ = root
    with pytest.raises(InterruptedError):
        _inventory(base, cancel=lambda: True)


def test_profile_additional_identity_preserves_worker_checkpoint_dialect(root):
    base, site = root
    executable = base / 'python.exe'
    executable.write_bytes(b'fixture interpreter; not executed')
    worker = base / 'worker.py'
    worker.write_bytes(b'# fixture worker; not executed')
    model = base / 'model'
    model.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'fixture model identity')
    legacy = module.inspect_python(executable)
    assert 'runtime_additional_distributions' not in legacy
    payload, _ = install(site, 'tqdm')
    before = module.inspect_python(executable)
    assert before['runtime_distributions'] == legacy['runtime_distributions']
    assert set(before['runtime_distributions']) == set(module.RUNTIME_DISTRIBUTIONS)
    assert set(before['runtime_additional_distributions']) == {'tqdm'}
    assert before['fingerprint'] != legacy['fingerprint']
    payload.write_bytes(b'# modified only\n')
    after = module.inspect_python(executable)
    assert before['runtime_distributions'] == after['runtime_distributions']
    assert before['fingerprint'] != after['fingerprint']


@pytest.mark.parametrize('omitted', ('METADATA', 'RECORD'))
def test_required_inventory_records(root, omitted):
    base, site = root
    _, info = install(site, 'tqdm')
    record = info / 'RECORD'
    record.write_text(''.join(line for line in record.read_text(encoding='utf-8').splitlines(keepends=True)
                              if not line.startswith(f'{info.name}/{omitted},')), encoding='utf-8')
    with pytest.raises(ValueError, match='omits METADATA or RECORD'):
        _inventory(base)


def test_duplicate_absolute_alias(root):
    base, site = root
    payload, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write(f'{payload.as_posix()},,\n')
    with pytest.raises(ValueError, match='duplicate resolved installed-file records'):
        _inventory(base)


def test_hardlinked_recorded_payload_is_rejected_without_reading_it(root, monkeypatch):
    base, site = root
    payload, _ = install(site, 'tqdm')
    target = site / 'other.py'
    payload.rename(target)
    os.link(target, payload)
    opened = Path.open
    def no_payload(path, *args, **kwargs):
        if path in (payload, target):
            raise AssertionError('A linked installed payload reached the reader.')
        return opened(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_payload)
    with pytest.raises(ValueError, match='without links'):
        _inventory(base)


def test_valid_absolute_record(root):
    base, site = root
    payload, info = install(site, 'tqdm')
    (info / 'RECORD').write_text(
        f'{payload.as_posix()},,\n{(info / "METADATA").as_posix()},,\n{(info / "RECORD").as_posix()},,\n',
        encoding='utf-8')
    inventory, _ = _inventory(base)
    assert inventory['tqdm']['file_count'] == 3


@pytest.mark.parametrize('entry', ('absent_dependency.py', 'absent_dependency.pyd'))
def test_missing_recorded_payload_rejected(root, entry):
    base, site = root
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write(f'{entry},,\n')
    with pytest.raises((FileNotFoundError, ValueError)):
        _inventory(base)


def test_recorded_payload_directory_is_rejected(root):
    base, site = root
    payload, _ = install(site, 'tqdm')
    payload.unlink()
    payload.mkdir()
    with pytest.raises(ValueError, match='regular local files'):
        _inventory(base)


def test_missing_generated_bytecode_is_explicitly_excluded(root):
    base, site = root
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write('__pycache__/tqdm.cpython-311.pyc,,\n')
    inventory, _ = _inventory(base)
    assert inventory['tqdm']['file_count'] == 3


@pytest.mark.parametrize('row', ('only_one_column\n', 'file.py,,,\n', ',,\n',
    'file.py,,not-a-byte-count\n', 'file.py,missing-hash-mode,1\n',
    'file.py,sha256=value,-1\n', 'file.py,,١\n'))
def test_malformed_raw_record_row(root, row):
    base, site = root
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write(row)
    with pytest.raises(ValueError, match='malformed RECORD'):
        _inventory(base)


def test_record_mutated_after_parse_rejected(root, monkeypatch):
    base, site = root
    _, info = install(site, 'tqdm')
    record = info / 'RECORD'
    original = module._record_labels

    def mutate_after_parse(path, **kwargs):
        labels, identity = original(path, **kwargs)
        if path == record:
            with record.open('a', encoding='utf-8') as stream:
                stream.write('absent_dependency.py,,\n')
        return labels, identity

    monkeypatch.setattr(module, '_record_labels', mutate_after_parse)
    with pytest.raises(ValueError, match='RECORD changed during verification'):
        _inventory(base)



# CARR-SEC-ASR-PYC-001: installed Python may execute existing .pyc
# under -I -B; RECORD need not list these files. No inference or network.
@pytest.mark.parametrize('module_name', [
    'alpha', 'script-name', 'розмова', 'δοκιμή', 'Nød', '日本語', '𐍈text', 'long_module',
])
@pytest.mark.parametrize('subdirectory', [
    '', '__pycache__', 'nested/__pycache__', 'nested/level/__pycache__',
])
@pytest.mark.parametrize('change', ['same_size', 'increased_size'])
def test_unrecorded_bytecode_mutation_invalidates_profile_auxiliary(
        root, module_name, subdirectory, change):
    base, site = root
    folder = site / subdirectory
    folder.mkdir(parents=True, exist_ok=True)
    payload = folder / (module_name + '.cpython-311.pyc')
    payload.write_bytes(b'PYC-A')
    previous_packages, previous_auxiliary = _inventory(base)
    assert str(payload) in previous_auxiliary
    assert previous_auxiliary[str(payload)]['size'] == 5
    payload.write_bytes(b'PYC-B' if change == 'same_size' else b'PYC-INCREASED')
    next_packages, next_auxiliary = _inventory(base)
    assert next_packages == previous_packages
    assert next_auxiliary != previous_auxiliary
    assert next_auxiliary[str(payload)]['sha256'] != previous_auxiliary[str(payload)]['sha256']


def test_installed_bytecode_identity_stable_when_unchanged(root):
    base, site = root
    folder = site / '__pycache__'
    folder.mkdir()
    (folder / 'warm.cpython-311.pyc').write_bytes(b'PYC-WARM')
    assert _inventory(base) == _inventory(base)


def test_bytecode_inventory_recognizes_legacy_pyo(root):
    base, site = root
    legacy = site / 'old.pyo'
    legacy.write_bytes(b'PYO')
    _, auxiliary = _inventory(base)
    assert auxiliary[str(legacy)]['size'] == 3


def test_bytecode_inventory_scans_optional_recorded_cache(root):
    base, site = root
    cached = site / '__pycache__' / 'tqdm.cpython-311.pyc'
    cached.parent.mkdir()
    cached.write_bytes(b'RECORDED-PYC')
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write('__pycache__/tqdm.cpython-311.pyc,,\n')
    inventory, auxiliary = _inventory(base)
    assert inventory['tqdm']['file_count'] == 3
    assert auxiliary[str(cached)]['size'] == len(b'RECORDED-PYC')


def test_bytecode_inventory_honors_file_budget(root, monkeypatch):
    base, site = root
    (site / 'payload.pyc').write_bytes(b'PYC')
    monkeypatch.setattr(module, 'MAX_RUNTIME_BYTECODE_FILES', 0)
    with pytest.raises(ValueError, match='bytecode file budget'):
        _inventory(base)


def test_bytecode_inventory_honors_byte_budget(root, monkeypatch):
    base, site = root
    (site / 'payload.pyc').write_bytes(b'PYC')
    monkeypatch.setattr(module, 'MAX_RUNTIME_BYTECODE_BYTES', 2)
    with pytest.raises(ValueError, match='byte budget'):
        _inventory(base)


def test_bytecode_inventory_honors_walk_budget(root, monkeypatch):
    base, _ = root
    monkeypatch.setattr(module, 'MAX_RUNTIME_SCAN_ENTRIES', 0)
    with pytest.raises(ValueError, match='bytecode-scan entry budget'):
        _inventory(base)


def test_bytecode_inventory_cancelled_on_empty_directory(tmp_path):
    directory = tmp_path / 'empty'
    directory.mkdir()
    with pytest.raises(InterruptedError):
        module._bytecode_inventory([directory], cancel=lambda: True)



def test_real_isolated_python_reads_existing_modified_bytecode(tmp_path):
    """Safe synthetic subprocess proof: -I -B does not prohibit cache reads."""
    import py_compile
    import subprocess
    import sys
    from hashlib import sha256

    source = tmp_path / 'synthetic_source.py'
    source.write_text("VALUE = 'safe'\n", encoding='utf-8')
    source_digest = sha256(source.read_bytes()).hexdigest()
    py_compile.compile(str(source), doraise=True)
    cache = next((tmp_path / '__pycache__').glob('synthetic_source.*.pyc'))
    original_bytecode = cache.read_bytes()
    replacement = tmp_path / 'synthetic_replacement.py'
    replacement.write_text("VALUE = 'changed'\n", encoding='utf-8')
    py_compile.compile(str(replacement), doraise=True)
    altered_bytecode = next((tmp_path / '__pycache__').glob('synthetic_replacement.*.pyc')).read_bytes()
    cache.write_bytes(original_bytecode[:16] + altered_bytecode[16:])
    assert source_digest == sha256(source.read_bytes()).hexdigest()
    command = ("import sys; sys.path.insert(0, {!r}); "
               "import synthetic_source; print(synthetic_source.VALUE)").format(str(tmp_path))
    altered = subprocess.run([sys.executable, '-I', '-B', '-c', command],
                             capture_output=True, text=True, check=True)
    assert altered.stdout.strip() == 'changed'
    clean = subprocess.run([sys.executable, '-I', '-B', '-X',
                            f'pycache_prefix={tmp_path / "clean-cache"}', '-c', command],
                           capture_output=True, text=True, check=True)
    assert clean.stdout.strip() == 'safe'


def test_reviewed_runtime_fingerprint_changes_for_unrecorded_bytecode(root):
    base, site = root
    executable = base / 'python.exe'
    executable.write_bytes(b'synthetic interpreter is not executed')
    worker = base / 'reviewed_worker.py'
    worker.write_bytes(b'# synthetic reviewed worker')
    model = base / 'model'
    model.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'synthetic model identity')
    bytecode = site / '__pycache__' / 'synthetic_module.cpython-311.pyc'
    bytecode.parent.mkdir()
    bytecode.write_bytes(b'ORIGINAL')
    initial = module.inspect_python(executable)
    bytecode.write_bytes(b'CHANGED!')
    changed = module.inspect_python(executable)
    assert initial['runtime_distributions'] == changed['runtime_distributions']
    assert initial['runtime_auxiliary'] == changed['runtime_auxiliary']
    assert initial['runtime_bytecode'] != changed['runtime_bytecode']
    assert initial['fingerprint'] != changed['fingerprint']



# ASR-07: distribution name/version and digest must come from one snapshot.
@pytest.mark.parametrize('version', [
    '0.1', '1.2.3', '2.0rc1', '1!2.0', '1.0+local', '1.2.post1', '1.0.dev3', '2026.11.27',
])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('name', ['Extra.Dep', 'extra_dep', 'EXTRA-DEP'])
def test_metadata_snapshot_preserves_valid_version_and_normalized_name(root, version, newline, name):
    base, site = root
    _, info = install(site, 'extra-dep')
    (info / 'METADATA').write_bytes(newline.join([
        'Metadata-Version: 2.1', f'Name: {name}', f'Version: {version}',
        'Summary: fixture', ' continuation', '', 'Description',
    ]).encode('utf-8'))
    inventory, _ = _inventory(base)
    assert inventory['extra-dep']['version'] == version
    assert inventory['extra-dep']['file_count'] == 3


@pytest.mark.parametrize('raw', [
    b'Name: extra-dep\nVersion: 0.1\nName: other\n',
    b'Name: extra-dep\nVersion: 0.1\nVersion: 0.2\n',
    b'Version: 0.1\n',
    b'Name: extra-dep\n',
    b'Name: extra-dep\nVersion: 0.1\nSummary: \xff\n',
    b'Name: extra-dep\nVersion: ' + b'1' * 1025 + b'\n',
    b'Name: extra-dep\nVersion: 1\x00\n',
    b'Name: extra-dep\nVersion: 1\n 2\n',
    b'Name: extra-dep\nVersion: 0.1\nbroken header\n',
])
def test_invalid_distribution_metadata_rejected(root, raw):
    base, site = root
    _, info = install(site, 'extra-dep')
    (info / 'METADATA').write_bytes(raw)
    with pytest.raises(ValueError, match='METADATA|valid name or version'):
        _inventory(base)


@pytest.mark.parametrize('change', ['name', 'version', 'body'])
def test_metadata_mutated_after_parse_rejected(root, monkeypatch, change):
    base, site = root
    _, info = install(site, 'extra-dep')
    target = info / 'METADATA'
    original = module._distribution_metadata

    def mutate_after_parse(path, **kwargs):
        snapshot = original(path, **kwargs)
        if path == target:
            raw = target.read_bytes()
            if change == 'name':
                raw = raw.replace(b'Name: extra-dep', b'Name: other-dep')
            elif change == 'version':
                raw = raw.replace(b'Version: 0.1', b'Version: 0.2')
            else:
                raw += b'\nChanged body'
            target.write_bytes(raw)
        return snapshot

    monkeypatch.setattr(module, '_distribution_metadata', mutate_after_parse)
    with pytest.raises(ValueError, match='METADATA changed during verification'):
        _inventory(base)


def test_metadata_snapshot_cancelled(root):
    _, site = root
    _, info = install(site, 'extra-dep')
    with pytest.raises(InterruptedError):
        module._distribution_metadata(info / 'METADATA', cancel=lambda: True)


# ASR-07: recorded cache exclusions must still bind actual present payloads.
def _append_recorded_path(info, label):
    import csv
    with (info / 'RECORD').open('a', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerow([label, '', ''])


@pytest.mark.parametrize('where', ['site', 'outside-site'])
def test_real_sourceless_recorded_import_identity(root, where):
    import py_compile
    import subprocess
    import sys
    base, site = root
    _, info = install(site, 'extra')
    location = site if where == 'site' else base / 'other-modules'
    location.mkdir(exist_ok=True)
    if where != 'site':
        (site / 'local-extra.pth').write_text('../../other-modules\n', encoding='ascii')
    source = location / 'qa_marker.py'
    cache = location / 'qa_marker.pyc'

    def compile_marker(value):
        source.write_text(f'MARKER = {value!r}\n', encoding='utf-8')
        py_compile.compile(str(source), cfile=str(cache), doraise=True)
        source.unlink()

    def marker():
        command = f'import site; site.addsitedir({str(site)!r}); import qa_marker; print(qa_marker.MARKER)'
        return subprocess.check_output([sys.executable, '-I', '-B', '-c', command], text=True).strip()

    compile_marker('safe')
    _append_recorded_path(info, cache.as_posix())
    assert marker() == 'safe'
    before, auxiliary = _inventory(base)
    compile_marker('changed')
    assert marker() == 'changed'
    after, changed = _inventory(base)
    assert before == after  # Supplied worker checkpoint dialect unchanged.
    assert auxiliary[str(cache)] != changed[str(cache)]


@pytest.mark.parametrize('leaf', ['module.py', 'data.json', 'native.pyd'])
def test_explicit_nonbytecode_cache_payload_is_bound(root, leaf):
    base, site = root
    _, info = install(site, 'extra')
    payload = site / '__pycache__' / leaf
    payload.parent.mkdir()
    payload.write_bytes(b'one')
    _append_recorded_path(info, payload.relative_to(site).as_posix())
    before, auxiliary = _inventory(base)
    payload.write_bytes(b'two')
    after, changed = _inventory(base)
    assert before == after
    assert auxiliary[str(payload)] != changed[str(payload)]


@pytest.mark.parametrize('suffix', ['.pyc', '.pyo'])
@pytest.mark.parametrize('case', ['absent-in-root', 'absent-outside-root', 'directory',
                                 'hardlink', 'duplicate-alias'])
def test_explicit_excluded_record_boundaries(root, suffix, case):
    import os
    base, site = root
    _, info = install(site, 'extra')
    other = base / 'other'
    other.mkdir()
    payload = other / ('file' + suffix)
    if case == 'absent-outside-root':
        payload = base.parent / ('outside' + suffix)
    elif case == 'directory':
        payload.mkdir()
    elif case == 'hardlink':
        target = other / 'target'
        target.write_bytes(b'bytes')
        os.link(target, payload)
    elif case == 'duplicate-alias':
        payload.write_bytes(b'bytes')
        _append_recorded_path(info, '../../other/' + payload.name)
    _append_recorded_path(info, payload.as_posix())
    if case == 'absent-in-root':
        _inventory(base)
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            _inventory(base)


@pytest.mark.parametrize('leaf', ['file.py', 'file.json', 'file.pyd'])
def test_absent_explicit_nonbytecode_cache_record_rejected(root, leaf):
    base, site = root
    _, info = install(site, 'extra')
    _append_recorded_path(info, '__pycache__/' + leaf)
    with pytest.raises((ValueError, FileNotFoundError)):
        _inventory(base)



def _peer(tmp_path, *, protocol=transcription.RECEIPTS_PROTOCOL):
    fixture = runpy.run_path(str(Path(__file__).with_name('test_runtime_review.py')))
    return fixture['make_peer'](tmp_path / 'actual-peer', protocol=protocol)


def _extended_peer(tmp_path, *, protocol=transcription.RECEIPTS_PROTOCOL):
    peer = _peer(tmp_path, protocol=protocol)
    extra, info = install(peer.site, 'extra-review-dependency')
    bytecode = peer.site / '__pycache__' / 'inert-review.pyc'
    bytecode.parent.mkdir(exist_ok=True)
    bytecode.write_bytes(b'Inert bytecode inventory fixture; never imported.')
    configured = peer.service.configure(peer.python, peer.worker, peer.models, protocol=protocol)
    assert set(configured['runtime_inventory']['runtime_distributions']) == set(module.RUNTIME_DISTRIBUTIONS)
    assert set(configured['runtime_inventory']['runtime_additional_distributions']) == {'extra-review-dependency'}
    assert str(bytecode) in configured['runtime_inventory']['runtime_bytecode']
    return peer, extra, info, bytecode, configured


@pytest.mark.parametrize('changed', ['payload', 'record', 'metadata', 'bytecode'])
def test_additional_identity_drift_refuses_before_actual_worker_admission(tmp_path, monkeypatch, changed):
    peer, extra, info, bytecode, configured = _extended_peer(tmp_path)
    identity = peer.service.submit([peer.source])
    if changed == 'payload':
        extra.write_bytes(b'# Changed installed transitive package.\n')
    elif changed == 'record':
        with (info / 'RECORD').open('a', encoding='utf-8') as stream:
            stream.write('now-missing.py,,\n')
    elif changed == 'metadata':
        path = info / 'METADATA'
        path.write_bytes(path.read_bytes().replace(b'Version: 0.1', b'Version: 0.2'))
    else:
        bytecode.write_bytes(b'Changed inert bytecode bytes; never imported.')
    def forbidden(*args, **kwargs):
        raise AssertionError('A changed runtime reached the actual child launcher.')
    monkeypatch.setattr(transcription, 'run_process', forbidden)
    with pytest.raises(transcription.RuntimeReviewError):
        peer.service.run(identity, timeout=30)
    assert not (peer.service._path(identity) / 'job_status.json').exists()
    assert peer.service.get(identity)['done'] == 0
    assert peer.service._admission_broker(lambda: False).snapshot()['leases'] == []


@pytest.mark.parametrize('protocol', [transcription.RECEIPTS_PROTOCOL, transcription.V5_PROTOCOL])
def test_actual_commit_with_changed_additional_bytes_cannot_restore_old_cache(
        tmp_path, monkeypatch, protocol):
    peer, extra, _, _, _ = _extended_peer(tmp_path, protocol=protocol)
    service = peer.service
    identity = service.submit([peer.source], protocol=protocol)
    path = service._path(identity)
    original, committed = extra.read_bytes(), []
    save = service._save
    def after_candidate(where, value):
        result = save(where, value)
        if value['status'] == 'completed' and not committed:
            committed.append(True)
            extra.write_bytes(original + b'# Real post-publication dependency drift.\n')
        return result
    monkeypatch.setattr(service, '_save', after_candidate)
    result = service.run(identity, timeout=30)
    assert committed and (path / 'job_status.json').exists(), result
    assert result['status'] == 'failed' and result['done'] == 0
    assert result['receipts'] == result['artifacts'] == []
    rejected = (path / 'rejected-execution.json').read_bytes()
    extra.write_bytes(original)
    service.configure(peer.python, peer.worker, peer.models, protocol=protocol)
    with pytest.raises(transcription.RuntimeReviewError, match='rejected'):
        service.run(identity, timeout=30)
    assert (path / 'rejected-execution.json').read_bytes() == rejected
    assert service._admission_broker(lambda: False).snapshot()['leases'] == []


@pytest.mark.parametrize('protocol', [transcription.RECEIPTS_PROTOCOL, transcription.V5_PROTOCOL])
def test_extended_runtime_seal_roundtrips_passively_without_original_runtime(tmp_path, monkeypatch, protocol):
    peer, _, _, _, configured = _extended_peer(tmp_path, protocol=protocol)
    service = peer.service
    identity = service.submit([peer.source], protocol=protocol)
    result = service.run(identity, timeout=30)
    assert result['status'] == 'completed', result['last_error']
    path = service._path(identity)
    for name in ('fixture-mode.json', 'fixture-runs.json'):
        (path / name).unlink(missing_ok=True)
    source = result['sources'][0]['filename']
    text = service.text(identity, source)
    revision = service.save_edit(identity, source, 'Human edit survives passive runtime inventory restore.')
    retained = (path / 'input' / source).read_bytes()
    inventory = copy.deepcopy(configured['runtime_inventory'])
    peer.source.unlink()
    peer.worker.unlink()
    shutil.rmtree(peer.environment)
    shutil.rmtree(peer.models)
    def forbidden(*args, **kwargs):
        raise AssertionError('Passive inventory backup/restore tried to inspect or execute the removed runtime.')
    with monkeypatch.context() as scoped:
        for name in ('run_process', '_python_runtime', '_v5_model', '_v5_file'):
            scoped.setattr(transcription, name, forbidden)
        scoped.setattr(module, 'inspect_python', forbidden)
        scoped.setattr(module, 'file_identity', forbidden)
        backup = tmp_path / 'backup'
        checksum = peer.app.backup(backup)
        shutil.rmtree(peer.app.root)
        restored = restore_backup(backup, tmp_path / 'restored', expected_manifest_sha256=checksum)
        recovered = LibraryApp(restored).transcription(peer.workspace)
        recorded = recovered.configuration()
        assert recorded['runtime_inventory'] == inventory
        assert recorded['execution_review_required'] is True
        selected = next(row for row in recovered.list_runtimes() if row['id'] == configured['profile_id'])
        assert selected['configuration']['runtime_inventory'] == inventory
        assert selected['trusted'] is False and selected['restored'] is True
        verified = recovered.verify(identity)
        assert verified['status'] == 'completed'
        assert verified['receipts'] == result['receipts']
        assert verified['artifacts'] == result['artifacts']
        assert recovered.run(identity, timeout=30)['status'] == 'completed'
        assert recovered.text(identity, source) == text
        assert recovered.edits(identity)[0]['id'] == revision
        assert (recovered._path(identity) / 'input' / source).read_bytes() == retained


def test_passive_runtime_validator_accepts_historical_seal_without_external_reads(root, monkeypatch):
    base, _ = root
    executable = base / 'python.exe'
    executable.write_bytes(b'Inert selected identity; not executed.')
    legacy = module.inspect_python(executable)
    assert 'runtime_bytecode' not in legacy and 'runtime_additional_distributions' not in legacy
    def forbidden(*args, **kwargs):
        raise AssertionError('A historical passive seal opened an external runtime path.')
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'open', forbidden)
        scoped.setattr(module, 'local_path', forbidden)
        assert module.validate_runtime(legacy) is legacy


@pytest.mark.parametrize('changed', [
    'duplicate-core', 'false-additional', 'unknown-row', 'bad-name', 'empty-additional',
    'empty-bytecode', 'relative-bytecode', 'negative-size', 'unknown-field',
])
def test_passive_additional_inventory_rejects_untyped_records(root, changed):
    base, site = root
    executable = base / 'python.exe'
    executable.write_bytes(b'Inert selected identity; not executed.')
    install(site, 'extra')
    cache = site / 'inert.pyc'
    cache.write_bytes(b'Inert bytes.')
    value = module.inspect_python(executable)
    if changed == 'duplicate-core':
        value['runtime_additional_distributions']['numpy'] = value['runtime_additional_distributions'].pop('extra')
    elif changed == 'false-additional':
        value['runtime_additional_distributions']['extra'] = {'present': False}
    elif changed == 'unknown-row':
        value['runtime_additional_distributions']['extra']['command'] = 'ignored is not permitted'
    elif changed == 'bad-name':
        value['runtime_additional_distributions']['Extra.Dep'] = value['runtime_additional_distributions'].pop('extra')
    elif changed == 'empty-additional':
        value['runtime_additional_distributions'] = {}
    elif changed == 'empty-bytecode':
        value['runtime_bytecode'] = {}
    elif changed == 'relative-bytecode':
        value['runtime_bytecode']['relative.pyc'] = value['runtime_bytecode'].pop(str(cache))
    elif changed == 'negative-size':
        value['runtime_bytecode'][str(cache)]['size'] = -1
    else:
        value['run_command'] = 'No restored execution authority'
    value['fingerprint'] = module.fingerprint({key: item for key, item in value.items() if key != 'fingerprint'})
    with pytest.raises(ValueError):
        module.validate_runtime(value)



def test_core_seven_digest_matches_the_existing_worker_json_dialect(root):
    base, site = root
    core, paths, additional, bytecode = module._runtime_inventory([base])
    assert paths == additional == bytecode == {}
    assert set(core) == set(module.RUNTIME_DISTRIBUTIONS)
    for name in module.RUNTIME_DISTRIBUTIONS:
        info = name.replace('-', '_') + '-0.1.dist-info'
        labels = (name.replace('-', '_') + '.py', info + '/METADATA', info + '/RECORD')
        files = {}
        for label in labels:
            raw = (site / label).read_bytes()
            files[label] = {'sha256': sha256(raw).hexdigest(), 'size': len(raw)}
        expected = sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()
        assert core[name] == {'present': True, 'version': '0.1', 'file_count': 3,
                              'installed_files_sha256': expected}
