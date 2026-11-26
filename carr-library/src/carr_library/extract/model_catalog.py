"""Typed model-manager adapter over the single packaged official catalogue.

The catalogue and its upstream provenance belong to ocr_model_catalog.
This adapter preserves the native manager/store API and materializes compatible
alias choices. It never maintains another editable list or accesses a network.
Git blob verification identifies reviewed bytes; it is not a publisher signature
or a claim of OCR accuracy.
"""
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha1, sha256
from pathlib import Path

from . import ocr_model_catalog as official
from .ocr_languages import is_model_id


REVISIONS = {
    'fast': '87416418657359cb625c412a48b6e1d6d41c29bd',
    'best': 'e12c65a915945e4c28e237a9b52bc4a8f39a0cec',
}


@dataclass(frozen=True)
class Model:
    identifier: str
    label: str
    kind: str
    profile: str
    revision: str
    size: int
    blob_sha1: str
    relative_path: str
    alias_of: str | None = None

    @property
    def download_url(self):
        """Exact source URL for an explicit, separate acquisition step."""
        return (f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_{self.profile}/'
                f'{self.revision}/{self.relative_path}')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate OCR manifest field.')
        result[key] = value
    return result


@lru_cache(maxsize=1)
def _catalogue():
    resource = official.files(official.__package__).joinpath(official.CATALOGUE)
    raw = resource.read_bytes()
    if len(raw) > official.MAX_CATALOGUE:
        raise ValueError('OCR catalogue exceeds its size limit.')
    profiles, provenance = {}, {}
    try:
        for profile, revision in REVISIONS.items():
            source = official.load_source(profile)
            if source.revision != revision:
                raise ValueError('Unexpected pinned OCR profile revision.')
            rows, by_code = [], {model.code: model for model in source.models}
            for identifier, target in [*((code, code) for code in by_code), *source.aliases]:
                model = by_code[target]
                label = model.name
                if (not is_model_id(identifier) or not isinstance(label, str)
                        or not 1 <= len(label) <= 200 or any(ord(c) < 32 for c in label)):
                    raise ValueError('Invalid OCR catalogue label or identifier.')
                # Preserve localized search names from the canonical parser
                # without maintaining a second name mapping.
                localized = official.NAMES.get(target, '')
                if localized and localized.casefold() not in label.casefold():
                    label += ' / ' + localized
                if identifier != target:
                    label += f' (compatibility alias: {identifier})'
                rows.append(Model(identifier, label,
                                  'auxiliary' if model.kind == 'special' else model.kind,
                                  profile, revision, model.size, model.git_blob_sha1,
                                  model.path, target if identifier != target else None))
            if len({model.identifier.casefold() for model in rows}) != len(rows):
                raise ValueError('Ambiguous OCR model paths.')
            profiles[profile] = tuple(sorted(rows, key=lambda m: (m.label.casefold(), m.identifier)))
            license_raw = source.license_text.encode('utf-8')
            digest = sha1(usedforsecurity=False)
            digest.update(f'blob {len(license_raw)}\0'.encode('ascii'))
            digest.update(license_raw)
            provenance[profile] = {
                'repository': source.repository, 'revision': revision,
                'license': 'Apache-2.0', 'license_blob_sha1': digest.hexdigest(),
            }
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid packaged OCR catalogue.') from exc
    if resource.read_bytes() != raw:
        raise ValueError('The OCR catalogue changed during inspection.')
    return profiles, sha256(raw).hexdigest(), provenance


def models(profile, query=''):
    if profile not in REVISIONS:
        raise ValueError('Choose the Fast or Best OCR model profile.')
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError('Invalid OCR model search.')
    terms = query.casefold().split()
    return tuple(m for m in _catalogue()[0][profile]
                 if all(term in f'{m.identifier} {m.label} {m.kind}'.casefold() for term in terms))


def catalogue_sha256():
    return _catalogue()[1]


def profile_provenance(profile):
    models(profile)
    return dict(_catalogue()[2][profile])


def license_bytes(profile):
    """Return the complete notice, checked against the authoritative catalogue."""
    provenance = profile_provenance(profile)
    raw = Path(__file__).with_name(f'TESSDATA_{profile.upper()}_LICENSE').read_bytes()
    digest = sha1(usedforsecurity=False)
    digest.update(f'blob {len(raw)}\0'.encode('ascii'))
    digest.update(raw)
    if digest.hexdigest() != provenance['license_blob_sha1']:
        raise ValueError('The packaged OCR model license does not match its source.')
    return raw
