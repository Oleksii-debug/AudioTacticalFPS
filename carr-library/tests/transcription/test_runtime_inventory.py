"""Real selected-file and inert installed-inventory boundaries; no ASR execution."""
from hashlib import sha256
from importlib import metadata
import json
import os
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
