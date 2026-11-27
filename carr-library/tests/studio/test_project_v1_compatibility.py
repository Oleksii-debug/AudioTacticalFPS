"""Literal v1 wire acceptance and unchanged completed-source receipts.

Synthetic voices/real FFmpeg prove file/state compatibility, not voice quality.
"""
from dataclasses import replace
from hashlib import sha256
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio import AudioRuntime
from carr_library.durable_state import encode
from carr_library.studio.project import Project, parse_script
from carr_library.studio.render import ProductionService
from carr_library.studio.publishing import PublicationService, inspect_epub

_spec = importlib.util.spec_from_file_location('clip_v1_publication_fixtures',
    Path(__file__).with_name('test_publishing.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
actual_job, source_job = _fixture.actual_job, _fixture.source_job


def test_existing_completed_v1_job_project_and_epub_remain_exact_on_read(source_job, tmp_path):
    service, state = source_job
    assert state['project']['schema'] == 1
    literal_fingerprint = sha256(encode(state['project'])).hexdigest()
    assert state['fingerprint'] == literal_fingerprint
    expected = {Path(row['path']): Path(row['path']).read_bytes() for row in state['artifacts']}
    job = service.jobs_root / state['id'] / 'job.json'
    expected[job] = job.read_bytes()
    project_path = service.projects.directory(state['project']['id']) / 'project.json'
    expected[project_path] = project_path.read_bytes()
    output = tmp_path / 'v1-reading-copy.epub'
    receipt = PublicationService(service).export_epub(state['id'], output)
    epub_bytes = output.read_bytes()
    saved_receipt = service.root / 'publications' / (receipt['id'] + '.json')
    expected[saved_receipt] = saved_receipt.read_bytes()
    reopened = ProductionService(service.root)
    loaded = reopened.projects.load(state['project']['id'])
    assert loaded.schema == 1 and loaded.fingerprint == literal_fingerprint
    assert Project.from_dict(state['project']).to_dict() == loaded.to_dict()
    assert reopened.get(state['id']) == state
    assert inspect_epub(output)['source']['project_sha256'] == literal_fingerprint
    assert PublicationService(reopened).verify(receipt['id']) == receipt
    assert output.read_bytes() == epub_bytes
    assert {path: path.read_bytes() for path in expected} == expected


def test_owned_voice_discovery_and_close_are_explicit_and_borrowed_registry_survives(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    owned = SimpleNamespace(close=Mock(return_value=False))
    calls = []
    def factory(state_root, *, cancelled):
        assert not cancelled()
        calls.append(Path(state_root))
        return owned
    monkeypatch.setattr(runtime_settings, 'voice_registry', factory)
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio')
    assert not calls
    assert service.discover_voices() is owned and service.discover_voices() is owned
    assert len(calls) == 1
    assert service.close() is False
    assert service._voices is owned
    owned.close.return_value = True
    assert service.close() is True and service._voices is None
    with pytest.raises(InterruptedError, match='closed'):
        service.discover_voices()
    borrowed = SimpleNamespace(close=Mock())
    other = ProductionService(tmp_path / 'other' / 'state' / 'studio', voices=borrowed)
    assert other.close() is True
    borrowed.close.assert_not_called()


def test_cancelled_discovery_does_not_create_an_owned_registry(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    factory = Mock()
    monkeypatch.setattr(runtime_settings, 'voice_registry', factory)
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio')
    with pytest.raises(InterruptedError, match='cancelled'):
        service.discover_voices(cancelled=lambda: True)
    factory.assert_not_called()
    assert service.close() is True


def test_model_fingerprint_receives_live_cancellation_before_synthesis(tmp_path):
    class CancelHash(_fixture.ToneVoices):
        cancellable_fingerprint = True
        def __init__(self):
            self.callbacks, self.synthesis = [], []
        def fingerprint(self, voice_id, *, cancelled):
            self.callbacks.append(cancelled)
            assert not cancelled()
            stopped.set()
            if cancelled():
                raise InterruptedError('Fingerprint cancelled during exact model hashing')
        def synthesize(self, *args, **kwargs):
            self.synthesis.append(args)
            raise AssertionError('A cancelled fingerprint must not start synthesis')
    stopped = threading.Event()
    voices = CancelHash()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio', voices=voices)
    project = service.create('Exact source.', voice_ids=('synthetic-tone',))
    output = tmp_path / 'outputs'; output.mkdir()
    identity = service.submit(project, output)
    with pytest.raises(InterruptedError, match='Fingerprint cancelled'):
        service.run(identity, cancelled=stopped.is_set)
    assert voices.callbacks and not voices.synthesis
    assert service.get(identity)['status'] == 'paused'
    assert service.broker.snapshot()['leases'] == []


def test_legacy_injected_fingerprint_one_argument_api_is_preserved(tmp_path):
    class LegacyHash(_fixture.ToneVoices):
        def __init__(self):
            self.identities = []
        def fingerprint(self, voice_id):
            self.identities.append(voice_id)
            return {'synthetic_runtime': 'legacy-one-argument'}
    voices = LegacyHash()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio', voices=voices)
    project = service.create('Exact source.', voice_ids=('synthetic-tone',))
    output = tmp_path / 'outputs'; output.mkdir()
    result = service.run(service.submit(project, output))
    assert result['status'] == 'complete'
    # Retain the legacy callable signature through pre/post synthesis checks.
    assert voices.identities and set(voices.identities) == {'synthetic-tone'}
    assert project.schema == 1 and service.projects.load(project.id) == project
