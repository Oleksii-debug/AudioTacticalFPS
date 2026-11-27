"""Cancellation reaches owned voice workers and cannot become a silent notice."""
from threading import Event

import pytest

from carr_library.speech import Voice, VoiceRegistry
from carr_library.speech.runtime import NativeSapiProvider, SapiProvider


def test_discovery_cancellation_closes_every_owned_provider():
    cancel = Event()
    closed = []

    class Provider:
        name = 'Synthetic cancellable discovery'
        cancellable_discovery = True

        def voices(self, *, cancelled):
            assert not cancelled()
            cancel.set()
            raise RuntimeError('Synthetic provider cancellation')

        def close(self):
            closed.append(self)

    first, unstarted = Provider(), Provider()
    with pytest.raises(InterruptedError):
        VoiceRegistry((first, unstarted), cancelled=cancel.is_set)
    assert closed == [first, unstarted]


def test_legacy_discovery_does_not_receive_new_keyword():
    class Provider:
        name = 'Existing provider'

        def voices(self):
            return (Voice('legacy', 'Existing voice', 'existing', 'en'),)

    registry = VoiceRegistry((Provider(),), cancelled=lambda: False)
    assert [voice.id for voice in registry.voices()] == ['legacy']


def test_cancelled_registration_keeps_previous_inventory_and_closes_new_owner():
    cancel = Event()

    class Existing:
        name = 'Existing provider'

        def voices(self):
            return (Voice('existing', 'Existing', 'fixture', 'en'),)

    class Incoming:
        name = 'Incoming provider'
        cancellable_discovery = True
        closed = False

        def voices(self, *, cancelled):
            cancel.set()
            return ()

        def close(self):
            self.closed = True

    registry = VoiceRegistry((Existing(),))
    incoming = Incoming()
    with pytest.raises(InterruptedError):
        registry.add_provider(incoming, cancelled=cancel.is_set)
    assert incoming.closed
    assert incoming not in registry._providers
    assert [voice.id for voice in registry.voices()] == ['existing']
    cancel.clear()
    assert registry.reload()[0].id == 'existing'


@pytest.mark.parametrize('provider_class', [SapiProvider, NativeSapiProvider])
def test_sapi_discovery_forwards_cancellation_to_existing_bounded_worker(provider_class):
    provider = object.__new__(provider_class)
    provider.architecture = '64'
    provider._tokens = {}
    cancel = Event()
    cancel.set()
    calls = []

    def request(data, *, cancelled):
        calls.append(data)
        assert cancelled()
        raise InterruptedError('Synthetic cancelled native request')

    provider._request = request
    with pytest.raises(InterruptedError):
        provider.voices(cancelled=cancel.is_set)
    assert calls == [{'operation': 'voices'}]
