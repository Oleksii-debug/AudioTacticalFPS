"""Real remuxed files retain their truthful result across the commit boundary."""
from hashlib import sha256
from pathlib import Path
import os
import runpy
import tempfile

import pytest

from carr_library.audio import metadata
from carr_library.audio.metadata import MetadataChanges, publish_metadata, review_metadata
from carr_library.audio.models import AudioError


_helpers = runpy.run_path(str(Path(__file__).with_name('test_metadata.py')))
actual = _helpers['actual']
encoded = _helpers['encoded']


def _plan(actual):
    runtime, _, root, _ = actual
    source = encoded(actual)
    target = root / 'committed-boundary.flac'
    return runtime, source, target, review_metadata(
        runtime, source, target, MetadataChanges({'title': 'Exact committed metadata'}))


def _completed(runtime, plan):
    try:
        return publish_metadata(runtime, plan)
    except KeyboardInterrupt:
        pytest.fail('A late interruption replaced an already committed metadata result.')


@pytest.mark.parametrize('exception', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('boundary', ['audio', 'receipt'])
def test_actual_link_survives_late_writer_interruption(actual, monkeypatch, exception, boundary):
    runtime, source, target, plan = _plan(actual)
    before = source.read_bytes()
    original = metadata.publish_file
    receipt_path = target.with_name(target.name + '.metadata.json')
    def publish(staged, destination, **kwargs):
        original(staged, destination, **kwargs)
        selected = target if boundary == 'audio' else receipt_path
        if destination == selected:
            raise exception('injected interruption after the actual no-clobber link')
    monkeypatch.setattr(metadata, 'publish_file', publish)
    result = _completed(runtime, plan)
    assert result.audio.path == target
    assert result.audio.sha256 == sha256(target.read_bytes()).hexdigest()
    assert result.receipt['output']['sha256'] == result.audio.sha256
    assert result.receipt_path == receipt_path and receipt_path.is_file()
    assert result.warning and source.read_bytes() == before
    assert not list(target.parent.glob('.audio-metadata-*'))


@pytest.mark.parametrize('exception', [OSError, KeyboardInterrupt])
def test_completed_result_survives_actual_staging_cleanup_interruption(actual, monkeypatch, exception):
    runtime, source, target, plan = _plan(actual)
    before = source.read_bytes()
    cleanup = tempfile.TemporaryDirectory.cleanup
    observed = []
    def interrupted(self):
        selected = Path(self.name).name.startswith('.audio-metadata-')
        cleanup(self)
        if selected and target.exists():
            observed.append(self.name)
            raise exception('injected failure after actual staging removal')
    monkeypatch.setattr(tempfile.TemporaryDirectory, 'cleanup', interrupted)
    result = _completed(runtime, plan)
    assert len(observed) == 1 and result.warning
    assert result.audio.sha256 == sha256(target.read_bytes()).hexdigest()
    assert result.receipt_path.is_file() and source.read_bytes() == before
    assert target.stat().st_nlink == 1


def test_equal_bytes_from_another_inode_do_not_authenticate_a_failed_writer(actual, monkeypatch):
    runtime, source, target, plan = _plan(actual)
    original = metadata.publish_file
    foreign = []
    def publish(staged, destination, **kwargs):
        if destination == target:
            foreign.append(staged.read_bytes())
            destination.write_bytes(foreign[-1])
            assert not destination.samefile(staged)
            raise OSError('another producer won the selected destination')
        return original(staged, destination, **kwargs)
    monkeypatch.setattr(metadata, 'publish_file', publish)
    with pytest.raises((OSError, AudioError)):
        publish_metadata(runtime, plan)
    assert target.read_bytes() == foreign[0]
    assert not target.with_name(target.name + '.metadata.json').exists()


def test_changed_linked_bytes_are_not_reported_as_verified_completion(actual, monkeypatch):
    runtime, source, target, plan = _plan(actual)
    original = metadata.publish_file
    def publish(staged, destination, **kwargs):
        original(staged, destination, **kwargs)
        if destination == target:
            with destination.open('ab') as stream:
                stream.write(b'foreign mutation after linking')
            raise OSError('writer returned after output mutation')
    monkeypatch.setattr(metadata, 'publish_file', publish)
    with pytest.raises((OSError, AudioError)):
        publish_metadata(runtime, plan)
    assert target.read_bytes().endswith(b'foreign mutation after linking')
    assert not target.with_name(target.name + '.metadata.json').exists()


@pytest.mark.parametrize('which', ['audio', 'receipt'])
def test_cleanup_cannot_grant_a_foreign_equal_byte_inode_completion(actual, monkeypatch, which):
    runtime, source, target, plan = _plan(actual)
    receipt_path = target.with_name(target.name + '.metadata.json')
    cleanup = tempfile.TemporaryDirectory.cleanup
    replaced = []
    def substitute(self):
        selected = Path(self.name).name.startswith('.audio-metadata-')
        cleanup(self)
        if selected:
            destination = target if which == 'audio' else receipt_path
            raw = destination.read_bytes()
            foreign = destination.with_name('foreign-inode-' + destination.name)
            foreign.write_bytes(raw)
            assert not foreign.samefile(destination)
            os.replace(foreign, destination)
            replaced.append(raw)
    monkeypatch.setattr(tempfile.TemporaryDirectory, 'cleanup', substitute)
    if which == 'audio':
        with pytest.raises(AudioError, match='replaced'):
            publish_metadata(runtime, plan)
        assert target.read_bytes() == replaced[0]
    else:
        result = _completed(runtime, plan)
        assert result.audio.sha256 == sha256(target.read_bytes()).hexdigest()
        assert result.receipt_path is None and 'receipt' in result.warning
        assert receipt_path.read_bytes() == replaced[0]
