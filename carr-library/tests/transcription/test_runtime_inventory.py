"""Real selected-file and inert installed-inventory boundaries; no ASR execution."""
from hashlib import sha256
from importlib import metadata
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from carr_library.transcription import runtime as module


def environment(runtime):
    return runtime.python.parent.parent.resolve()


def inspect(runtime, **kwargs):
    return module.inspect_runtime(runtime.python, runtime.worker, runtime.model, **kwargs)


def info(runtime, name='faster-whisper'):
    return runtime.site.resolve() / (name.replace('-', '_') + '-0.0.synthetic.dist-info')


class ReadHook:
    """Observe an actual held descriptor; never fabricate read bytes or stats."""
    def __init__(self, stream, before=None, after=None):
        self.stream, self.before, self.after = stream, before, after
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def fileno(self):
        return self.stream.fileno()

    def read(self, count):
        self.requests.append(count)
        if self.before is not None:
            hook, self.before = self.before, None
            hook()
        value = self.stream.read(count)
        if self.after is not None:
            hook, self.after = self.after, None
            hook()
        return value


def test_valid_inventory_preserves_importlib_profile_and_never_executes_selected_code(
        synthetic_asr_runtime, monkeypatch):
    runtime = synthetic_asr_runtime
    expected = {}
    for name in module.RUNTIME_DISTRIBUTIONS:
        distribution = metadata.PathDistribution(info(runtime, name))
        files = {}
        for relative in sorted(distribution.files, key=str):
            raw = Path(distribution.locate_file(relative)).read_bytes()
            files[str(relative)] = {'sha256': sha256(raw).hexdigest(), 'size': len(raw)}
        expected[name] = {
            'version': distribution.version, 'file_count': len(files),
            'installed_files_sha256': sha256(json.dumps(files, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}
    payload = b"raise AssertionError('A selected environment must never execute during inspection.')\n"
    (runtime.site / 'sitecustomize.py').write_bytes(payload)
    (runtime.site / 'review-only.pth').write_bytes(b'import forbidden_selected_asr_site\n')
    before = tuple(sys.path)

    def forbidden(*args, **kwargs):
        raise AssertionError('Inspection must neither launch Python nor reread with PathDistribution.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(metadata, 'PathDistribution', forbidden)
    manifest = inspect(runtime)
    assert manifest['runtime_distributions'] == expected
    assert set(manifest['runtime_auxiliary']) == {
        str(runtime.site.resolve() / 'sitecustomize.py'), str(runtime.site.resolve() / 'review-only.pth')}
    assert module.verified_runtime_paths(manifest) == (runtime.site.resolve(),)
    assert tuple(sys.path) == before
    assert 'forbidden_selected_asr_site' not in sys.modules
    assert manifest['schema'] == 'resonance-transcription-runtime/1'
    assert manifest['runtime_capability'] == 'installed-files-reviewed; execution-and-quality-unqualified'


@pytest.mark.parametrize('field', ['python_identity', 'python_configuration', 'runtime_distributions'])
def test_self_consistent_changed_manifest_cannot_supply_unreviewed_import_roots(
        synthetic_asr_runtime, field):
    profile = inspect(synthetic_asr_runtime)
    profile[field] = {}
    profile['fingerprint'] = module.fingerprint(
        {key: value for key, value in profile.items() if key != 'fingerprint'})
    with pytest.raises(ValueError, match='changed since'):
        module.verified_runtime_paths(profile)


def test_actual_changed_package_rejects_old_review(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    profile = inspect(runtime)
    runtime.dependencies['av'].write_bytes(b'# Different installed bytes.\n')
    with pytest.raises(ValueError, match='changed since'):
        module.verified_runtime_paths(profile)


def test_captured_worker_bytes_use_closed_noninheritable_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'worker.py'
    raw = b'print("reviewed worker source")\n'
    path.write_bytes(raw)
    original = module._open_selected_file
    opened = []

    def observed(selected):
        stream = original(selected)
        opened.append(stream)
        assert not os.get_inheritable(stream.fileno())
        return stream
    monkeypatch.setattr(module, '_open_selected_file', observed)
    assert module.read_file_bytes(path) == raw
    assert module.read_file_bytes(path, maximum=32 * 1024**2) == raw
    assert module.file_identity(path) == {'sha256': sha256(raw).hexdigest(), 'size': len(raw)}
    assert len(opened) == 3 and all(stream.closed for stream in opened)


@pytest.mark.parametrize('api', ['read_file_bytes', 'file_identity'])
def test_equal_byte_replacement_after_selection_is_not_the_opened_file(tmp_path, monkeypatch, api):
    target, replacement = tmp_path / 'selected.py', tmp_path / 'replacement.py'
    target.write_bytes(b'# same bytes\n')
    replacement.write_bytes(target.read_bytes())
    original = module._open_selected_file
    replaced = []

    def raced(path):
        if path == target.resolve() and not replaced:
            os.replace(replacement, target)
            replaced.append(True)
        return original(path)
    monkeypatch.setattr(module, '_open_selected_file', raced)
    with pytest.raises(ValueError, match='changed while opening'):
        getattr(module, api)(target)
    assert replaced == [True]
    assert target.read_bytes() == b'# same bytes\n'


@pytest.mark.parametrize('api', ['read_file_bytes', 'file_identity'])
def test_growth_after_descriptor_validation_is_bounded_by_captured_length(tmp_path, monkeypatch, api):
    target = tmp_path / 'selected.py'
    target.write_bytes(b'# small captured input\n')
    length = target.stat().st_size
    original = module._open_selected_file
    opened = []

    def raced(path):
        def grow():
            with path.open('ab') as writer:
                writer.write(b'x' * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        wrapped = ReadHook(original(path), before=grow)
        opened.append(wrapped)
        return wrapped
    monkeypatch.setattr(module, '_open_selected_file', raced)
    with pytest.raises(ValueError, match='grew beyond'):
        getattr(module, api)(target)
    assert opened[0].requests == [length + 1]
    assert opened[0].stream.closed


@pytest.mark.parametrize('api', ['read_file_bytes', 'file_identity'])
def test_cancellation_after_actual_read_closes_descriptor_without_result(tmp_path, monkeypatch, api):
    target = tmp_path / 'selected.py'
    target.write_bytes(b'real selected bytes\n')
    cancel = threading.Event()
    original = module._open_selected_file
    opened = []

    def raced(path):
        wrapped = ReadHook(original(path), after=cancel.set)
        opened.append(wrapped)
        return wrapped
    monkeypatch.setattr(module, '_open_selected_file', raced)
    with pytest.raises(InterruptedError, match='cancelled'):
        getattr(module, api)(target, cancel=cancel.is_set)
    assert cancel.is_set() and opened[0].stream.closed


@pytest.mark.parametrize('name', ['METADATA', 'RECORD'])
def test_growing_metadata_and_record_are_captured_before_parser_reads(
        synthetic_asr_runtime, monkeypatch, name):
    runtime = synthetic_asr_runtime
    target = info(runtime) / name
    length = target.stat().st_size
    original = module._open_selected_file
    opened = []

    def raced(path):
        stream = original(path)
        if path != target or opened:
            return stream
        def grow():
            with path.open('ab') as writer:
                writer.write(b'x' * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        wrapped = ReadHook(stream, before=grow)
        opened.append(wrapped)
        return wrapped
    monkeypatch.setattr(module, '_open_selected_file', raced)
    with pytest.raises(ValueError, match='grew beyond'):
        inspect(runtime)
    assert opened[0].requests == [length + 1]
    assert opened[0].stream.closed


@pytest.mark.parametrize('name', ['METADATA', 'RECORD'])
def test_parsed_declaration_must_match_final_installed_inventory(
        synthetic_asr_runtime, name):
    runtime = synthetic_asr_runtime
    target = info(runtime) / name
    original = target.read_bytes()
    changed = []

    def progress(event):
        if changed or not event['current'].startswith('faster-whisper:'):
            return
        if name == 'METADATA':
            target.write_bytes(original.replace(b'Version: 0.0.synthetic', b'Version: 0.1.synthetic'))
        else:
            target.write_bytes(original + b'unreviewed-new-file.py,,\n')
        changed.append(True)
    with pytest.raises(ValueError, match='metadata changed'):
        inspect(runtime, progress=progress)
    assert changed == [True]


class CountedDirectory:
    def __init__(self, stream, seen):
        self.stream, self.seen, self.closed = stream, seen, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        self.stream.close()

    def __iter__(self):
        for entry in self.stream:
            self.seen.append(entry.name)
            yield entry


def counted_scan(monkeypatch, selected):
    original, seen, opened = os.scandir, [], []
    def scan(path):
        stream = original(path)
        if isinstance(path, int) or Path(path).resolve() != selected.resolve():
            return stream
        wrapped = CountedDirectory(stream, seen)
        opened.append(wrapped)
        return wrapped
    monkeypatch.setattr(module.os, 'scandir', scan)
    return seen, opened


def test_site_scan_counts_ignored_entries_and_stops_before_collecting_rest(
        synthetic_asr_runtime, monkeypatch):
    runtime = synthetic_asr_runtime
    for number in range(6):
        (runtime.site / f'ignored-{number}').write_bytes(b'')
    seen, opened = counted_scan(monkeypatch, runtime.site)
    monkeypatch.setattr(module, 'MAX_RUNTIME_ENTRIES', 3)
    with pytest.raises(ValueError, match='directory inventory budget'):
        module._runtime_inventory([environment(runtime)])
    assert len(seen) == 4 and opened[0].closed


def test_layout_scan_counts_unrelated_entries_incrementally(synthetic_asr_runtime, monkeypatch):
    root = environment(synthetic_asr_runtime)
    directory = root / 'lib'
    directory.mkdir(exist_ok=True)
    for number in range(6):
        (directory / f'ignored-{number}').write_bytes(b'')
    seen, opened = counted_scan(monkeypatch, directory)
    monkeypatch.setattr(module, 'MAX_MODEL_ENTRIES', 3)
    with pytest.raises(ValueError, match='layout inventory budget'):
        module._site_packages([root])
    assert len(seen) == 4 and opened[0].closed


def test_cancel_is_checked_for_each_site_entry(synthetic_asr_runtime, monkeypatch):
    runtime = synthetic_asr_runtime
    seen, opened = counted_scan(monkeypatch, runtime.site)
    with pytest.raises(InterruptedError, match='cancelled'):
        module._runtime_inventory([environment(runtime)], cancel=lambda: len(seen) >= 4)
    assert len(seen) == 4 and opened[0].closed


def test_unreadable_selected_directory_is_not_silently_missing(synthetic_asr_runtime, monkeypatch):
    runtime = synthetic_asr_runtime
    original = os.scandir
    def denied(path):
        if not isinstance(path, int) and Path(path).resolve() == runtime.site.resolve():
            raise PermissionError('actual selected directory denied')
        return original(path)
    monkeypatch.setattr(module.os, 'scandir', denied)
    with pytest.raises(PermissionError, match='actual selected directory denied'):
        module._runtime_inventory([environment(runtime)])


def test_auxiliary_inventory_is_bounded_before_any_selected_file_read(
        synthetic_asr_runtime, monkeypatch):
    runtime = synthetic_asr_runtime
    for number in range(3):
        (runtime.site / f'optional-{number}.pth').write_bytes(b'# inert\n')
    monkeypatch.setattr(module, 'MAX_RUNTIME_AUXILIARY_FILES', 2)
    opened = []
    original = module._open_selected_file
    def observed(path):
        opened.append(path)
        return original(path)
    monkeypatch.setattr(module, '_open_selected_file', observed)
    with pytest.raises(ValueError, match='auxiliary-file budget'):
        module._runtime_inventory([environment(runtime)])
    assert opened == []


@pytest.mark.parametrize('limit, message', [
    ('MAX_RUNTIME_RECORDS', 'installed-file budget'),
    ('MAX_RUNTIME_METADATA_BYTES', 'metadata byte budget'),
])
def test_captured_metadata_and_records_keep_total_bounds(synthetic_asr_runtime, monkeypatch, limit, message):
    monkeypatch.setattr(module, limit, 2)
    with pytest.raises(ValueError, match=message):
        module._runtime_inventory([environment(synthetic_asr_runtime)])


def test_site_membership_changed_during_file_review_is_rejected(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    changed = []
    def progress(event):
        if not changed:
            (runtime.site / 'arrived-after-selection.py').write_bytes(b'# not reviewed\n')
            changed.append(True)
    with pytest.raises(ValueError, match='directory changed'):
        module._runtime_inventory([environment(runtime)], progress=progress)
    assert changed == [True]


@pytest.mark.parametrize('maximum', [0, -1, True, '32', 32 * 1024**2 + 1])
def test_byte_capture_refuses_invalid_limits_before_open(tmp_path, monkeypatch, maximum):
    path = tmp_path / 'worker.py'
    path.write_bytes(b'# selected\n')
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid byte limits must be rejected before opening.')
    monkeypatch.setattr(module, '_open_selected_file', forbidden)
    with pytest.raises(ValueError, match='positive selected-file byte budget'):
        module.read_file_bytes(path, maximum=maximum)


# PR197 recorded-dependency cases reuse the current captured inventory reader.
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


def test_valid_absolute_record(root):
    base, site = root
    payload, info = install(site, 'tqdm')
    (info / 'RECORD').write_text(
        f'{payload.as_posix()},,\n{(info / "METADATA").as_posix()},,\n{(info / "RECORD").as_posix()},,\n',
        encoding='utf-8')
    inventory, _ = module._runtime_inventory([base])
    assert inventory['tqdm']['file_count'] == 3

def test_additional_profile_is_rechecked_at_the_actual_import_root_boundary(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    old = inspect(runtime)
    payload, _ = install(runtime.site, 'tqdm')
    with pytest.raises(ValueError, match='changed since'):
        module.verified_runtime_paths(old)
    reviewed = inspect(runtime)
    assert reviewed['runtime_distributions'] == old['runtime_distributions']
    assert module.verified_runtime_paths(reviewed) == (runtime.site.resolve(),)
    changed = dict(reviewed, runtime_additional_distributions={})
    changed['fingerprint'] = module.fingerprint(
        {name: value for name, value in changed.items() if name != 'fingerprint'})
    with pytest.raises(ValueError, match='changed since'):
        module.verified_runtime_paths(changed)
    payload.write_bytes(payload.read_bytes() + b'# changed optional installed package\n')
    with pytest.raises(ValueError, match='changed since'):
        module.verified_runtime_paths(reviewed)
    assert inspect(runtime)['fingerprint'] != reviewed['fingerprint']


@pytest.mark.parametrize('broken', ['missing', 'oversized', 'no-version', 'bad-name'])
def test_additional_metadata_is_never_silently_ignored(root, broken):
    base, site = root
    _, selected = install(site, 'tqdm')
    meta = selected / 'METADATA'
    if broken == 'missing':
        meta.unlink()
    elif broken == 'oversized':
        meta.write_bytes(b'x' * (1024**2 + 1))
    elif broken == 'no-version':
        meta.write_text('Metadata-Version: 2.1\nName: tqdm\n', encoding='utf-8')
    else:
        meta.write_text('Metadata-Version: 2.1\nName: invalid/name\nVersion: 1\n', encoding='utf-8')
    with pytest.raises(ValueError, match='bounded METADATA|valid name or version'):
        module._runtime_inventory([base])


def test_all_recorded_packages_share_one_installed_byte_bound(root, monkeypatch):
    base, _ = root
    read = module.file_identity
    actual = []
    monkeypatch.setattr(module, 'MAX_RUNTIME_BYTES', 8)
    def bounded(path, **options):
        actual.append((Path(path), options.get('maximum')))
        return read(path, **options)
    monkeypatch.setattr(module, 'file_identity', bounded)
    with pytest.raises(ValueError, match='byte budget'):
        module._runtime_inventory([base])
    assert actual and actual[0][1] == 8
