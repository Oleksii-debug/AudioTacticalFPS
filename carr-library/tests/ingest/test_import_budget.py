"""Typed review budgets never silently change legacy source/launch defaults."""
from dataclasses import FrozenInstanceError, replace
import pytest

from carr_library.ingest.resource_budget import ImportBudget, checked_budget, effective_budget, source_limit


def test_default_profile_and_omitted_budget_are_explicit():
    value = effective_budget()
    assert value.source_bytes == 64 * 1024**2
    assert (value.segments, value.steps, value.seconds) == (1000, 1001, 120.0)
    assert checked_budget(None) is None
    assert ImportBudget.from_dict(value.to_dict()) == value
    with pytest.raises(FrozenInstanceError):
        value.source_bytes = 1


def test_large_source_policy_is_typed_and_keeps_nonstreaming_decoder_limit():
    value = ImportBudget(source_bytes=2 * 1024**3, segment_bytes=4 * 1024**2,
                         segments=100000, steps=10001, seconds=1800,
                         memory_bytes=512 * 1024**2, library_bytes=16 * 1024**3,
                         temporary_bytes=4 * 1024**3)
    assert ImportBudget.from_dict(value.to_dict()) == value
    assert source_limit('text/plain', value) == 2 * 1024**3
    assert source_limit('application/pdf', value) == 2 * 1024**3
    assert source_limit('application/rtf', value) == 64 * 1024**2


@pytest.mark.parametrize('changes', [
    {'source_bytes': True}, {'source_bytes': 0}, {'source_bytes': 2**63},
    {'segment_bytes': 64 * 1024**2 + 1}, {'segments': True}, {'segments': 2**31},
    {'steps': 1002}, {'seconds': True}, {'seconds': float('inf')},
    {'seconds': float('nan')}, {'seconds': 0}, {'seconds': 86401},
    {'memory_bytes': 64 * 1024**2}, {'library_bytes': 1024}, {'temporary_bytes': 1024},
])
def test_invalid_budget_never_becomes_a_profile(changes):
    with pytest.raises(ValueError):
        replace(ImportBudget(), **changes)


@pytest.mark.parametrize('change', ['extra', 'missing', 'schema', 'object'])
def test_saved_budget_shape_is_exact(change):
    value = ImportBudget().to_dict()
    if change == 'extra':
        value['current_source'] = '/not-authority'
    elif change == 'missing':
        del value['segments']
    elif change == 'schema':
        value['schema'] = 'future'
    else:
        value = ImportBudget()
    with pytest.raises(ValueError):
        ImportBudget.from_dict(value)


def test_saved_dictionary_is_not_implicitly_accepted_as_active_policy():
    with pytest.raises(TypeError):
        checked_budget(ImportBudget().to_dict())
