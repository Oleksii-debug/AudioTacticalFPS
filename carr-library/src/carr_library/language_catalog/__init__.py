"""Pinned selected-language inventory, separate from installed voice capability.

The public upstream Markdown remains authoritative. This package validates a
deterministic projection of that source and its recorded DEV2 resource audit;
it neither installs models nor assigns a language to a speech profile.
"""
from collections import Counter
from dataclasses import dataclass
from hashlib import sha1, sha256
from importlib.resources import files
import json

REPOSITORY = 'Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension'
REVISION = 'a1c73a5747bc86eb11f0c037120442e0c2593e91'
SCHEMA_VERSION = 1
DERIVATION = 'canonical-selected135-dev2-v1'
MAX_SOURCE_BYTES = 1024 * 1024
MAX_CATALOG_BYTES = 512 * 1024
MAX_QUERY = 300
CATALOG_SHA256 = 'a551edf490b65585943e755707d68dc7eff3e8468d0598c948f2f297df806a9d'


@dataclass(frozen=True)
class SourceIdentity:
    path: str
    git_blob_sha1: str
    sha256: str
    size: int

    def to_dict(self):
        return dict(path=self.path, git_blob_sha1=self.git_blob_sha1, sha256=self.sha256, size=self.size)


SOURCES = {
    'selection_markdown': SourceIdentity('dordiai-voices/SELECTED_LANGUAGES_UK.md',
        'caea8c5a8a5698971d75a565cba9a5a9c00b411f',
        'bdfcdd8f03e52df649e74091f4481769a7e55479f76b9334b73788dd4fd83cbb', 12281),
    'selection_registry': SourceIdentity('dordiai-voices/selected_languages.json',
        '8fc686ec2aeb839c6e7cac81f87384d9e8787964',
        '2478dbccacb967bc38afe28cf20db9c6ada517b8ad5e1c03c340db8a7693564d', 58887),
    'audit_markdown': SourceIdentity('dordiai-voices/LANGUAGE_COVERAGE_DEV2_UK.md',
        '8081b24c7c33eff578e7896052d4221cfff8c684',
        '696dd21a89b30e3ef09c1b581aca4432386c2aea8e8073bc206e29bf41b01067', 5888),
    'audit_registry': SourceIdentity('dordiai-voices/evidence/language_coverage_dev2.json',
        '6d96cd6704b6acc3d42e0624949656f1f8a59e8d',
        '6406a1302321f141dd50b00c9656be08166c6b86ed32c278215b9dc568b35904', 92631),
}
REGIONS = ('Європа', 'Північна Америка, Центральна Америка та Кариби',
           'Південна Америка', 'Азія', 'Африка', 'Австралія та Океанія')
REGIONAL_COUNTS = (37, 4, 8, 54, 39, 9)
RESOURCE_STATUSES = frozenset(('resources-present-unverified', 'not-registered',
                             'partial-variant-resources-present-unverified'))
CAPABILITIES = ('ui_menu_help_translation', 'unicode_text', 'rtl_text', 'vertical_text', 'ocr',
    'tts', 'phonemization', 'stress', 'tone', 'punctuation', 'segmentation', 'lexical_retrieval',
    'semantic_retrieval', 'translation', 'accessible_reading', 'input_keyboard_numbers_dates',
    'installed_model_voice_availability')
# Explicit unresolved/group qualifications in the pinned selection and DEV2
# audit, not guessed ISO/BCP47 conversions or a replacement language registry.
IDENTITY_REVIEW = {
    'tamazight': 'unresolved_group_standard',
    'comorian': 'unresolved_group_standard',
    'qu': 'group_variant_requires_review',
    'ff': 'group_variant_requires_review',
    'ku': 'group_variant_requires_review',
}
ASSERTION_KEYS = ('character_names_ready', 'number_normalization_ready',
                  'interactive_reading_ready', 'native_orthography_partial', 'keyboard_echo_verified')


class CatalogError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError('A language catalog contains a duplicate JSON field.')
        result[key] = value
    return result


def _json(raw, limit):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise CatalogError('The language catalog input is empty or exceeds its byte budget.')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda value: (_ for _ in ()).throw(CatalogError('Non-finite catalog value.')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CatalogError('The language catalog is not valid bounded UTF-8 JSON.') from exc


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def _verified(raw, identity):
    if not isinstance(raw, bytes) or len(raw) != identity.size or len(raw) > MAX_SOURCE_BYTES:
        raise CatalogError('A catalog source does not match its pinned byte size.')
    blob = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
    if blob != identity.git_blob_sha1 or sha256(raw).hexdigest() != identity.sha256:
        raise CatalogError('A catalog source does not match its pinned Git blob and SHA256.')
    try:
        return raw.decode('utf-8')
    except UnicodeError as exc:
        raise CatalogError('A pinned catalog source is not UTF-8.') from exc


def _markdown_regions(text, region_names):
    """Retain the last em-dash as geography separator; names may contain one."""
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith('## '):
            heading = line[3:].strip()
            current = heading if heading in region_names else None
            if current is not None:
                if current in sections:
                    raise CatalogError('A canonical geographic section occurs more than once.')
                sections[current] = []
        elif current is not None and line.startswith('- '):
            try:
                name, country_text = line[2:].rsplit(' — ', 1)
            except ValueError as exc:
                raise CatalogError('A selected language lacks explicit geography.') from exc
            if not country_text.endswith('.'):
                raise CatalogError('Unexpected selected-language geography format.')
            countries = country_text[:-1].split(', ')
            if not name or any(not country for country in countries):
                raise CatalogError('A selected language or country is empty.')
            sections[current].append((name, countries))
    if tuple(sections) != tuple(region_names):
        raise CatalogError('The canonical geographic order has changed.')
    return sections


def derive_catalog(*, selection_markdown, selection_registry, audit_markdown, audit_registry):
    """Project only exact reviewed source bytes; no network, models or guessing."""
    supplied = dict(selection_markdown=selection_markdown, selection_registry=selection_registry,
                    audit_markdown=audit_markdown, audit_registry=audit_registry)
    texts = {name: _verified(supplied[name], identity) for name, identity in SOURCES.items()}
    selected = _json(selection_registry, MAX_SOURCE_BYTES)
    audit = _json(audit_registry, MAX_SOURCE_BYTES)
    sections = _markdown_regions(texts['selection_markdown'], REGIONS)
    if (selected['unique_catalogue_entries'] != 135 or len(selected['languages']) != 135
            or tuple(row['name_uk'] for row in selected['regions']) != REGIONS
            or audit['format'] != 1 or audit['inventory_type'] != 'registration-and-nonempty-resource-presence-only'):
        raise CatalogError('The pinned source schemas no longer match this derivation.')
    by_id = {row['id']: row for row in selected['languages']}
    if len(by_id) != 135:
        raise CatalogError('The canonical selection has duplicate language identities.')
    order, regions = [], []
    for index, source_region in enumerate(selected['regions']):
        name = source_region['name_uk']
        identities = source_region['languages_in_user_order']
        if (source_region['priority'] != index + 1 or len(identities) != REGIONAL_COUNTS[index]
                or len(set(identities)) != len(identities)):
            raise CatalogError('A canonical geographic membership list is inconsistent.')
        entries = [(by_id[identity]['name_uk'], by_id[identity]['countries_by_region'][name])
                   for identity in identities]
        if entries != sections[name]:
            raise CatalogError('The machine selection differs from the authoritative Markdown geography.')
        regions.append(dict(name_uk=name, priority=index + 1, entries=identities))
        for identity in identities:
            if identity not in order:
                order.append(identity)
    if order != list(by_id) or len(order) != 135:
        raise CatalogError('Selected languages are not deduplicated in canonical first-occurrence order.')
    if [row['id'] for row in audit['languages']] != order:
        raise CatalogError('The recorded resource audit does not cover this exact selected catalog.')
    projected = []
    for selected_row, evidence in zip(selected['languages'], audit['languages']):
        identity = selected_row['id']
        expected_regions = [region['name_uk'] for region in regions if identity in region['entries']]
        if (selected_row['regions'] != expected_regions or set(selected_row['countries_by_region']) != set(expected_regions)
                or selected_row['first_priority_region'] != expected_regions[0]
                or any(evidence[field] != selected_row[field] for field in ('id', 'name_uk', 'regions', 'first_priority_region'))):
            raise CatalogError('A language identity, name or geographic membership changed between sources.')
        if (evidence['status'] not in RESOURCE_STATUSES or evidence['windows_audio_verified'] is not False
                or type(evidence['registered']) is not bool or type(evidence['resources_present']) is not bool):
            raise CatalogError('This derivation must not upgrade resource presence to speech acceptance.')
        assertions = {key: evidence[key] for key in ASSERTION_KEYS if key in evidence}
        if any(type(value) is not bool for value in assertions.values()):
            raise CatalogError('A recorded capability assertion is not boolean.')
        projected.append(dict(id=identity, name_uk=selected_row['name_uk'],
            geography=[dict(region=name, countries=selected_row['countries_by_region'][name]) for name in expected_regions],
            identity_review=IDENTITY_REVIEW.get(identity, 'retain_selected_identity'),
            resource_inventory=dict(status=evidence['status'], runtime_code=evidence['runtime_code'],
                backend=evidence['backend'], registered=evidence['registered'], resources_present=evidence['resources_present'],
                partial_variant=evidence.get('partial_variant', False),
                standard_note=evidence.get('standard_note', ''), recorded_assertions=assertions),
            windows_audio_verified_by_this_audit=False))
    summary = _summary(projected)
    expected = audit['summary']
    comparisons = {'selected_entries': 'selected_catalogue_entries',
        'registered_with_resources': 'registered_with_resources_present',
        'full_entry_resource_mappings': 'complete_selected_entries_with_resources',
        'partial_group_resource_mappings': 'partial_variant_resources_present',
        'registered_missing_resources': 'registered_with_missing_resources',
        'not_registered': 'not_registered', 'windows_audio_verified_by_this_audit': 'windows_audio_verified'}
    if any(summary[left] != expected[right] for left, right in comparisons.items()):
        raise CatalogError('Computed resource counts disagree with the pinned audit.')
    if summary['backend_resource_counts'] != expected['backend_counts_resources_present']:
        raise CatalogError('Computed resource backend counts disagree with the pinned audit.')
    for region, count, observed in zip(regions, REGIONAL_COUNTS, audit['regions']):
        present = sum(row['resource_inventory']['resources_present'] for row in projected if row['id'] in region['entries'])
        if observed != dict(name_uk=region['name_uk'], priority=region['priority'],
                            selected_entries_in_region=count, resources_present=present, missing=count - present):
            raise CatalogError('Regional resource counts disagree with the pinned audit.')
    result = dict(kind='carr-canonical-language-inventory', schema_version=SCHEMA_VERSION,
        derivation=DERIVATION, source_repository=REPOSITORY, source_commit=REVISION,
        sources={name: identity.to_dict() for name, identity in SOURCES.items()},
        work_order=selected['work_order'], regions=regions, entries=projected, summary=summary,
        unselected_resource_entries_not_counted=expected['existing_unselected_registry_entries'],
        capability_defaults={name: 'not_assessed' for name in CAPABILITIES},
        qualification_stage='last_after_core_and_secure_delivery',
        evidence_scope='Recorded DEV2 registration and nonempty resource inventory; not installed voice compatibility or acoustic acceptance.')
    return _canonical(result)


def _summary(entries):
    resources = [entry['resource_inventory'] for entry in entries]
    return dict(selected_entries=len(entries),
        registered_with_resources=sum(row['registered'] and row['resources_present'] for row in resources),
        full_entry_resource_mappings=sum(row['resources_present'] and not row['partial_variant'] for row in resources),
        partial_group_resource_mappings=sum(row['resources_present'] and row['partial_variant'] for row in resources),
        registered_missing_resources=sum(row['registered'] and not row['resources_present'] for row in resources),
        not_registered=sum(not row['registered'] for row in resources),
        windows_audio_verified_by_this_audit=sum(entry['windows_audio_verified_by_this_audit'] for entry in entries),
        backend_resource_counts=dict(sorted(Counter(row['backend'] for row in resources if row['resources_present']).items())))


def validate_catalog_bytes(raw):
    """Accept only the reviewed generated projection, with no mutable copy list."""
    value = _json(raw, MAX_CATALOG_BYTES)
    if not isinstance(value, dict) or sha256(_canonical(value)).hexdigest() != CATALOG_SHA256:
        raise CatalogError('The generated language catalog differs from its pinned derivation. Regenerate from the exact reviewed sources.')
    return value


@dataclass(frozen=True)
class Geography:
    region: str
    countries: tuple[str, ...]


@dataclass(frozen=True)
class ResourceInventory:
    status: str
    runtime_code: str
    backend: str
    registered: bool
    resources_present: bool
    partial_variant: bool
    standard_note: str
    recorded_assertions: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class LanguageEntry:
    id: str
    name_uk: str
    geography: tuple[Geography, ...]
    identity_review: str
    resource_inventory: ResourceInventory


@dataclass(frozen=True)
class LanguageCatalog:
    entries: tuple[LanguageEntry, ...]

    def matching(self, query='', *, region=None, resource_status=None):
        if not isinstance(query, str) or len(query) > MAX_QUERY:
            raise CatalogError(f'Use at most {MAX_QUERY} characters to search the selected catalog.')
        if region is not None and region not in REGIONS:
            raise CatalogError('Choose a geographic region from the canonical catalog.')
        if resource_status is not None and resource_status not in RESOURCE_STATUSES:
            raise CatalogError('Choose a recorded resource-inventory status.')
        words = query.casefold().split()
        result = []
        for entry in self.entries:
            if region is not None and not any(item.region == region for item in entry.geography):
                continue
            inventory = entry.resource_inventory
            if resource_status is not None and inventory.status != resource_status:
                continue
            terms = [entry.id, entry.name_uk, inventory.runtime_code, inventory.backend, inventory.standard_note]
            for item in entry.geography:
                terms.extend((item.region, *item.countries))
            haystack = ' '.join(terms).casefold()
            if all(word in haystack for word in words):
                result.append(entry)
        return tuple(result)

    def report(self, query='', *, region=None, resource_status=None):
        rows = []
        for entry in self.matching(query, region=region, resource_status=resource_status):
            inventory = entry.resource_inventory
            rows.append(dict(id=entry.id, name_uk=entry.name_uk,
                geography=[dict(region=item.region, countries=list(item.countries)) for item in entry.geography],
                identity_review=entry.identity_review,
                recorded_resource_status=inventory.status, recorded_runtime_code=inventory.runtime_code,
                recorded_backend=inventory.backend, partial_group_variant=inventory.partial_variant,
                standard_note=inventory.standard_note, recorded_assertions=dict(inventory.recorded_assertions),
                capabilities={name: 'not_assessed' for name in CAPABILITIES},
                automatic_voice_assignment=False, windows_audio_verified_by_this_audit=False))
        return dict(source_repository=REPOSITORY, source_commit=REVISION, catalog_sha256=CATALOG_SHA256,
            total_selected_entries=len(self.entries), matching_entries=len(rows),
            qualification_stage='last_after_core_and_secure_delivery',
            evidence_scope='Recorded DEV2 resources only; current installed voices and acoustic quality are not established.',
            entries=rows)


def load_catalog():
    value = validate_catalog_bytes(files(__package__).joinpath('UPSTREAM.json').read_bytes())
    entries = []
    for row in value['entries']:
        resource = dict(row['resource_inventory'])
        resource['recorded_assertions'] = tuple(sorted(resource['recorded_assertions'].items()))
        entries.append(LanguageEntry(row['id'], row['name_uk'],
            tuple(Geography(item['region'], tuple(item['countries'])) for item in row['geography']),
            row['identity_review'], ResourceInventory(**resource)))
    return LanguageCatalog(tuple(entries))
