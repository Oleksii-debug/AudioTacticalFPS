from dataclasses import FrozenInstanceError
from hashlib import sha1, sha256
import json

import pytest

from carr_library.language_qualification import (
    CatalogError, DeclaredLanguageBinding, build_capability_matrix,
    canonical_language_tag, load_catalog,
)
from carr_library.language_qualification import catalog as module
from carr_library.speech import Voice


def _entry(catalog, label):
    return next(row for row in catalog.entries if row.label_uk == label)


def test_exact_pinned_source_bytes_and_135_stable_unique_entries():
    catalog = load_catalog()
    assert len(catalog.entries) == len({row.id for row in catalog.entries}) == 135
    assert [x.order for x in catalog.entries] == list(range(1, 136))
    assert catalog.entries[0].label_uk == 'Англійська'
    assert catalog.entries[-1].label_uk == 'Хірі-моту'
    assert catalog.entries[0].id == 'selected-9b1dff4c1fb86c14804bb7e6'
    assert catalog.entries[-1].id == 'selected-593f6f77442c6b06687190cd'
    assert [x.id for x in load_catalog().entries] == [x.id for x in catalog.entries]
    for source in (catalog.source, catalog.audit.source):
        raw = (module._DATA / source.path.split('/')[-1]).read_bytes()
        assert sha256(raw).hexdigest() == source.sha256
        assert sha1(b'blob ' + str(len(raw)).encode() + b'\x00' + raw).hexdigest() == source.git_blob


def test_geographic_order_and_overlaps_are_preserved_without_double_counting():
    catalog = load_catalog()
    assert [r.continent for r in catalog.regions] == [
        'europe', 'america', 'america', 'asia', 'africa', 'australia-oceania']
    counts = [len(catalog.search(region=r.id)) for r in catalog.regions]
    assert counts == [37, 4, 8, 54, 39, 9]
    assert sum(counts) == 151  # Regional memberships overlap; not 151 languages.
    english = _entry(catalog, 'Англійська')
    assert len(english.associations) == 6
    assert 'Велика Британія' in english.countries and 'Гаяна' in english.countries
    assert 'Нова Зеландія' in english.countries
    assert len([x for x in catalog.entries if x.label_uk == 'Англійська']) == 1
    assert [x.region_id for x in english.associations] == [r.id for r in catalog.regions]


def test_source_lines_reconstruct_every_selected_geographic_row():
    catalog = load_catalog()
    source = (module._DATA / 'SELECTED_LANGUAGES_UK.md').read_text().splitlines()
    rows = [association for entry in catalog.entries for association in entry.associations]
    assert {x.source_line for x in rows} == {index for index, line in enumerate(source, 1) if line.startswith('- ')}
    for row in rows:
        assert source[row.source_line - 1] == row.source_text


def test_ambiguous_groups_named_standards_and_ndebele_remain_distinct():
    catalog = load_catalog()
    groups = {row.label_uk for row in catalog.entries if row.scope == 'group-standard-unresolved'}
    assert groups == {'Тамазигхт', 'Коморська', 'Кечуа', 'Фула, або фульфульде', 'Курдська'}
    assert _entry(catalog, 'Китайська — стандартна мандаринська').scope == 'named-standard'
    assert _entry(catalog, 'Філіппінська — стандарт на тагальській основі').scope == 'named-standard'
    assert _entry(catalog, 'Північна ндебеле').id != _entry(catalog, 'Південна ндебеле').id
    assert all(x.canonical_locale is None and x.canonical_voice is None for x in catalog.entries)


def test_upstream_inventory_is_never_claimed_as_installed_or_acoustically_verified():
    audit = load_catalog().audit
    assert (audit.selected, audit.resources_reported, audit.missing_reported) == (135, 125, 10)
    assert (audit.full_entry_resource_mappings_reported, audit.partial_group_resource_mappings_reported) == (124, 1)
    assert audit.windows_nvda_acoustic_tests_reported == 0
    assert audit.inventory_only and not audit.local_installation_verified
    matrix = build_capability_matrix()
    assert matrix.summary()['entries_with_declared_voice_matches'] == 0
    assert matrix.summary()['qualified_entries'] == 0
    assert len(matrix.search(capability='tts', status='unsupported')) == 135
    assert len(matrix.search(status='verified')) == 0
    assert all(x.capability('accessible_reading').status == 'untested' for x in matrix.entries)


def test_canonical_catalog_is_immutable_and_generated_snapshots_are_independent():
    catalog = load_catalog()
    with pytest.raises(FrozenInstanceError):
        catalog.entries[0].label_uk = 'Replacement'
    snapshot = catalog.to_dict()
    snapshot['entries'][0]['label_uk'] = 'Changed exported copy'
    assert catalog.entries[0].label_uk == 'Англійська'
    assert json.loads(json.dumps(catalog.to_dict(), ensure_ascii=False))['schema'] == 'carr.selected-languages/1'


def test_search_uses_unicode_names_countries_and_fixed_region():
    catalog = load_catalog()
    assert [x.label_uk for x in catalog.search('УКРАЇН')] == ['Українська']
    assert [x.label_uk for x in catalog.search('Гаїті', region='america-north-central-caribbean')] == ['Французька', 'Гаїтянська креольська']
    assert catalog.search('Кечуа', region='europe') == ()
    with pytest.raises(CatalogError):
        catalog.search('', region='invented-region')


def test_tampered_selection_or_audit_is_rejected(tmp_path, monkeypatch):
    for source in ('SELECTED_LANGUAGES_UK.md', 'LANGUAGE_COVERAGE_DEV2_UK.md'):
        (tmp_path / source).write_bytes((module._DATA / source).read_bytes())
    monkeypatch.setattr(module, '_DATA', tmp_path)
    (tmp_path / 'SELECTED_LANGUAGES_UK.md').write_text('135 invented names')
    with pytest.raises(CatalogError, match='integrity'):
        load_catalog()


@pytest.mark.parametrize('raw,expected', [
    ('EN-us', 'en-US'), ('zh-hans-cn', 'zh-Hans-CN'), ('cmn-Hant-TW', 'cmn-Hant-TW'),
    ('sr-Latn', 'sr-Latn'), ('fil-PH', 'fil-PH'), ('nb', 'nb'),
    ('sl-rozaj-biske', 'sl-rozaj-biske'), ('de-u-co-phonebk', 'de-u-co-phonebk'),
    ('en-US-x-local', 'en-US-x-local'),
])
def test_structural_tags_preserve_script_region_variants_and_extensions(raw, expected):
    assert canonical_language_tag(raw) == expected


@pytest.mark.parametrize('invalid', ['', 'e', 'en_US', 'en--US', 'en-../../file', 'en-a',
    'en-a-abc-a-def', 'sl-rozaj-rozaj', 'en-🇺🇸', 'en-US\n'])
def test_unsafe_or_malformed_tags_are_rejected(invalid):
    with pytest.raises(CatalogError):
        canonical_language_tag(invalid)


def test_explicit_exact_tag_match_never_falls_back_to_parent_locale_or_voice_name():
    catalog = load_catalog()
    english = _entry(catalog, 'Англійська')
    # Metadata fixtures exercise matching only; they are not acoustic evidence.
    voices = [Voice('fixture-en', 'English fixture', 'Metadata fixture', 'en'),
              Voice('fixture-us', 'English fixture US', 'Metadata fixture', 'en-US'),
              Voice('fixture-name', 'Українська', 'Metadata fixture', 'unknown')]
    matrix = build_capability_matrix(catalog=catalog, voices=voices, bindings=[
        DeclaredLanguageBinding(english.id, 'en', 'Test-only exact metadata association')])
    entry = next(x for x in matrix.entries if x.language.id == english.id)
    assert [x.voice_id for x in entry.voices] == ['fixture-en']
    assert entry.capability('tts').status == 'candidate'
    assert entry.capability('accessible_reading').status == 'untested'
    assert set(matrix.unmatched_voice_ids) == {'fixture-us', 'fixture-name'}
    assert matrix.summary()['selected_entries'] == 135
    assert matrix.summary()['qualified_entries'] == 0


def test_partial_group_resource_does_not_qualify_every_variety():
    catalog = load_catalog()
    selected = _entry(catalog, 'Тамазигхт')
    voice = Voice('fixture-kab', 'Kabyle metadata fixture', 'Metadata fixture', 'kab', ('phonemes',))
    matrix = build_capability_matrix(catalog=catalog, voices=[voice], bindings=[
        DeclaredLanguageBinding(selected.id, 'kab', 'Pinned audit describes Kabyle as one partial group resource')])
    row = matrix.search('Тамазигхт')[0]
    assert row.capability('tts').status == 'partial'
    assert row.capability('voice_availability').status == 'partial'
    assert row.capability('phonemization').status == 'candidate'
    assert row.capability('stress').status == 'untested'
    assert not matrix.search(capability='tts', status='verified')


def test_binding_is_explicit_and_cannot_be_a_verification_certificate():
    catalog = load_catalog()
    with pytest.raises(CatalogError):
        DeclaredLanguageBinding(catalog.entries[0].id, 'en', '')
    with pytest.raises(CatalogError, match='never verified'):
        DeclaredLanguageBinding(catalog.entries[0].id, 'en', 'Evidence text', coverage='verified')
    with pytest.raises(CatalogError, match='different'):
        build_capability_matrix(catalog=catalog, bindings=[
            DeclaredLanguageBinding(catalog.entries[0].id, 'en', 'Review'),
            DeclaredLanguageBinding(catalog.entries[1].id, 'en', 'Wrong second mapping')])


def test_catalog_and_matrix_do_not_launch_processes_or_discover_resources(monkeypatch):
    import subprocess
    from carr_library.speech import VoiceRegistry
    def forbidden(*args, **kwargs):
        pytest.fail('Read-only canonical catalog performed runtime discovery or execution.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(VoiceRegistry, 'discover', forbidden)
    assert len(load_catalog().entries) == 135
    assert build_capability_matrix().summary()['qualified_entries'] == 0
