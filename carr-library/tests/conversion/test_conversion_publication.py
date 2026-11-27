"""Actual conversion publication boundaries; no replacement converter or output."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import carr_library.conversion.service as module
from carr_library.conversion import ConversionService
from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.models import AudioCleanupPending


@pytest.fixture
def conversion(tmp_path):
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'Джерело.txt'
    source.write_text('Actual bounded conversion.\nТочний текст.', encoding='utf-8')
    return ConversionService(outputs, input_roots=(inputs,)), source, outputs


def assert_complete(result, source, original, *, warning):
    raw = result.path.read_bytes()
    assert source.read_bytes() == original
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['text_sha256'] == sha256(raw).hexdigest()
    assert result.report['text_size'] == len(raw)
    assert result.report['verified_for_quotation'] is False
    assert result.report['original_modified'] is False
    assert json.loads(result.report_path.read_text(encoding='utf-8')) == result.report
    assert bool(result.cleanup_warning) is warning
    assert result.path.stat().st_nlink == result.report_path.stat().st_nlink == 1


@pytest.mark.parametrize('boundary', ['marker', 'directory-sync', 'staging-cleanup'])
@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_verified_pair_survives_actual_postcommit_failure(conversion, monkeypatch, boundary, failure):
    service, source, outputs = conversion
    original, real_link, real_sync = source.read_bytes(), os.link, module.sync_directory
    real_staging = module.retained_temporary_directory
    observed = []

    def link(incoming, destination, *args, **kwargs):
        value = real_link(incoming, destination, *args, **kwargs)
        if boundary == 'marker' and str(destination).endswith('.provenance.json'):
            assert (outputs / 'reading.txt').exists()
            assert os.path.samefile(incoming, destination)
            observed.append(boundary)
            raise failure('The actual completion link is already visible')
        return value

    def sync(path):
        if boundary == 'directory-sync' and Path(path) == outputs:
            assert (outputs / 'reading.txt').exists()
            assert (outputs / 'reading.txt.provenance.json').exists()
            observed.append(boundary)
            raise failure('The actual complete pair is already published')
        return real_sync(path)

    @contextmanager
    def staging(**kwargs):
        with real_staging(**kwargs) as folder:
            yield folder
        if boundary == 'staging-cleanup':
            assert not Path(folder).exists()
            observed.append(boundary)
            raise failure('The actual staging cleanup completed before notification failed')

    monkeypatch.setattr(os, 'link', link)
    monkeypatch.setattr(module, 'sync_directory', sync)
    monkeypatch.setattr(module, 'retained_temporary_directory', staging)
    result = service.extract(source, 'reading.txt')
    assert observed == [boundary]
    assert_complete(result, source, original, warning=True)
    assert set(path.name for path in outputs.iterdir()) == {'reading.txt', 'reading.txt.provenance.json'}


@pytest.mark.parametrize('after_marker', [False, True])
def test_cancellation_boundary_is_the_actual_complete_pair(conversion, monkeypatch, after_marker):
    service, source, outputs = conversion
    original, real_link, stopped = source.read_bytes(), os.link, [False]
    def link(incoming, destination, *args, **kwargs):
        value = real_link(incoming, destination, *args, **kwargs)
        if str(destination).endswith('.provenance.json') is after_marker:
            stopped[0] = True
        return value
    monkeypatch.setattr(os, 'link', link)
    if after_marker:
        result = service.extract(source, 'reading.txt', cancelled=lambda: stopped[0])
        assert_complete(result, source, original, warning=False)
    else:
        with pytest.raises(InterruptedError, match='provenance'):
            service.extract(source, 'reading.txt', cancelled=lambda: stopped[0])
        assert not list(outputs.iterdir())
    assert stopped[0] and source.read_bytes() == original


@pytest.mark.parametrize('foreign_name', ['reading.txt', 'reading.txt.provenance.json'])
def test_equal_byte_foreign_inode_is_not_adopted_or_deleted(conversion, monkeypatch, foreign_name):
    service, source, outputs = conversion
    original, real_link, captured = source.read_bytes(), os.link, {}
    def link(incoming, destination, *args, **kwargs):
        value = real_link(incoming, destination, *args, **kwargs)
        if str(destination).endswith('.provenance.json'):
            foreign = outputs / foreign_name
            raw, original_inode = foreign.read_bytes(), foreign.stat().st_ino
            replacement = outputs / 'foreign.tmp'
            replacement.write_bytes(raw)
            os.replace(replacement, foreign)
            assert foreign.stat().st_ino != original_inode
            captured.update(path=foreign, raw=raw, identity=(foreign.stat().st_dev, foreign.stat().st_ino))
            raise OSError('A competing equal-byte file replaced the selected output')
        return value
    monkeypatch.setattr(os, 'link', link)
    with pytest.raises(OSError, match='competing'):
        service.extract(source, 'reading.txt')
    assert captured['path'].read_bytes() == captured['raw']
    assert (captured['path'].stat().st_dev, captured['path'].stat().st_ino) == captured['identity']
    assert set(path.name for path in outputs.iterdir()) == {foreign_name}
    assert source.read_bytes() == original


def test_modified_owned_bytes_cannot_be_reported_complete(conversion, monkeypatch):
    service, source, outputs = conversion
    original, real_link = source.read_bytes(), os.link
    def link(incoming, destination, *args, **kwargs):
        value = real_link(incoming, destination, *args, **kwargs)
        if str(destination).endswith('.provenance.json'):
            target = outputs / 'reading.txt'
            raw = target.read_bytes()
            target.write_bytes(b'X' * len(raw))
        return value
    monkeypatch.setattr(os, 'link', link)
    with pytest.raises(module.ConversionError, match='saved conversion bytes changed'):
        service.extract(source, 'reading.txt')
    assert source.read_bytes() == original
    assert not list(outputs.iterdir())


@pytest.mark.parametrize('mutation', ['staged-text', 'source'])
def test_fsynced_stage_and_original_are_rechecked_before_first_link(conversion, monkeypatch, mutation):
    service, source, outputs = conversion
    original, real_fsync, changed = source.read_bytes(), os.fsync, []
    def fsync(descriptor):
        value = real_fsync(descriptor)
        info = os.fstat(descriptor)
        staged = next(outputs.glob('.pending-conversion-*/reading.txt'), None)
        if not changed and staged is not None and (info.st_dev, info.st_ino) == (staged.stat().st_dev, staged.stat().st_ino):
            selected = staged if mutation == 'staged-text' else source
            raw = selected.read_bytes()
            selected.write_bytes(b'Z' * len(raw))
            changed.append(mutation)
        return value
    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises(module.ConversionError, match='changed'):
        service.extract(source, 'reading.txt')
    assert changed == [mutation]
    assert not list(outputs.iterdir())
    if mutation == 'staged-text':
        assert source.read_bytes() == original
    else:
        assert source.read_bytes() == b'Z' * len(original)


def test_pending_native_owner_keeps_exact_staging_until_confirmed_shutdown(conversion, monkeypatch):
    service, source, outputs = conversion
    owner = CleanupOwner(lambda: True)
    pending = AudioCleanupPending('Actual cleanup owner has not confirmed shutdown', cleanup_owner=owner)
    observed = []
    def waiting_process(argv, **kwargs):
        assert kwargs['owned_tree'] is True
        stage = Path(kwargs['cwd'])
        assert (stage / 'source.txt').read_bytes() == source.read_bytes()
        observed.append(stage)
        raise pending
    monkeypatch.setattr(module, 'run_process', waiting_process)
    with pytest.raises(AudioCleanupPending) as raised:
        service.extract(source, 'reading.txt')
    assert raised.value is pending and raised.value.cleanup_owner is owner
    assert len(observed) == 1 and observed[0].is_dir()
    assert not (outputs / 'reading.txt').exists()
    assert not owner.quiescent
    assert owner.close() is True
    assert owner.quiescent and not observed[0].exists()
    assert not list(outputs.iterdir())


@pytest.mark.parametrize('after_marker', [False, True])
def test_actual_child_exit_never_overwrites_a_residual_publication(conversion, after_marker):
    service, source, outputs = conversion
    original = source.read_bytes()
    imported = Path(module.__file__).resolve()
    code = r"""
from hashlib import sha256
import os, pathlib, sys
import carr_library.conversion.service as module
assert pathlib.Path(module.__file__).resolve() == pathlib.Path(sys.argv[1]).resolve()
assert sha256(pathlib.Path(module.__file__).read_bytes()).hexdigest() == sys.argv[2]
source, output = pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4])
after_marker = sys.argv[5] == 'True'
real_link = os.link
def link(incoming, destination, *args, **kwargs):
    value = real_link(incoming, destination, *args, **kwargs)
    if str(destination).endswith('.provenance.json') is after_marker:
        os._exit(87)
    return value
os.link = link
module.ConversionService(output, input_roots=(source.parent,)).extract(source, 'crashed.txt')
raise AssertionError('The child did not exit at its actual link boundary')
"""
    import resonance_bootstrap
    product_roots = (imported.parents[2], Path(resonance_bootstrap.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(root) for root in product_roots),
               PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.run([sys.executable, '-B', '-c', code, str(imported),
        sha256(imported.read_bytes()).hexdigest(), str(source), str(outputs), str(after_marker)],
        env=env, capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert child.returncode == 87, child.stdout + child.stderr
    target, receipt = outputs / 'crashed.txt', outputs / 'crashed.txt.provenance.json'
    assert target.read_bytes() == original and receipt.exists() is after_marker
    snapshot = {str(path.relative_to(outputs)): path.read_bytes()
                for path in outputs.rglob('*') if path.is_file()}
    with pytest.raises((FileExistsError, ValueError)):
        service.extract(source, 'crashed.txt')
    assert snapshot == {str(path.relative_to(outputs)): path.read_bytes()
                        for path in outputs.rglob('*') if path.is_file()}
    if after_marker:
        saved = json.loads(receipt.read_text(encoding='utf-8'))
        assert saved['text_sha256'] == sha256(target.read_bytes()).hexdigest()
        assert saved['source_sha256'] == sha256(original).hexdigest()
    fresh = service.extract(source, 'explicit-new-name.txt')
    assert_complete(fresh, source, original, warning=False)
    assert target.read_bytes() == original


@pytest.mark.parametrize('change', ['equal-byte-inode', 'same-size-content'])
def test_final_second_digest_cannot_bless_a_changed_first_output(conversion, monkeypatch, change):
    service, source, outputs = conversion
    original, real_digest, changed = source.read_bytes(), module._digest, []
    target, receipt = outputs / 'reading.txt', outputs / 'reading.txt.provenance.json'
    def digest(path, *args, **kwargs):
        result = real_digest(path, *args, **kwargs)
        # Only the final pair verification occurs after actual staging cleanup.
        if (Path(path) == receipt and not changed
                and not list(outputs.glob('.pending-conversion-*'))):
            before, raw = target.stat(), target.read_bytes()
            if change == 'equal-byte-inode':
                replacement = outputs / 'competing.txt'
                replacement.write_bytes(raw)
                os.replace(replacement, target)
                assert target.stat().st_ino != before.st_ino
            else:
                target.write_bytes(b'Q' * len(raw))
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            changed.append(target.read_bytes())
        return result
    monkeypatch.setattr(module, '_digest', digest)
    with pytest.raises(module.ConversionError, match='changed|publication'):
        service.extract(source, 'reading.txt')
    assert changed and target.read_bytes() == changed[0]
    assert source.read_bytes() == original
    assert receipt.exists()  # no destruction of a later unverified graph
