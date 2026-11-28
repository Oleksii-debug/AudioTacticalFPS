"""Typed resource review; choosing fields creates no job or new authority."""
from dataclasses import replace
import pytest

from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.ui.import_resources import profile_values, profile_from_values, profile_summary


def test_explicit_ui_values_reproduce_frozen_budget_and_legacy_defaults():
    original = ImportBudget()
    assert profile_from_values(profile_values()) == original
    assert '64 MiB' in profile_summary() and '120-second' in profile_summary()
    selected = replace(original, source_bytes=129*MIB, segment_bytes=4*MIB,
                       segments=9000, steps=2, seconds=600)
    assert profile_from_values(profile_values(selected)) == selected
    text = profile_summary(selected)
    assert '129 MiB' in text and '9000' in text and '2 steps' in text and '600 seconds' in text
    assert 'memory' in text and 'library growth' in text and 'temporary' in text


@pytest.mark.parametrize('field,value', [
    ('source_mib', '0'), ('source_mib', '-1'), ('segment_mib', '65'),
    ('segments', '2147483648'), ('steps', '1002'), ('seconds', 'nan'),
    ('seconds', 'inf'), ('seconds', '0.01'), ('seconds', '86401'),
    ('memory_mib', '1'), ('library_mib', '1'), ('temporary_mib', '1'),
    ('source_mib', '2.5'), ('seconds', ''), ('segments', '9'*33),
])
def test_invalid_ui_budget_is_rejected_before_any_plan(field, value):
    values = profile_values()
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        profile_from_values(values)


@pytest.mark.parametrize('change', ['missing', 'unknown', 'not-dict', 'not-text'])
def test_resource_dialog_input_requires_exact_known_fields(change):
    values = profile_values()
    if change == 'missing':
        values.pop('seconds')
    elif change == 'unknown':
        values['authority'] = 'old approval'
    elif change == 'not-dict':
        values = tuple(values.items())
    else:
        values['seconds'] = 120
    with pytest.raises((TypeError, ValueError)):
        profile_from_values(values)
