"""Synthetic passive data only; these values are never language qualifications."""
from dataclasses import replace
from hashlib import sha256

from carr_library.language_qualification import load_catalog
from carr_library.language_qualification.evidence import (
    ArtifactIdentity, EvidenceStore, PersonProvenance, QualificationObservation,
    RecordedContext, RecordedReview, TestObservation,
)


def observation(capability='tts', *, outcome='pass', coverage='full-selected-entry', context=None):
    source = 'Synthetic input only; this is not acoustic or linguistic evidence.'
    recorded = context or RecordedContext(
        ArtifactIdentity('Synthetic fixture application', '1', 'a' * 64),
        'Synthetic fixture platform', 'fixture', 'Not a real tested installation',
        sha256(b'fixture environment').hexdigest())
    test = TestObservation('synthetic-fixture', '1',
        'declaration' if outcome == 'not_run' else 'executed',
        PersonProvenance('fixture-executor', 'Synthetic fixture provenance', 'fixture'),
        None if outcome == 'not_run' else '2026-01-01T00:00:00Z',
        source, sha256(source.encode()).hexdigest(), 'Synthetic expected result',
        outcome, 'Synthetic result; no real language quality claim.',
        None if outcome == 'not_run' else sha256(b'synthetic result bytes').hexdigest())
    catalog = load_catalog()
    return QualificationObservation(catalog.source.sha256, catalog.entries[0].id, 'en-GB',
        'Synthetic exact selected-entry scope; not a language certificate.', coverage,
        capability, recorded,
        ArtifactIdentity('Synthetic fixture engine', '1', 'b' * 64),
        ArtifactIdentity('Synthetic fixture pack', '1', 'c' * 64), test)


def reviewer():
    return PersonProvenance('fixture-reviewer', 'Independent synthetic test actor', 'fixture reviewer')


def review(value, decision='accept'):
    return RecordedReview(value.fingerprint, reviewer(), '2026-01-02T00:00:00Z',
                          decision, True, 'Synthetic recorded human assessment for boundary tests only.')


def saved(root, value=None):
    value = value or observation()
    store = EvidenceStore(root)
    snapshot, token = store.add(value, expected_token=None)
    return store, value, snapshot, token


def assess(store, value, token, decision='accept'):
    return store.review(value.fingerprint, reviewer(), decision=decision,
        independent_assessment=True, statement='Synthetic fixture assessment only.',
        confirmation=decision.upper() + ' ' + value.fingerprint, expected_token=token)
