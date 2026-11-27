"""Derive the selected 135 entries from the exact approved Markdown snapshot.

There is no second editable language list, model download, runtime discovery or
engine execution in this module. Installed-voice matching consumes an already
enumerated registry plus explicit language-tag bindings; it never infers a
language, national accent, script or acoustic quality from similar names.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha1, sha256
from pathlib import Path
import re
import unicodedata
from typing import Iterable

_DATA = Path(__file__).resolve().parent / 'data'
_REPOSITORY = 'Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension'
_COMMIT = 'a1c73a5747bc86eb11f0c037120442e0c2593e91'
_CATALOG_SCHEMA = 'carr.selected-languages/1'
_MATRIX_SCHEMA = 'carr.language-qualification/1'
_REGION_KEYS = (
    ('Європа', 'europe', 'europe'),
    ('Північна Америка, Центральна Америка та Кариби', 'america-north-central-caribbean', 'america'),
    ('Південна Америка', 'america-south', 'america'),
    ('Азія', 'asia', 'asia'),
    ('Африка', 'africa', 'africa'),
    ('Австралія та Океанія', 'australia-oceania', 'australia-oceania'),
)
_CAPABILITIES = (
    'ui_translation', 'menu_translation', 'help_translation', 'unicode_text',
    'rtl_layout', 'vertical_layout', 'ocr', 'tts', 'phonemization', 'stress',
    'tone', 'punctuation', 'segmentation', 'lexical_retrieval',
    'semantic_retrieval', 'translation', 'accessible_reading', 'keyboard_input',
    'number_rules', 'date_rules', 'model_availability', 'voice_availability',
)
# These are qualification caveats from the pinned audit, not a substitute
# catalog or an ISO/BCP47 mapping. Full selected entries are parsed below.
_GROUP_LABELS = frozenset(('Тамазигхт', 'Коморська', 'Кечуа',
                          'Фула, або фульфульде', 'Курдська'))
_NATIONAL_VARIANTS = frozenset(('Англійська', 'Французька', 'Португальська', 'Іспанська'))


class CatalogError(ValueError):
    """The canonical source or explicit matching input failed validation."""


@dataclass(frozen=True)
class SourceSnapshot:
    repository: str
    commit: str
    path: str
    git_blob: str
    sha256: str

    @property
    def url(self):
        return f'https://github.com/{self.repository}/blob/{self.commit}/{self.path}'


_SELECTED = SourceSnapshot(_REPOSITORY, _COMMIT, 'dordiai-voices/SELECTED_LANGUAGES_UK.md',
    'caea8c5a8a5698971d75a565cba9a5a9c00b411f',
    'bdfcdd8f03e52df649e74091f4481769a7e55479f76b9334b73788dd4fd83cbb')
_AUDIT = SourceSnapshot(_REPOSITORY, _COMMIT, 'dordiai-voices/LANGUAGE_COVERAGE_DEV2_UK.md',
    '8081b24c7c33eff578e7896052d4221cfff8c684',
    '696dd21a89b30e3ef09c1b581aca4432386c2aea8e8073bc206e29bf41b01067')


@dataclass(frozen=True)
class Region:
    id: str
    label_uk: str
    continent: str
    order: int


@dataclass(frozen=True)
class RegionAssociation:
    region_id: str
    countries: tuple[str, ...]
    source_line: int
    source_text: str


@dataclass(frozen=True)
class LanguageEntry:
    id: str
    label_uk: str
    order: int
    scope: str
    associations: tuple[RegionAssociation, ...]
    identity_notes: tuple[str, ...]
    # The canonical source names languages/standards, not locale/voice IDs.
    canonical_locale: None = field(default=None, init=False)
    canonical_voice: None = field(default=None, init=False)

    @property
    def region_ids(self):
        return tuple(x.region_id for x in self.associations)

    @property
    def countries(self):
        return tuple(dict.fromkeys(c for association in self.associations for c in association.countries))


@dataclass(frozen=True)
class RegionalAudit:
    region_id: str
    selected: int
    resources_reported: int
    missing_reported: int


@dataclass(frozen=True)
class ResourceAudit:
    source: SourceSnapshot
    selected: int
    resources_reported: int
    missing_reported: int
    missing_dependencies_reported: int
    full_entry_resource_mappings_reported: int
    partial_group_resource_mappings_reported: int
    windows_nvda_acoustic_tests_reported: int
    regions: tuple[RegionalAudit, ...]
    inventory_only: bool = field(default=True, init=False)
    local_installation_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class LanguageCatalog:
    source: SourceSnapshot
    audit: ResourceAudit
    regions: tuple[Region, ...]
    entries: tuple[LanguageEntry, ...]
    schema: str = _CATALOG_SCHEMA

    def get(self, identifier: str) -> LanguageEntry:
        for entry in self.entries:
            if entry.id == identifier:
                return entry
        raise CatalogError('Select a stable language ID from the canonical catalog.')

    def search(self, query: str = '', *, region: str | None = None) -> tuple[LanguageEntry, ...]:
        query = _query(query)
        if region is not None and region not in {x.id for x in self.regions}:
            raise CatalogError('Choose a canonical region ID.')
        region_names = {r.id: r.label_uk for r in self.regions}
        result = []
        for entry in self.entries:
            if region is not None and region not in entry.region_ids:
                continue
            text = ' '.join((entry.id, entry.label_uk, *entry.countries,
                             *(region_names[x] for x in entry.region_ids), *entry.identity_notes))
            if query in unicodedata.normalize('NFC', text).casefold():
                result.append(entry)
        return tuple(result)

    def to_dict(self):
        return {'schema': self.schema, 'source': asdict(self.source),
                'source_url': self.source.url, 'resource_audit': asdict(self.audit),
                'regions': [asdict(x) for x in self.regions],
                'entries': [asdict(x) for x in self.entries]}


def _query(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise CatalogError('The language search query must be bounded text.')
    return unicodedata.normalize('NFC', value).casefold().strip()


def _read(snapshot: SourceSnapshot) -> str:
    path = _DATA / Path(snapshot.path).name
    try:
        if path.stat().st_size > 100_000:
            raise CatalogError('The canonical language snapshot exceeds its size budget.')
        raw = path.read_bytes()
    except OSError as exc:
        raise CatalogError('The installed canonical language snapshot is missing.') from exc
    blob = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\x00' + raw).hexdigest()
    if sha256(raw).hexdigest() != snapshot.sha256 or blob != snapshot.git_blob:
        raise CatalogError('The canonical language snapshot failed its pinned integrity check.')
    return raw.decode('utf-8')


def _scope(label: str) -> tuple[str, tuple[str, ...]]:
    if label in _GROUP_LABELS:
        return 'group-standard-unresolved', (
            'The pinned audit requires evidence for the exact language standard; one resource cannot qualify every variety.',)
    if label.startswith('Китайська — стандартна мандаринська'):
        return 'named-standard', ('Standard Mandarin only; other Chinese languages are not implied.',)
    if label.startswith('Філіппінська — стандарт на тагальській основі'):
        return 'named-standard', ('Filipino is the selected standard; a Tagalog label is not an automatic replacement.',)
    if label.startswith('Перська, включно з дарі'):
        return 'language-with-variants', ('Dari is retained inside the single selected Persian entry, not counted again.',)
    if label == 'Норвезька':
        return 'language-with-variants', (
            'The upstream inventory maps nb; nn is an additional optional resource, not another selected language.',)
    if label in _NATIONAL_VARIANTS:
        return 'language-with-national-variants', (
            'National pronunciation variants are separate from language and voice identity; geographic coverage does not prove an accent.',)
    if label in ('Північна ндебеле', 'Південна ндебеле'):
        return 'language', ('Northern and Southern Ndebele remain distinct selected entries.',)
    if label == 'Чорногорська':
        return 'language', ('Do not substitute a related language merely because a compatible resource name looks similar.',)
    return 'language', ()


def load_catalog() -> LanguageCatalog:
    """Verify and derive the catalog anew; never trust an editable generated list."""
    source, audit_text = _read(_SELECTED), _read(_AUDIT)
    regions = tuple(Region(key, label, continent, index) for index, (label, key, continent) in enumerate(_REGION_KEYS))
    by_title = {r.label_uk: r for r in regions}
    selected: dict[str, list[RegionAssociation]] = {}
    active, seen_regions = None, []
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith('## '):
            active = by_title.get(line[3:])
            if active is not None:
                seen_regions.append(active.id)
        if line.startswith('- '):
            if active is None or ' — ' not in line:
                raise CatalogError('An unexpected selection row changed the canonical language format.')
            label, geography = line[2:].rsplit(' — ', 1)
            label = unicodedata.normalize('NFC', label.strip())
            countries = tuple(part.strip() for part in geography.removesuffix('.').split(','))
            if not label or any(not x for x in countries):
                raise CatalogError('A canonical selection row is incomplete.')
            association = RegionAssociation(active.id, countries, number, line)
            group = selected.setdefault(label, [])
            if any(x.region_id == active.id for x in group):
                raise CatalogError('The canonical selection repeats a language inside one region.')
            group.append(association)
    if seen_regions != [r.id for r in regions] or len(selected) != 135:
        raise CatalogError('The pinned catalog must contain exactly 135 entries in the approved regional order.')
    entries = []
    for index, (label, associations) in enumerate(selected.items(), 1):
        scope, notes = _scope(label)
        entries.append(LanguageEntry('selected-' + sha256(label.encode('utf-8')).hexdigest()[:24],
                                     label, index, scope, tuple(associations), notes))
    if len({x.id for x in entries}) != 135:
        raise CatalogError('Canonical stable language IDs must be unique.')
    summary = re.search(r'Вибрано (\d+) пунктів\. Для (\d+) є реєстрація.*?Для (\d+) немає.*?Ще (\d+) зареєстрованих', audit_text)
    mappings = re.search(r'ресурсів для повних вибраних пунктів: (\d+); явно позначених часткових варіантів груп: (\d+)\.', audit_text)
    acoustics = re.search(r'акустичних перевірок Windows/NVDA: (\d+)\.', audit_text)
    if not all((summary, mappings, acoustics)):
        raise CatalogError('The pinned resource inventory cannot be parsed faithfully.')
    regional = []
    for label, count, resources, missing in re.findall(
            r'^- (.+): вибрано (\d+); ресурси наявні для (\d+); відсутні для (\d+)\.$',
            audit_text, re.MULTILINE):
        if label not in by_title:
            raise CatalogError('The resource inventory contains an unknown region.')
        row = RegionalAudit(by_title[label].id, int(count), int(resources), int(missing))
        actual = sum(row.region_id in entry.region_ids for entry in entries)
        if row.selected != actual or row.resources_reported + row.missing_reported != row.selected:
            raise CatalogError('The resource inventory and selected geography disagree.')
        regional.append(row)
    chosen, registered, missing, dependencies = map(int, summary.groups())
    full, partial = map(int, mappings.groups())
    acoustic_tests = int(acoustics.group(1))
    if (chosen != len(entries) or registered + missing != chosen or full + partial != registered
            or [x.region_id for x in regional] != [x.id for x in regions]):
        raise CatalogError('The canonical inventory totals are inconsistent.')
    audit = ResourceAudit(_AUDIT, chosen, registered, missing, dependencies,
                          full, partial, acoustic_tests, tuple(regional))
    return LanguageCatalog(_SELECTED, audit, regions, tuple(entries))


def canonical_language_tag(value: str, *, adapter_underscores: bool = False) -> str:
    """Validate BCP47-shaped tags, without claiming IANA/CLDR registration.

    Exact matching never drops a script, region, variant, extension or private
    subtag. Legacy underscores are accepted only for an adapter's declaration.
    Grandfathered tags require a separately reviewed explicit modern mapping.
    """
    if not isinstance(value, str) or not 2 <= len(value) <= 128 or not value.isascii():
        raise CatalogError('Use a bounded BCP47 language tag.')
    if adapter_underscores:
        value = value.replace('_', '-')
    parts = value.split('-')
    if not re.fullmatch(r'[A-Za-z]{2,8}', parts[0]):
        raise CatalogError('Use a structurally valid BCP47 primary language tag.')
    result, index = [parts[0].lower()], 1
    if len(parts[0]) <= 3:
        extlangs = 0
        while index < len(parts) and re.fullmatch(r'[A-Za-z]{3}', parts[index]) and extlangs < 3:
            result.append(parts[index].lower())
            index += 1
            extlangs += 1
    if index < len(parts) and re.fullmatch(r'[A-Za-z]{4}', parts[index]):
        result.append(parts[index].title())
        index += 1
    if index < len(parts) and re.fullmatch(r'[A-Za-z]{2}|[0-9]{3}', parts[index]):
        result.append(parts[index].upper())
        index += 1
    variants = set()
    while index < len(parts) and re.fullmatch(r'[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}', parts[index]):
        variant = parts[index].lower()
        if variant in variants:
            raise CatalogError('A BCP47 variant must not repeat.')
        variants.add(variant)
        result.append(variant)
        index += 1
    extensions = set()
    while index < len(parts):
        singleton = parts[index].lower()
        if not re.fullmatch(r'[a-z0-9]', singleton) or singleton in extensions:
            raise CatalogError('A BCP47 extension must be valid and unique.')
        extensions.add(singleton)
        result.append(singleton)
        index += 1
        start = index
        pattern = r'[A-Za-z0-9]{1,8}' if singleton == 'x' else r'[A-Za-z0-9]{2,8}'
        while index < len(parts) and re.fullmatch(pattern, parts[index]):
            result.append(parts[index].lower())
            index += 1
        if start == index:
            raise CatalogError('A BCP47 extension needs a value.')
        if singleton == 'x' and index != len(parts):
            raise CatalogError('Private-use subtags must end a BCP47 tag.')
    return '-'.join(result)


@dataclass(frozen=True)
class DeclaredLanguageBinding:
    """A caller-reviewed tag association, never a pronunciation certificate."""
    language_id: str
    tag: str
    evidence: str
    coverage: str = 'candidate'

    def __post_init__(self):
        object.__setattr__(self, 'tag', canonical_language_tag(self.tag))
        if (not isinstance(self.language_id, str) or not self.language_id
                or not isinstance(self.evidence, str) or not self.evidence.strip()
                or len(self.evidence) > 4000):
            raise CatalogError('An explicit language binding needs a catalog ID and its review evidence.')
        if self.coverage not in ('candidate', 'partial'):
            raise CatalogError('A declared tag binding can only be candidate or partial, never verified.')


@dataclass(frozen=True)
class VoiceMatch:
    voice_id: str
    voice_name: str
    engine: str
    adapter_language: str
    matched_tag: str
    license: str
    controls: tuple[str, ...]
    binding_evidence: str
    coverage: str


@dataclass(frozen=True)
class Capability:
    name: str
    status: str
    detail: str
    voice_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LanguageQualification:
    language: LanguageEntry
    capabilities: tuple[Capability, ...]
    voices: tuple[VoiceMatch, ...]

    def capability(self, name: str):
        for value in self.capabilities:
            if value.name == name:
                return value
        raise CatalogError('Unknown language capability.')


@dataclass(frozen=True)
class CapabilityMatrix:
    catalog: LanguageCatalog
    entries: tuple[LanguageQualification, ...]
    unmatched_voice_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    schema: str = _MATRIX_SCHEMA

    def search(self, query: str = '', *, region: str | None = None,
               capability: str | None = None, status: str | None = None):
        query = _query(query)
        if capability is not None and capability not in _CAPABILITIES:
            raise CatalogError('Unknown language capability filter.')
        if status is not None and status not in ('untested', 'unsupported', 'candidate', 'partial', 'verified'):
            raise CatalogError('Unknown capability status filter.')
        allowed = {x.id for x in self.catalog.search('', region=region)}
        result = []
        for entry in self.entries:
            if entry.language.id not in allowed:
                continue
            values = [entry.capability(capability)] if capability else entry.capabilities
            if status is not None and not any(x.status == status for x in values):
                continue
            text = ' '.join((entry.language.label_uk, entry.language.id, *entry.language.countries,
                            *(v.voice_name + ' ' + v.matched_tag + ' ' + v.engine for v in entry.voices)))
            if query not in unicodedata.normalize('NFC', text).casefold():
                continue
            result.append(entry)
        return tuple(result)

    def summary(self):
        return {'selected_entries': len(self.entries),
                'entries_with_declared_voice_matches': sum(bool(x.voices) for x in self.entries),
                'distinct_matched_voices': len({v.voice_id for x in self.entries for v in x.voices}),
                'qualified_entries': 0, 'unmatched_voices': len(self.unmatched_voice_ids),
                'upstream_reported_resources': self.catalog.audit.resources_reported,
                'upstream_reported_acoustic_tests': self.catalog.audit.windows_nvda_acoustic_tests_reported}

    def to_dict(self):
        return {'schema': self.schema, 'catalog_source': asdict(self.catalog.source),
                'resource_audit': asdict(self.catalog.audit), 'summary': self.summary(),
                'entries': [asdict(x) for x in self.entries],
                'unmatched_voice_ids': list(self.unmatched_voice_ids), 'warnings': list(self.warnings)}


def build_capability_matrix(*, catalog: LanguageCatalog | None = None,
                            voices: Iterable = (),
                            bindings: Iterable[DeclaredLanguageBinding] = ()) -> CapabilityMatrix:
    """Match exact declared tags on already enumerated voices, without synthesis.

    Pass ``registry.voices()`` from the existing voice registry. This function
    does not discover adapters, inspect model weights, load helpers, perform
    acoustic tests or turn the separate upstream inventory into local support.
    """
    catalog = catalog or load_catalog()
    bound = {}
    for index, binding in enumerate(bindings):
        if index >= 2000 or not isinstance(binding, DeclaredLanguageBinding):
            raise CatalogError('Supply a bounded sequence of explicit language-tag bindings.')
        entry = catalog.get(binding.language_id)
        if binding.tag in bound and bound[binding.tag][0].id != entry.id:
            raise CatalogError('One exact language tag cannot silently qualify different selected entries.')
        if binding.tag in bound:
            raise CatalogError('Duplicate exact language-tag binding.')
        bound[binding.tag] = (entry, binding)
    matched = {x.id: [] for x in catalog.entries}
    unmatched, warnings, seen_voices = [], [], set()
    from ..audio.engines import Voice
    for index, voice in enumerate(voices):
        if (index >= 10_000 or not isinstance(voice, Voice)
                or not isinstance(voice.id, str) or not voice.id or len(voice.id) > 512
                or voice.id in seen_voices):
            raise CatalogError('Use distinct bounded voice records from the installed voice registry.')
        seen_voices.add(voice.id)
        if (not isinstance(voice.languages, tuple) or len(voice.languages) > 256
                or not isinstance(voice.controls, tuple) or len(voice.controls) > 64
                or any(not isinstance(value, str) or len(value) > 4000
                       for value in (voice.name, voice.engine, voice.license, *voice.controls))
                or type(voice.available) is not bool):
            raise CatalogError('The installed voice metadata is malformed or exceeds its limits.')
        if not voice.available:
            unmatched.append(voice.id)
            warnings.append(f'Voice {voice.id} is unavailable; its declaration does not establish local support.')
            continue
        tags = {}
        for language in voice.languages:
            try:
                tag = canonical_language_tag(language, adapter_underscores=True)
            except CatalogError:
                warnings.append(f'Voice {voice.id} has an invalid declared language tag; no language was inferred.')
                continue
            tags.setdefault(tag, language)
        found = False
        for tag, language in tags.items():
            if tag not in bound:
                continue
            entry, binding = bound[tag]
            coverage = 'partial' if entry.scope == 'group-standard-unresolved' else binding.coverage
            matched[entry.id].append(VoiceMatch(voice.id, voice.name, voice.engine, language,
                tag, voice.license, voice.controls, binding.evidence, coverage))
            found = True
        if not found:
            unmatched.append(voice.id)
    records = []
    for entry in catalog.entries:
        matches = tuple(matched[entry.id])
        status = 'candidate' if any(v.coverage == 'candidate' for v in matches) else 'partial'
        capabilities = []
        for name in _CAPABILITIES:
            value = Capability(name, 'untested', 'No language-specific qualification evidence has been recorded.')
            if name in ('tts', 'voice_availability'):
                value = (Capability(name, status,
                    'Exact caller-declared tag matches an enumerated local voice; pronunciation, model version and accessibility remain untested.',
                    tuple(v.voice_id for v in matches)) if matches else Capability(name, 'unsupported',
                    'No confirmed local voice was supplied for an explicit exact language-tag binding.'))
            elif name in ('phonemization', 'punctuation'):
                control = 'phonemes' if name == 'phonemization' else 'punctuation'
                supported = tuple(v.voice_id for v in matches if control in v.controls)
                if supported:
                    value = Capability(name, 'candidate',
                        'The matched adapter declares this control; language-specific output is untested.', supported)
            capabilities.append(value)
        records.append(LanguageQualification(entry, tuple(capabilities), matches))
    warnings.append('The upstream 125-resource inventory is separate from this installation and proves zero Windows/NVDA acoustic tests.')
    return CapabilityMatrix(catalog, tuple(records), tuple(unmatched), tuple(warnings))
