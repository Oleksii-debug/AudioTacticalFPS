"""Passive, scoped language observations and explicit recorded human reviews.

Checksums bind exact data; they do not authenticate people or prove that a
claimed experiment happened. Import never accepts review/approval authority.
No artifact, engine, model, test input or provenance reference is executed.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import unicodedata

from ..durable_state import (atomic_bytes, encode, local_path, read_json_snapshot,
                             regular, state_lock)
from .catalog import CatalogError, _CAPABILITIES, canonical_language_tag, load_catalog

OBSERVATION_SCHEMA = 'carr-language-observation/1'
STATE_SCHEMA = 'carr-language-evidence/1'
REPORT_SCHEMA = 'carr-language-evidence-report/1'
RELATIVE_PATH = 'language-qualification/evidence.json'
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_OBSERVATIONS = 8192
MAX_REVIEWS = 16384
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise CatalogError('Unknown or missing qualification evidence fields.')


def _text(value, maximum=1000, *, empty=False, multiline=False):
    if (type(value) is not str or len(value) > maximum
            or (not empty and not value.strip())
            or any((ord(c) < 32 and not (multiline and c in '\n\t'))
                   or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise CatalogError('Qualification evidence needs bounded readable text.')
    return value


def _hash(value):
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise CatalogError('Use an exact lowercase SHA-256 identity.')
    return value


def _utc(value):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise CatalogError('Use an explicit UTC observation or review time.')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError as error:
        raise CatalogError('Invalid qualification evidence UTC time.') from error
    return value


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _check(cancelled):
    if not callable(cancelled):
        raise CatalogError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Language evidence operation cancelled.')


def _fingerprint(value):
    return sha256(encode(value)).hexdigest()


def _person(value):
    # A consistency check only, never authentication or proof of independence.
    return unicodedata.normalize('NFKC', value).casefold().strip()


@dataclass(frozen=True)
class ArtifactIdentity:
    name: str | None
    version: str | None
    sha256: str | None
    not_applicable_reason: str | None = None

    def __post_init__(self):
        if self.not_applicable_reason is not None:
            _text(self.not_applicable_reason, 1000)
            if any(x is not None for x in (self.name, self.version, self.sha256)):
                raise CatalogError('An inapplicable artifact needs a reason, not an executable identity.')
        else:
            _text(self.name, 256)
            _text(self.version, 256)
            _hash(self.sha256)

    @classmethod
    def from_dict(cls, value):
        _fields(value, ('name', 'version', 'sha256', 'not_applicable_reason'))
        return cls(**value)


@dataclass(frozen=True)
class RecordedContext:
    application: ArtifactIdentity
    platform: str
    architecture: str
    environment: str
    environment_sha256: str

    def __post_init__(self):
        if type(self.application) is not ArtifactIdentity or self.application.not_applicable_reason is not None:
            raise CatalogError('Record the exact application build identity.')
        _text(self.platform, 256)
        _text(self.architecture, 64)
        _text(self.environment, 4000, multiline=True)
        _hash(self.environment_sha256)

    @property
    def fingerprint(self):
        return _fingerprint(asdict(self))

    @classmethod
    def from_dict(cls, value):
        _fields(value, ('application', 'platform', 'architecture', 'environment', 'environment_sha256'))
        return cls(ArtifactIdentity.from_dict(value['application']), value['platform'],
                   value['architecture'], value['environment'], value['environment_sha256'])


@dataclass(frozen=True)
class PersonProvenance:
    identity: str
    source: str
    role: str

    def __post_init__(self):
        _text(self.identity, 200)
        _text(self.source, 1000)
        _text(self.role, 256)

    @classmethod
    def from_dict(cls, value):
        _fields(value, ('identity', 'source', 'role'))
        return cls(**value)


@dataclass(frozen=True)
class TestObservation:
    identity: str
    version: str
    kind: str
    executor: PersonProvenance
    performed_utc: str | None
    input_text: str
    input_sha256: str
    expected_result: str
    outcome: str
    result: str
    result_sha256: str | None

    def __post_init__(self):
        _text(self.identity, 256)
        _text(self.version, 256)
        if self.kind not in ('executed', 'declaration') or type(self.kind) is not str:
            raise CatalogError('Declare whether the submitted test was actually executed.')
        if type(self.executor) is not PersonProvenance:
            raise CatalogError('Record exact executor provenance.')
        _text(self.input_text, 8192, empty=True, multiline=True)
        if _hash(self.input_sha256) != sha256(self.input_text.encode('utf-8')).hexdigest():
            raise CatalogError('The test input differs from its recorded digest.')
        _text(self.expected_result, 4000, multiline=True)
        _text(self.result, 8192, multiline=True)
        if type(self.outcome) is not str or self.outcome not in ('pass', 'partial', 'fail', 'not_run'):
            raise CatalogError('Record pass, partial, fail or not_run explicitly.')
        if self.kind == 'declaration' or self.outcome == 'not_run':
            if self.outcome != 'not_run' or self.performed_utc is not None or self.result_sha256 is not None:
                raise CatalogError('An unexecuted declaration cannot claim a completed test result.')
        else:
            _utc(self.performed_utc)
            _hash(self.result_sha256)

    @classmethod
    def from_dict(cls, value):
        _fields(value, ('identity', 'version', 'kind', 'executor', 'performed_utc',
                        'input_text', 'input_sha256', 'expected_result', 'outcome',
                        'result', 'result_sha256'))
        return cls(**{**value, 'executor': PersonProvenance.from_dict(value['executor'])})


@dataclass(frozen=True)
class QualificationObservation:
    catalog_sha256: str
    language_id: str
    language_tag: str
    language_scope: str
    coverage: str
    capability: str
    context: RecordedContext
    engine: ArtifactIdentity
    pack: ArtifactIdentity
    test: TestObservation

    def __post_init__(self):
        _hash(self.catalog_sha256)
        _text(self.language_id, 100)
        if (type(self.language_tag) is not str
                or canonical_language_tag(self.language_tag) != self.language_tag):
            raise CatalogError('Use the exact canonical spelling of the observed language tag.')
        _text(self.language_scope, 2000, multiline=True)
        if type(self.coverage) is not str or self.coverage not in ('full-selected-entry', 'partial-variety'):
            raise CatalogError('Distinguish full selected-entry coverage from a partial variety.')
        if type(self.capability) is not str or self.capability not in _CAPABILITIES:
            raise CatalogError('Choose one existing capability from the canonical matrix.')
        if (type(self.context) is not RecordedContext or type(self.engine) is not ArtifactIdentity
                or type(self.pack) is not ArtifactIdentity or type(self.test) is not TestObservation):
            raise CatalogError('Use typed exact configuration and test observations.')

    def validate_catalog(self, catalog):
        if self.catalog_sha256 != catalog.source.sha256:
            raise CatalogError('Evidence belongs to another canonical language snapshot.')
        catalog.get(self.language_id)
        return self

    @property
    def fingerprint(self):
        return _fingerprint(self.to_dict())

    @property
    def qualification_scope(self):
        return _fingerprint({'context': self.context.fingerprint,
            'language_id': self.language_id, 'tag': self.language_tag,
            'scope': self.language_scope, 'coverage': self.coverage})

    def to_dict(self):
        return {'schema': OBSERVATION_SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, value, *, catalog=None):
        _fields(value, ('schema', 'catalog_sha256', 'language_id', 'language_tag',
            'language_scope', 'coverage', 'capability', 'context', 'engine', 'pack', 'test'))
        if value['schema'] != OBSERVATION_SCHEMA:
            raise CatalogError('Unsupported qualification observation schema.')
        result = cls(**{key: value[key] for key in
            ('catalog_sha256', 'language_id', 'language_tag', 'language_scope', 'coverage', 'capability')},
            context=RecordedContext.from_dict(value['context']),
            engine=ArtifactIdentity.from_dict(value['engine']),
            pack=ArtifactIdentity.from_dict(value['pack']),
            test=TestObservation.from_dict(value['test']))
        result.validate_catalog(catalog or load_catalog())
        if len(encode(result.to_dict())) > MAX_OBSERVATION_BYTES:
            raise CatalogError('The observation exceeds its 64 KiB allowance.')
        return result


@dataclass(frozen=True)
class RecordedReview:
    observation_sha256: str
    reviewer: PersonProvenance
    reviewed_utc: str
    decision: str
    independent_assessment: bool
    statement: str

    def __post_init__(self):
        _hash(self.observation_sha256)
        if type(self.reviewer) is not PersonProvenance:
            raise CatalogError('Record explicit reviewer provenance.')
        _utc(self.reviewed_utc)
        if type(self.decision) is not str or self.decision not in ('accept', 'reject', 'revoke'):
            raise CatalogError('Choose an explicit recorded review decision.')
        if type(self.independent_assessment) is not bool:
            raise CatalogError('The recorded independence acknowledgement must be boolean.')
        _text(self.statement, 4000, multiline=True)
        if self.decision == 'accept' and not self.independent_assessment:
            raise CatalogError('Acceptance needs an explicit recorded independent assessment.')

    @property
    def fingerprint(self):
        return _fingerprint(asdict(self))

    def validate_observation(self, observation):
        if self.observation_sha256 != observation.fingerprint:
            raise CatalogError('The review does not bind this complete observation.')
        return self._check_provenance(observation)

    def _check_provenance(self, observation):
        if self.decision == 'accept':
            if _person(self.reviewer.identity) == _person(observation.test.executor.identity):
                raise CatalogError('Executor and independent reviewer identities must differ.')
            if observation.test.performed_utc is not None and self.reviewed_utc < observation.test.performed_utc:
                raise CatalogError('A review cannot predate its recorded experiment.')
        return self

    @classmethod
    def from_dict(cls, value):
        _fields(value, ('observation_sha256', 'reviewer', 'reviewed_utc', 'decision',
                        'independent_assessment', 'statement'))
        return cls(**{**value, 'reviewer': PersonProvenance.from_dict(value['reviewer'])})


@dataclass(frozen=True)
class EvidenceSnapshot:
    revision: int = 0
    observations: tuple[QualificationObservation, ...] = ()
    reviews: tuple[RecordedReview, ...] = ()
    dormant_through: int = 0

    def __post_init__(self):
        if type(self.revision) is not int or not 0 <= self.revision < 2**63:
            raise CatalogError('Invalid qualification state revision.')
        if (type(self.observations) is not tuple or len(self.observations) > MAX_OBSERVATIONS
                or any(type(row) is not QualificationObservation for row in self.observations)
                or type(self.reviews) is not tuple or len(self.reviews) > MAX_REVIEWS
                or any(type(row) is not RecordedReview for row in self.reviews)):
            raise CatalogError('Qualification state exceeds its typed collection allowance.')
        if type(self.dormant_through) is not int or not 0 <= self.dormant_through <= len(self.reviews):
            raise CatalogError('Invalid restored review boundary.')
        by_id = {row.fingerprint: row for row in self.observations}
        if len(by_id) != len(self.observations):
            raise CatalogError('Duplicate immutable qualification observation.')
        for row in self.reviews:
            if row.observation_sha256 not in by_id:
                raise CatalogError('A review references a missing exact observation.')
            # The call-local map already binds every canonical observation digest.
            row._check_provenance(by_id[row.observation_sha256])

    def get(self, identity):
        _hash(identity)
        for row in self.observations:
            if row.fingerprint == identity:
                return row
        raise CatalogError('Choose an existing exact qualification observation.')

    def contexts(self):
        result = {}
        for row in self.observations:
            result.setdefault(row.context.fingerprint, row.context)
        return tuple(result.values())

    def assessments(self, context_identity):
        _hash(context_identity)
        if context_identity not in {row.fingerprint for row in self.contexts()}:
            raise CatalogError('Choose one explicitly recorded configuration.')
        by_id = {row.fingerprint: row for row in self.observations}
        result = {}
        for row in self.observations:
            if row.context.fingerprint == context_identity:
                result[(row.language_id, row.capability)] = (row, None, False)
        # Reviewing an exact record explicitly selects it for this capability.
        # Imports cannot replace an existing review selection.
        for index, review in enumerate(self.reviews, 1):
            row = by_id[review.observation_sha256]
            if row.context.fingerprint == context_identity:
                result[(row.language_id, row.capability)] = (row, review, index > self.dormant_through)
        return result

    def to_dict(self):
        return {'schema': STATE_SCHEMA, 'revision': self.revision,
            'observations': [row.to_dict() for row in self.observations],
            'reviews': [asdict(row) for row in self.reviews], 'dormant_through': self.dormant_through}

    @classmethod
    def from_dict(cls, value, *, catalog=None, cancelled=lambda: False):
        _fields(value, ('schema', 'revision', 'observations', 'reviews', 'dormant_through'))
        if value['schema'] != STATE_SCHEMA:
            raise CatalogError('Unsupported qualification state schema.')
        if (type(value['observations']) is not list or len(value['observations']) > MAX_OBSERVATIONS
                or type(value['reviews']) is not list or len(value['reviews']) > MAX_REVIEWS):
            raise CatalogError('Invalid bounded qualification collections.')
        catalog = catalog or load_catalog()
        observations, reviews = [], []
        for row in value['observations']:
            _check(cancelled)
            observations.append(QualificationObservation.from_dict(row, catalog=catalog))
        for row in value['reviews']:
            _check(cancelled)
            reviews.append(RecordedReview.from_dict(row))
        _check(cancelled)
        result = cls(value['revision'], tuple(observations), tuple(reviews), value['dormant_through'])
        if len(encode(result.to_dict())) > MAX_STATE_BYTES:
            raise CatalogError('Qualification state exceeds its byte allowance.')
        return result


def import_observation(path, *, cancelled=lambda: False):
    """Parse one passive observation; generated reports and approvals refuse."""
    _check(cancelled)
    value, source_digest = read_json_snapshot(path, max_bytes=MAX_OBSERVATION_BYTES)
    _check(cancelled)
    return QualificationObservation.from_dict(value), source_digest


class EvidenceSaveCommittedError(OSError):
    """The exact update is visible, but final durability/cleanup is uncertain."""
    def __init__(self, snapshot, token):
        self.snapshot, self.token = snapshot, token
        self.durability_confirmed = False
        super().__init__('Language evidence was saved; final durability is unconfirmed. '
                         'Reload the saved record before another change. Do not repeat the review.')


class EvidenceStore:
    """Existing private state, checked directory identities and exact-byte CAS."""
    def __init__(self, library_root, *, guard=lambda: None):
        if not callable(guard):
            raise CatalogError('A current-library guard is required.')
        self._external_guard = guard
        guard()
        self.root = local_path(library_root, exists=True)
        regular(self.root, directory=True)
        self.path = self.root / 'state' / RELATIVE_PATH
        self._directories = {}
        for directory in reversed((self.path.parent, *self.path.parent.parents)):
            if os.path.lexists(directory):
                info = regular(local_path(directory, exists=True), directory=True)
                self._directories[directory] = info.st_dev, info.st_ino
        self.guard()

    def guard(self):
        self._external_guard()
        self._validate_scope()

    def _validate_scope(self):
        local_path(self.path, root=self.root)
        for directory, identity in self._directories.items():
            info = regular(directory, directory=True)
            if (info.st_dev, info.st_ino) != identity:
                raise CatalogError('The selected language evidence directory changed. Reopen the view.')

    def _check(self, cancelled):
        _check(cancelled)
        self.guard()

    def load(self, *, cancelled=lambda: False):
        self._check(cancelled)
        if not os.path.lexists(self.path):
            return EvidenceSnapshot(), None
        value, token = read_json_snapshot(self.path, max_bytes=MAX_STATE_BYTES)
        self._check(cancelled)
        result = EvidenceSnapshot.from_dict(value, cancelled=cancelled)
        self._check(cancelled)
        return result, token

    def _prepare_parent(self, cancelled):
        for directory in (self.root / 'state', self.path.parent):
            self._check(cancelled)
            directory.mkdir(mode=0o700, exist_ok=True)
            self.guard()
            info = regular(local_path(directory, root=self.root, exists=True), directory=True)
            identity = info.st_dev, info.st_ino
            if directory in self._directories and self._directories[directory] != identity:
                raise CatalogError('The language evidence directory changed during creation.')
            self._directories[directory] = identity

    def _change(self, operation, expected_token, cancelled):
        if expected_token is not None:
            _hash(expected_token)
        self._prepare_parent(cancelled)
        saved, digest = None, None
        try:
            with state_lock(self.path.parent, 'language-evidence'):
                current, token = self.load(cancelled=cancelled)
                if token != expected_token:
                    raise CatalogError('Language evidence changed after review. Reload before saving.')
                candidate = operation(current)
                if type(candidate) is not EvidenceSnapshot:
                    raise CatalogError('Use typed qualification state.')
                payload = encode(candidate.to_dict())
                if len(payload) > MAX_STATE_BYTES:
                    raise CatalogError('Qualification state exceeds its byte allowance.')
                if candidate == current:
                    return current, token
                self._check(cancelled)
                # The last user callback may have changed even an identical-revision
                # document. Recheck the exact bytes immediately before publication.
                if os.path.lexists(self.path):
                    _, fresh = read_json_snapshot(self.path, max_bytes=MAX_STATE_BYTES)
                else:
                    fresh = None
                self._validate_scope()
                if fresh != token:
                    raise CatalogError('Language evidence changed before publication. Reload.')
                digest = sha256(payload).hexdigest()
                try:
                    atomic_bytes(self.path, payload, replace=token is not None)
                except FileExistsError:
                    raise
                except OSError as error:
                    try:
                        _, visible = read_json_snapshot(self.path, max_bytes=MAX_STATE_BYTES)
                    except (OSError, ValueError):
                        visible = None
                    if visible == digest:
                        raise EvidenceSaveCommittedError(candidate, digest) from error
                    raise
                saved = candidate
            return saved, digest
        except EvidenceSaveCommittedError:
            raise
        except OSError as error:
            if saved is not None:
                raise EvidenceSaveCommittedError(saved, digest) from error
            raise

    def add(self, observation, *, expected_token, cancelled=lambda: False):
        if type(observation) is not QualificationObservation:
            raise CatalogError('Import one validated passive observation.')
        observation.validate_catalog(load_catalog())
        if len(encode(observation.to_dict())) > MAX_OBSERVATION_BYTES:
            raise CatalogError('The observation exceeds its byte allowance.')

        def apply(current):
            if any(row.fingerprint == observation.fingerprint for row in current.observations):
                return current
            return replace(current, revision=current.revision + 1,
                           observations=current.observations + (observation,))
        return self._change(apply, expected_token, cancelled)

    def review(self, observation_sha256, reviewer, *, decision, independent_assessment,
               statement, confirmation, expected_token, cancelled=lambda: False):
        _hash(observation_sha256)
        review = RecordedReview(observation_sha256, reviewer, _now(), decision,
                                independent_assessment, statement)
        if confirmation != decision.upper() + ' ' + observation_sha256:
            raise CatalogError('Confirm the exact review decision and complete observation digest.')

        def apply(current):
            review.validate_observation(current.get(observation_sha256))
            return replace(current, revision=current.revision + 1, reviews=current.reviews + (review,))
        return self._change(apply, expected_token, cancelled)


def assessment_status(observation, review, active):
    """A recorded assessment of one exact configuration, not live device proof."""
    if review is not None and review.decision == 'revoke':
        return 'revoked'
    if review is not None and review.decision == 'reject':
        return 'failed'
    if review is None or not active:
        return 'candidate'
    if observation.test.outcome == 'not_run':
        return 'untested'
    if observation.test.outcome == 'fail':
        return 'failed'
    if observation.test.outcome == 'partial' or observation.coverage == 'partial-variety':
        return 'partial'
    return 'verified'


def assessment_detail(observation, review, active):
    def artifact(label, value):
        if value.not_applicable_reason is not None:
            return label + ': not applicable — ' + value.not_applicable_reason
        return label + ': ' + value.name + '; version ' + value.version + '; SHA-256 ' + value.sha256

    lines = ['Recorded evidence only; no automatic execution or current-device qualification.',
        'Observation SHA-256: ' + observation.fingerprint,
        'Recorded configuration: ' + observation.context.fingerprint,
        artifact('Application', observation.context.application),
        'Platform: ' + observation.context.platform + '; architecture: ' + observation.context.architecture,
        'Environment: ' + observation.context.environment,
        'Environment SHA-256: ' + observation.context.environment_sha256,
        artifact('Engine', observation.engine), artifact('Pack', observation.pack),
        'Language tag: ' + observation.language_tag + '; scope: ' + observation.language_scope,
        'Coverage: ' + observation.coverage,
        'Test: ' + observation.test.identity + '; version: ' + observation.test.version,
        'Reported outcome: ' + observation.test.outcome,
        'Reported result: ' + observation.test.result,
        'Input SHA-256: ' + observation.test.input_sha256,
        'Result SHA-256: ' + (observation.test.result_sha256 or 'not run'),
        'Executor: ' + observation.test.executor.identity + '; ' + observation.test.executor.source]
    if review is None:
        lines.append('No local human assessment has been recorded; imported claims cannot verify a capability.')
    else:
        lines.extend(('Recorded human decision: ' + review.decision,
            'Reviewer: ' + review.reviewer.identity + '; ' + review.reviewer.source,
            'Review time: ' + review.reviewed_utc, 'Review statement: ' + review.statement,
            'Review SHA-256: ' + review.fingerprint,
            'Reviewer identities and independence are recorded assertions, not authenticated identities.'))
        if not active:
            lines.append('Restored historical review: explicit local re-review is required before verification.')
    return '\n'.join(lines)
