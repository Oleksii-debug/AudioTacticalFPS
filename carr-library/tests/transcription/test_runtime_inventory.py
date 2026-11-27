"""Synthetic installation metadata: no ASR package or model is executed."""
import shutil

import pytest

from carr_library.transcription import runtime as module


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
    before, _ = module._runtime_inventory([base])
    assert 'tqdm' in before
    payload.write_bytes(b'# modified only\n')
    after, _ = module._runtime_inventory([base])
    assert before['tqdm'] != after['tqdm']
    assert before['faster-whisper'] == after['faster-whisper']


def test_duplicate_normalized_distribution_rejected(root):
    base, site = root
    install(site, 'Extra.Dep')
    install(site, 'extra-dep', suffix='0.2')
    with pytest.raises(ValueError, match='ambiguous installed distribution'):
        module._runtime_inventory([base])


def test_additional_missing_record_rejected(root):
    base, site = root
    install(site, 'tqdm', record=False)
    with pytest.raises(ValueError, match='bounded installed file inventory'):
        module._runtime_inventory([base])


def test_missing_required_distribution_rejected(root):
    base, site = root
    shutil.rmtree(site / 'faster_whisper-0.1.dist-info')
    with pytest.raises(ValueError, match='faster-whisper'):
        module._runtime_inventory([base])


def test_total_file_budget(root, monkeypatch):
    base, _ = root
    monkeypatch.setattr(module, 'MAX_RUNTIME_FILES', 4)
    with pytest.raises(ValueError, match='total installed-file budget'):
        module._runtime_inventory([base])


def test_cancellation(root):
    base, _ = root
    with pytest.raises(InterruptedError):
        module._runtime_inventory([base], cancel=lambda: True)


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
    legacy = module.inspect_runtime(executable, worker, model)
    assert 'runtime_additional_distributions' not in legacy
    payload, _ = install(site, 'tqdm')
    before = module.inspect_runtime(executable, worker, model)
    assert before['runtime_distributions'] == legacy['runtime_distributions']
    assert set(before['runtime_distributions']) == set(module.RUNTIME_DISTRIBUTIONS)
    assert set(before['runtime_additional_distributions']) == {'tqdm'}
    assert before['fingerprint'] != legacy['fingerprint']
    payload.write_bytes(b'# modified only\n')
    after = module.inspect_runtime(executable, worker, model)
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
        module._runtime_inventory([base])


def test_duplicate_absolute_alias(root):
    base, site = root
    payload, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write(f'{payload.as_posix()},,\n')
    with pytest.raises(ValueError, match='duplicate resolved installed-file records'):
        module._runtime_inventory([base])


def test_link_within_root(root):
    base, site = root
    payload, _ = install(site, 'tqdm')
    target = site / 'other.py'
    payload.rename(target)
    try:
        payload.symlink_to(target)
    except OSError:
        pytest.skip('No symlink permission')
    with pytest.raises(ValueError, match='without links'):
        module._runtime_inventory([base])


def test_valid_absolute_record(root):
    base, site = root
    payload, info = install(site, 'tqdm')
    (info / 'RECORD').write_text(
        f'{payload.as_posix()},,\n{(info / "METADATA").as_posix()},,\n{(info / "RECORD").as_posix()},,\n',
        encoding='utf-8')
    inventory, _ = module._runtime_inventory([base])
    assert inventory['tqdm']['file_count'] == 3


@pytest.mark.parametrize('entry', ('absent_dependency.py', 'absent_dependency.pyd'))
def test_missing_recorded_payload_rejected(root, entry):
    base, site = root
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write(f'{entry},,\n')
    with pytest.raises((FileNotFoundError, ValueError)):
        module._runtime_inventory([base])


def test_dangling_recorded_link_rejected(root):
    base, site = root
    payload, _ = install(site, 'tqdm')
    payload.unlink()
    try:
        payload.symlink_to(site / 'missing.py')
    except OSError:
        pytest.skip('No symlink permission')
    with pytest.raises((FileNotFoundError, ValueError)):
        module._runtime_inventory([base])


def test_missing_generated_bytecode_is_explicitly_excluded(root):
    base, site = root
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write('__pycache__/tqdm.cpython-311.pyc,,\n')
    inventory, _ = module._runtime_inventory([base])
    assert inventory['tqdm']['file_count'] == 3


@pytest.mark.parametrize('row', ('only_one_column\n', 'file.py,,,\n', ',,\n'))
def test_malformed_raw_record_row(root, row):
    base, site = root
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write(row)
    with pytest.raises(ValueError, match='malformed RECORD'):
        module._runtime_inventory([base])


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
        module._runtime_inventory([base])



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
    previous_packages, previous_auxiliary = module._runtime_inventory([base])
    assert str(payload) in previous_auxiliary
    assert previous_auxiliary[str(payload)]['size'] == 5
    payload.write_bytes(b'PYC-B' if change == 'same_size' else b'PYC-INCREASED')
    next_packages, next_auxiliary = module._runtime_inventory([base])
    assert next_packages == previous_packages
    assert next_auxiliary != previous_auxiliary
    assert next_auxiliary[str(payload)]['sha256'] != previous_auxiliary[str(payload)]['sha256']


def test_installed_bytecode_identity_stable_when_unchanged(root):
    base, site = root
    folder = site / '__pycache__'
    folder.mkdir()
    (folder / 'warm.cpython-311.pyc').write_bytes(b'PYC-WARM')
    assert module._runtime_inventory([base]) == module._runtime_inventory([base])


def test_bytecode_inventory_recognizes_legacy_pyo(root):
    base, site = root
    legacy = site / 'old.pyo'
    legacy.write_bytes(b'PYO')
    _, auxiliary = module._runtime_inventory([base])
    assert auxiliary[str(legacy)]['size'] == 3


def test_bytecode_inventory_scans_optional_recorded_cache(root):
    base, site = root
    cached = site / '__pycache__' / 'tqdm.cpython-311.pyc'
    cached.parent.mkdir()
    cached.write_bytes(b'RECORDED-PYC')
    _, info = install(site, 'tqdm')
    with (info / 'RECORD').open('a', encoding='utf-8') as stream:
        stream.write('__pycache__/tqdm.cpython-311.pyc,,\n')
    inventory, auxiliary = module._runtime_inventory([base])
    assert inventory['tqdm']['file_count'] == 3
    assert auxiliary[str(cached)]['size'] == len(b'RECORDED-PYC')


def test_bytecode_inventory_honors_file_budget(root, monkeypatch):
    base, site = root
    (site / 'payload.pyc').write_bytes(b'PYC')
    monkeypatch.setattr(module, 'MAX_RUNTIME_BYTECODE_FILES', 0)
    with pytest.raises(ValueError, match='bytecode file budget'):
        module._runtime_inventory([base])


def test_bytecode_inventory_honors_byte_budget(root, monkeypatch):
    base, site = root
    (site / 'payload.pyc').write_bytes(b'PYC')
    monkeypatch.setattr(module, 'MAX_RUNTIME_BYTECODE_BYTES', 2)
    with pytest.raises(ValueError, match='byte budget'):
        module._runtime_inventory([base])


def test_bytecode_inventory_honors_walk_budget(root, monkeypatch):
    base, _ = root
    monkeypatch.setattr(module, 'MAX_RUNTIME_SCAN_ENTRIES', 0)
    with pytest.raises(ValueError, match='bytecode-scan entry budget'):
        module._runtime_inventory([base])


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
    initial = module.inspect_runtime(executable, worker, model)
    bytecode.write_bytes(b'CHANGED!')
    changed = module.inspect_runtime(executable, worker, model)
    assert initial['runtime_distributions'] == changed['runtime_distributions']
    assert initial['runtime_auxiliary'] != changed['runtime_auxiliary']
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
    inventory, _ = module._runtime_inventory([base])
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
        module._runtime_inventory([base])


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
        module._runtime_inventory([base])


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
    before, auxiliary = module._runtime_inventory([base])
    compile_marker('changed')
    assert marker() == 'changed'
    after, changed = module._runtime_inventory([base])
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
    before, auxiliary = module._runtime_inventory([base])
    payload.write_bytes(b'two')
    after, changed = module._runtime_inventory([base])
    assert before == after
    assert auxiliary[str(payload)] != changed[str(payload)]


@pytest.mark.parametrize('suffix', ['.pyc', '.pyo'])
@pytest.mark.parametrize('case', ['absent-in-root', 'absent-outside-root', 'dangling-link',
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
    elif case == 'dangling-link':
        try:
            payload.symlink_to(other / 'absent')
        except OSError:
            pytest.skip('No symlink permission')
    elif case == 'hardlink':
        target = other / 'target'
        target.write_bytes(b'bytes')
        try:
            os.link(target, payload)
        except OSError:
            pytest.skip('No hardlink support')
    elif case == 'duplicate-alias':
        payload.write_bytes(b'bytes')
        _append_recorded_path(info, '../../other/' + payload.name)
    _append_recorded_path(info, payload.as_posix())
    if case == 'absent-in-root':
        module._runtime_inventory([base])
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            module._runtime_inventory([base])


@pytest.mark.parametrize('leaf', ['file.py', 'file.json', 'file.pyd'])
def test_absent_explicit_nonbytecode_cache_record_rejected(root, leaf):
    base, site = root
    _, info = install(site, 'extra')
    _append_recorded_path(info, '__pycache__/' + leaf)
    with pytest.raises((ValueError, FileNotFoundError)):
        module._runtime_inventory([base])
