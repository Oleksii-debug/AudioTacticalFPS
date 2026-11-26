"""Exact upstream selection and resource evidence never become voice claims."""
from dataclasses import FrozenInstanceError
from importlib.resources import files
from hashlib import sha256
import json

import pytest

from carr_library.language_catalog import (CAPABILITIES, CATALOG_SHA256, CatalogError,
    REGIONS, REVISION, SOURCES, _canonical, _markdown_regions, _verified, derive_catalog,
    load_catalog, validate_catalog_bytes)
from carr_library.language_catalog.__main__ import main


def payload():
    return files('carr_library.language_catalog').joinpath('UPSTREAM.json').read_bytes()


def test_canonical135_retains_upstream_ids_first_occurrence_order_and_all_geography():
    catalog = load_catalog()
    assert len(catalog.entries) == len({entry.id for entry in catalog.entries}) == 135
    assert [entry.id for entry in catalog.entries[:8]] == ['en', 'ru', 'de', 'fr', 'it', 'es', 'pl', 'uk']
    assert catalog.entries[-1].id == 'ho'
    assert tuple(len(catalog.matching(region=region)) for region in REGIONS) == (37, 4, 8, 54, 39, 9)
    assert sum(len(catalog.matching(region=region)) for region in REGIONS) == 151
    english = next(entry for entry in catalog.entries if entry.id == 'en')
    assert tuple(item.region for item in english.geography) == REGIONS
    assert english.geography[0].countries == ('Велика Британія', 'Ірландія', 'Мальта')
    assert 'Сінгапур' in english.geography[3].countries
    assert 'Нова Зеландія' in english.geography[-1].countries
    with pytest.raises(FrozenInstanceError):
        english.id = 'another-language'


def test_exact_pinned_sources_and_resource_counts_remain_distinct_from_acceptance():
    value = validate_catalog_bytes(payload())
    assert value['source_commit'] == REVISION
    assert value['sources'] == {name: source.to_dict() for name, source in SOURCES.items()}
    assert sha256(_canonical(value)).hexdigest() == CATALOG_SHA256
    summary = value['summary']
    assert summary['registered_with_resources'] == 125
    assert summary['full_entry_resource_mappings'] == 124
    assert summary['partial_group_resource_mappings'] == 1
    assert summary['not_registered'] == 10 and summary['registered_missing_resources'] == 0
    assert summary['windows_audio_verified_by_this_audit'] == 0
    assert sum(summary['backend_resource_counts'].values()) == 125
    assert value['unselected_resource_entries_not_counted'] == 38
    assert set(value['capability_defaults']) == set(CAPABILITIES)
    assert set(value['capability_defaults'].values()) == {'not_assessed'}
    assert 'asr' not in value['capability_defaults']  # Explicitly deferred scope.


def test_group_ambiguity_and_runtime_mapping_are_not_silently_resolved():
    rows = {entry.id: entry for entry in load_catalog().entries}
    tamazight = rows['tamazight']
    assert tamazight.identity_review == 'unresolved_group_standard'
    assert tamazight.resource_inventory.partial_variant
    assert tamazight.resource_inventory.runtime_code == 'kab'
    assert 'not Standard Moroccan Tamazight' in tamazight.resource_inventory.standard_note
    assert rows['comorian'].identity_review == 'unresolved_group_standard'
    assert not rows['comorian'].resource_inventory.registered
    assert all(rows[key].identity_review == 'group_variant_requires_review' for key in ('qu', 'ff', 'ku'))
    assert rows['no'].resource_inventory.runtime_code == 'nb'
    assert 'nb' not in rows and 'nn' not in rows and 'kab' not in rows
    assert rows['nd'] != rows['nr']
    assert rows['cmn'].name_uk == 'Китайська — стандартна мандаринська'
    assert rows['fil'].name_uk == 'Філіппінська — стандарт на тагальській основі'
    assert 'дарі без окремого підрахунку' in rows['fa'].name_uk


def test_search_uses_evidenced_names_countries_and_runtime_identifiers_only():
    catalog = load_catalog()
    assert [row.id for row in catalog.matching('НОВА ЗЕЛАНДІЯ')] == ['en', 'mi']
    assert [row.id for row in catalog.matching('kab')] == ['tamazight']
    assert [row.id for row in catalog.matching(resource_status='partial-variant-resources-present-unverified')] == ['tamazight']
    assert [row.id for row in catalog.matching(resource_status='not-registered')] == [
        'cnr', 'ito', 'tet', 'ss', 've', 'nr', 'aa', 'comorian', 'to', 'ho']
    assert catalog.matching('not in the selected catalog') == ()
    for kwargs in ({'query': 'x' * 301}, {'region': 'Invented geography'}, {'resource_status': '135 voices verified'}):
        with pytest.raises(CatalogError):
            catalog.matching(**kwargs)


def test_report_never_promotes_upstream_registry_flags_to_final_capability():
    report = load_catalog().report()
    assert report['total_selected_entries'] == report['matching_entries'] == 135
    assert report['qualification_stage'] == 'last_after_core_and_secure_delivery'
    assert all(not row['automatic_voice_assignment'] and not row['windows_audio_verified_by_this_audit']
               and set(row['capabilities'].values()) == {'not_assessed'} for row in report['entries'])
    filipino = next(row for row in report['entries'] if row['id'] == 'fil')
    assert filipino['recorded_assertions']['character_names_ready'] is False
    assert filipino['capabilities']['accessible_reading'] == 'not_assessed'
    # A consumer can annotate its copy without modifying the sealed catalog.
    report['entries'][0]['capabilities']['tts'] = 'verified'
    assert load_catalog().report()['entries'][0]['capabilities']['tts'] == 'not_assessed'


@pytest.mark.parametrize('change', ['name', 'geography', 'voice_claim', 'count', 'source', 'extra_entry', 'extra_field'])
def test_changed_generated_projection_cannot_become_an_independent_editable_list(change):
    value = json.loads(payload())
    if change == 'name': value['entries'][0]['name_uk'] = 'A substitute language'
    elif change == 'geography': value['entries'][0]['geography'].pop()
    elif change == 'voice_claim': value['entries'][0]['windows_audio_verified_by_this_audit'] = True
    elif change == 'count': value['summary']['registered_with_resources'] = 135
    elif change == 'source': value['source_commit'] = '0' * 40
    elif change == 'extra_entry': value['entries'].append(value['entries'][0])
    else: value['download_url'] = 'https://example.invalid/unreviewed-model'
    with pytest.raises(CatalogError, match='pinned derivation'):
        validate_catalog_bytes(json.dumps(value).encode('utf-8'))


def test_projection_rejects_duplicate_keys_nonfinite_values_and_excess_size():
    for raw in (b'{"kind":1,"kind":2}', b'{"value":NaN}', b'{}' * (512 * 1024), b'\xff'):
        with pytest.raises(CatalogError):
            validate_catalog_bytes(raw)


def test_source_authentication_requires_both_exact_git_blob_and_sha256():
    for identity in SOURCES.values():
        with pytest.raises(CatalogError, match='Git blob and SHA256'):
            _verified(b'x' * identity.size, identity)
    with pytest.raises(CatalogError, match='byte size'):
        derive_catalog(selection_markdown=b'changed', selection_registry=b'{}', audit_markdown=b'changed', audit_registry=b'{}')


def test_markdown_geography_preserves_variant_description_and_order():
    text = ('## Intro\nNot a language.\n## Азія\n'
            '- Китайська — стандартна мандаринська — Китай, Тайвань, Сінгапур.\n'
            '- Перська, включно з дарі без окремого підрахунку — Іран, Афганістан.\n')
    assert _markdown_regions(text, ('Азія',)) == {'Азія': [
        ('Китайська — стандартна мандаринська', ['Китай', 'Тайвань', 'Сінгапур']),
        ('Перська, включно з дарі без окремого підрахунку', ['Іран', 'Афганістан'])]}
    with pytest.raises(CatalogError):
        _markdown_regions(text + '## Азія\n', ('Азія',))
    with pytest.raises(CatalogError):
        _markdown_regions(text, ('Європа', 'Азія'))


def test_offline_cli_explains_partial_inventory_without_loading_engine(capsys, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: pytest.fail('Unexpected network'))
    assert main(['report', '--query', 'kab']) == 0
    output = capsys.readouterr().out
    assert '1 matching entries' in output and 'tamazight' in output
    assert 'partial-variant-resources-present-unverified' in output
    assert 'not assessed' in output
    assert main(['report', '--query', 'uk', '--json']) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed['source_commit'] == REVISION and parsed['total_selected_entries'] == 135
