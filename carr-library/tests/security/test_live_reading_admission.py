"""Actual signed synthetic admission for the bounded local speech owner.

PCM is synthetic. These tests do not qualify a local voice or audio device.
The shared domain fixture supplies the same actual signed entitlement store
used by the current owner-configured application boundaries.
"""
from types import SimpleNamespace

import pytest

from carr_library.reader.live_speech import (
    SpeechOperation, SpeechPositionStore, SpeechSelection, SpeechSession, SpeechSettings)
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.security.admission import operation_context
from carr_library.security.controller import TrustController
from carr_library.security.entitlements import ReviewRequired
from carr_library.ui.live_reading import LiveReadingDialog

from .test_domain_admission import authority, expire, fixture_module


@pytest.fixture
def speech(tmp_path, monkeypatch):
    engine_type = fixture_module(monkeypatch, 'reader/test_live_speech.py').PcmEngine
    root = tmp_path / 'library'
    root.mkdir()
    engine = engine_type()
    broker = ResourceBroker.for_app(root,
        limits=ResourceLimits(cpu_slots=1, memory_bytes=64 * 1024**2))
    sessions = []
    def create(controller=None, *, operation=None, text='First. Second!'):
        session = SpeechSession(root, text, SpeechSettings('fixture:voice', 'en'),
            SpeechSelection(0, len(text)), registry=engine, broker=broker,
            temporary_parent=tmp_path, trust_controller=controller, operation=operation)
        sessions.append(session)
        return session
    yield SimpleNamespace(create=create, engine=engine, broker=broker, root=root)
    for session in reversed(sessions):
        session.close()


def test_expired_fresh_speech_is_refused_before_pcm_or_saved_cursor_mutation(authority, speech):
    session = speech.create(authority.controller)
    store = SpeechPositionStore(speech.root)
    store.remember(session.text, 3, session.settings)
    before = store.path.read_bytes()
    expire(authority)
    with pytest.raises(ReviewRequired):
        session.render_next()
    assert speech.engine.calls == [] and session.current is None and session.cursor == 0
    assert not list(session.directory.iterdir()) and speech.broker.snapshot()['leases'] == []
    assert store.path.read_bytes() == before and store.load(session.text)['cursor'] == 3
    assert session.text == 'First. Second!'


def test_one_book_request_keeps_admission_across_utterances_and_pages(authority, speech, monkeypatch):
    calls, begin = [], authority.controller.begin_operation
    def observed(action):
        calls.append(action)
        return begin(action)
    monkeypatch.setattr(authority.controller, 'begin_operation', observed)
    operation = SpeechOperation(authority.controller)
    first = speech.create(authority.controller, operation=operation)
    first.render_next()
    first.release(completed=True)
    expire(authority)
    first.render_next()
    first.release(completed=True)
    assert first.render_next() is None
    first.close()
    assert not operation.closed
    following = speech.create(authority.controller, operation=operation, text='Next page.')
    following.render_next()
    following.release(completed=True)
    following.close()
    assert [call[0] for call in speech.engine.calls] == ['First.', 'Second!', 'Next page.']
    assert calls == ['audio.render'] and speech.broker.snapshot()['leases'] == []
    operation.close()
    fresh = speech.create(authority.controller)
    with pytest.raises(ReviewRequired):
        fresh.render_next()
    assert calls == ['audio.render', 'audio.render'] and len(speech.engine.calls) == 3


def test_new_explicit_request_does_not_inherit_a_finished_admission(authority, speech, monkeypatch):
    calls, begin = [], authority.controller.begin_operation
    def observed(action):
        calls.append(action)
        return begin(action)
    monkeypatch.setattr(authority.controller, 'begin_operation', observed)
    for _ in range(2):
        session = speech.create(authority.controller, text='One excerpt.')
        session.render_next()
        session.release(completed=True)
        session.close()
        assert session.operation.closed
    assert calls == ['audio.render', 'audio.render'] and len(speech.engine.calls) == 2


def test_shared_operation_cannot_replace_its_explicit_controller(authority, speech):
    operation = SpeechOperation(authority.controller)
    with pytest.raises(ValueError, match='same explicitly configured authority'):
        speech.create(TrustController(), operation=operation)
    assert speech.engine.calls == [] and speech.broker.snapshot()['leases'] == []
    operation.close()


def test_captured_context_owner_cannot_be_changed_between_utterances(authority, speech):
    operation = SpeechOperation()
    session = speech.create(operation=operation)
    with operation_context(authority.controller):
        session.render_next()
    session.release(completed=True)
    expire(authority)
    with operation_context(TrustController()):
        with pytest.raises(ValueError, match='authority'):
            session.render_next()
    assert len(speech.engine.calls) == 1 and session.cursor == 6
    with operation_context(authority.controller):
        session.render_next()
    session.release(completed=True)
    assert len(speech.engine.calls) == 2 and speech.broker.snapshot()['leases'] == []
    operation.close()


def test_closed_shared_operation_cannot_synthesize_again(authority, speech):
    operation = SpeechOperation(authority.controller)
    operation.close()
    session = speech.create(authority.controller, operation=operation)
    with pytest.raises(RuntimeError, match='already ended'):
        session.render_next()
    assert speech.engine.calls == [] and not list(session.directory.iterdir())


def test_host_authority_cannot_be_replaced_before_native_controls_are_created(authority, speech):
    parent = SimpleNamespace(trust_controller=authority.controller)
    with pytest.raises(ValueError, match='host authority'):
        LiveReadingDialog(parent, speech.root, 'Exact source.',
                          trust_controller=TrustController())


def test_owning_source_guard_runs_before_entitlement_admission(authority, speech, monkeypatch):
    session = speech.create(authority.controller)
    def revoked():
        raise PermissionError('Selected source was revoked.')
    def forbidden(*args):
        pytest.fail('A revoked source reached admission.')
    session.validate_source = revoked
    monkeypatch.setattr(authority.controller, 'begin_operation', forbidden)
    with pytest.raises(PermissionError, match='revoked'):
        session.render_next()
    assert speech.engine.calls == [] and speech.broker.snapshot()['leases'] == []
