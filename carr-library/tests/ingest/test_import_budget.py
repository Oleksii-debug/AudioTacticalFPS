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
    assert source_limit('application/pdf', value) == 64 * 1024**2
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


@pytest.mark.parametrize('kind', ['legacy', 'profile', 'configuration'])
def test_profile_codec_retains_legacy_bytes_and_binds_only_explicit_resource_policy(kind):
    from carr_library.ingest.streamed_text import TextProfile, profile_from_value, profile_from_config
    old = TextProfile()
    assert old.to_dict() == {'schema': 'carr-streamed-text-profile/1', 'maximum_bytes': 128 * 1024**2}
    assert 'import_budget' not in old.configuration()
    assert old.maximum_fragments == 8192 and old.maximum_steps == 8193 and old.maximum_timeout == 600
    selected = replace(ImportBudget(), source_bytes=256 * 1024**2, library_bytes=512 * 1024**2,
                       temporary_bytes=512 * 1024**2, segments=10000, steps=5, seconds=1800)
    profile = TextProfile(selected.source_bytes, selected)
    assert profile.maximum_fragments == profile.maximum_records == 10000
    assert profile.maximum_steps == 5 and profile.maximum_timeout == 1800
    assert profile.to_dict()['schema'] == 'carr-streamed-text-profile/2'
    assert profile.to_dict()['import_budget'] == selected.to_dict()
    decoded = (profile_from_value(old.to_dict()) if kind == 'legacy' else
               profile_from_value(profile.to_dict()) if kind == 'profile' else
               profile_from_config(profile.configuration()))
    assert decoded == (old if kind == 'legacy' else profile)


@pytest.mark.parametrize('field,value', [
    ('maximum_bytes', 65 * 1024**2), ('maximum_fragments', 1001),
    ('fragment_chars', 32), ('kind', 'physical_page'), ('unexpected', True),
])
def test_sealed_streamed_configuration_cannot_borrow_a_different_capacity(field, value):
    from carr_library.ingest.streamed_text import TextProfile, profile_from_config
    profile = TextProfile(64 * 1024**2, ImportBudget())
    saved = profile.configuration()
    saved[field] = value
    with pytest.raises((TypeError, ValueError)):
        profile_from_config(saved)


def test_actual_more_than_legacy_fragment_count_requires_exact_selected_policy(tmp_path):
    from hashlib import sha256
    from carr_library.ingest.streamed_text import TextProfile, inspect_text
    source = tmp_path / 'fragmented.txt'
    raw = b'original' + (b'\fexact' * 8192)
    source.write_bytes(raw)
    with pytest.raises(ValueError, match='fragment limit'):
        inspect_text(source, TextProfile())
    selected = replace(ImportBudget(), segments=8193, steps=100, memory_bytes=512 * 1024**2)
    profile = TextProfile(selected.source_bytes, selected)
    result = inspect_text(source, profile)
    assert len(result.fragments) == 8193
    assert result.digest == sha256(raw).hexdigest()
    assert source.read_bytes() == raw


@pytest.mark.parametrize('mime', ['application/pdf', 'text/html', 'application/rtf'])
def test_large_policy_does_not_expand_other_whole_buffer_adapters(mime):
    chosen = replace(ImportBudget(), source_bytes=1024**3, library_bytes=2*1024**3,
                     temporary_bytes=2*1024**3)
    assert source_limit(mime, chosen) == 64 * 1024**2


def test_explicit_segment_allowance_is_checked_on_actual_utf8_bytes(tmp_path):
    from carr_library.ingest.streamed_text import TextProfile, inspect_text
    source = tmp_path / 'multibyte.txt'
    source.write_text('Ї' * 4, encoding='utf-8')
    chosen = replace(ImportBudget(), segment_bytes=7)
    with pytest.raises(ValueError, match='segment-byte allowance'):
        inspect_text(source, TextProfile(chosen.source_bytes, chosen))
    assert source.read_bytes() == ('Ї' * 4).encode('utf-8')
