"""Owned inventory discovery releases providers without enabling a voice."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.runtime import VoiceRegistry
from carr_library.ui.language_matrix import collect_installed_voices


@pytest.mark.parametrize('outcome', ['success', 'cancelled_after_discovery', 'enumeration_error'])
def test_owned_inventory_registry_closes_on_every_exit(tmp_path, monkeypatch, outcome):
    registry = VoiceRegistry.__new__(VoiceRegistry)
    # The real close contract was independently exercised by the speech suite;
    # this test binds the inventory caller's ownership to that one registry.
    registry.voices = Mock(return_value=())
    registry.notices = []
    registry.close = Mock()
    if outcome == 'enumeration_error':
        registry.voices.side_effect = ValueError('Probe output could not be enumerated')
    factory = Mock(return_value=registry)
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry', factory)
    checks = iter([False, outcome == 'cancelled_after_discovery'])
    work = lambda: collect_installed_voices(
        SimpleNamespace(root=tmp_path), cancelled=lambda: next(checks))
    if outcome == 'success':
        assert work().voices == ()
    elif outcome == 'cancelled_after_discovery':
        with pytest.raises(InterruptedError, match='cancelled'):
            work()
        registry.voices.assert_not_called()
    else:
        with pytest.raises(ValueError, match='enumerated'):
            work()
    factory.assert_called_once_with(tmp_path / 'state')
    registry.close.assert_called_once_with()


def test_cancelled_inventory_does_not_create_a_registry(tmp_path, monkeypatch):
    factory = Mock(side_effect=AssertionError('No discovery is authorized after cancellation'))
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry', factory)
    with pytest.raises(InterruptedError):
        collect_installed_voices(SimpleNamespace(root=tmp_path), cancelled=lambda: True)
    factory.assert_not_called()
