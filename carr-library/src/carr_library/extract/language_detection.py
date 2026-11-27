"""Pinned offline language identification for OCR candidates, never quote approval.

The detector is the published py3langid model. Tesseract model selection happens
only after unrestricted text classification; installed models never alter its
probability distribution. Scores are model confidence, not OCR accuracy.
"""
from functools import lru_cache
from hashlib import sha256
import importlib
import importlib.metadata as metadata
import importlib.util
import math
import os
from pathlib import Path
import tempfile

from ..durable_state import local_path, regular

VERSION = '0.4.0'
POLICY = 'py3langid-0.4.0-unrestricted-v1'
MODEL_NAME = 'py3langid/data/model.npz.xz'
MODEL_SHA256 = 'f4f4a2c3465ca1f081541037f9cac23021d68151c2f709c55ae5eedfca522963'
MODEL_BYTES = 4586720
MAX_SAMPLE_BYTES = 8192
MIN_LETTERS = 48
MIN_CONFIDENCE = 0.85
MIN_MARGIN = 0.20
PINS = {
    'py3langid/__init__.py': (73, 'aa5e55604af5a54b69c5a0103ea52dc2c485ab833d91be455a17b7bb06b449a6'),
    'py3langid/langid.py': (12687, '87dd47f1253b2adc6f82ed96f53e385a80bbde4f3627741cc516e7d0dd4a55d5'),
    'py3langid/modelio.py': (2965, '598e46056edfb6507ebcdafbb5c64b57724c0543c19fa8a4de526d3d0513dbd6'),
    MODEL_NAME: (MODEL_BYTES, MODEL_SHA256),
}

# ISO 639 identifiers mapped to the existing Tesseract model names. Ambiguous
# script-specific pairs are deliberately not guessed when both are selected.
MODEL_CODES = {
    'af': ('afr',), 'am': ('amh',), 'ar': ('ara',), 'as': ('asm',),
    'az': ('aze', 'aze_cyrl'), 'be': ('bel',), 'bg': ('bul',), 'bn': ('ben',),
    'bo': ('bod',), 'br': ('bre',), 'bs': ('bos',), 'ca': ('cat',),
    'cs': ('ces',), 'cy': ('cym',), 'da': ('dan',), 'de': ('deu',),
    'dz': ('dzo',), 'el': ('ell',), 'en': ('eng',), 'eo': ('epo',),
    'es': ('spa',), 'et': ('est',), 'eu': ('eus',), 'fa': ('fas',),
    'fi': ('fin',), 'fo': ('fao',), 'fr': ('fra',), 'fy': ('fry',),
    'ga': ('gle',), 'gd': ('gla',), 'gl': ('glg',), 'grc': ('grc',),
    'gu': ('guj',), 'he': ('heb',), 'hi': ('hin',), 'hr': ('hrv',),
    'ht': ('hat',), 'hu': ('hun',), 'hy': ('hye',), 'id': ('ind',),
    'is': ('isl',), 'it': ('ita',), 'ja': ('jpn',), 'jv': ('jav',),
    'ka': ('kat',), 'kk': ('kaz',), 'km': ('khm',), 'kn': ('kan',),
    'ko': ('kor',), 'ky': ('kir',), 'la': ('lat',),
    'lb': ('ltz',), 'lo': ('lao',), 'lt': ('lit',), 'lv': ('lav',),
    'mg': ('mlg',), 'mi': ('mri',), 'mk': ('mkd',), 'ml': ('mal',),
    'mn': ('mon',), 'mr': ('mar',), 'ms': ('msa',), 'mt': ('mlt',),
    'my': ('mya',), 'ne': ('nep',), 'nl': ('nld',), 'no': ('nor',),
    'oc': ('oci',), 'or': ('ori',), 'pa': ('pan',), 'pl': ('pol',),
    'ps': ('pus',), 'pt': ('por',), 'ro': ('ron',), 'ru': ('rus',),
    'sa': ('san',), 'si': ('sin',), 'sk': ('slk',),
    'sl': ('slv',), 'sq': ('sqi',), 'sr': ('srp', 'srp_latn'),
    'su': ('sun',), 'sv': ('swe',), 'sw': ('swa',), 'ta': ('tam',),
    'te': ('tel',), 'tg': ('tgk',), 'th': ('tha',), 'ti': ('tir',),
    'tl': ('fil',), 'tr': ('tur',), 'tt': ('tat',), 'ug': ('uig',),
    'uk': ('ukr',), 'ur': ('urd',), 'uz': ('uzb', 'uzb_cyrl'),
    'vi': ('vie',), 'yi': ('yid',), 'yo': ('yor',),
    'zh': ('chi_sim', 'chi_tra'),
}


class DetectorUnavailable(ValueError):
    pass


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_pin(path, size, expected, remaining):
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size != size:
        raise DetectorUnavailable('The installed language detector does not match its pinned files.')
    digest, pieces, total = sha256(), [], 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise DetectorUnavailable('The language detector changed before verification.')
        while block := stream.read(min(1024 * 1024, size - total + 1)):
            remaining()
            total += len(block)
            if total > size:
                raise DetectorUnavailable('The language detector grew during verification.')
            pieces.append(block)
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise DetectorUnavailable('The language detector changed during verification.')
    remaining()
    if (total != size or digest.hexdigest() != expected
            or _stamp(regular(path)) != _stamp(before)):
        raise DetectorUnavailable('The language detector does not match its pinned identity.')
    return b''.join(pieces)


def _verified(remaining):
    try:
        distribution = metadata.distribution('py3langid')
        if distribution.version != VERSION:
            raise DetectorUnavailable('Install the reviewed py3langid 0.4.0 package for automatic OCR.')
        contents, paths = {}, {}
        for name, (size, digest) in PINS.items():
            remaining()
            paths[name] = local_path(distribution.locate_file(name), exists=True)
            contents[name] = _read_pin(paths[name], size, digest, remaining)
        spec = importlib.util.find_spec('py3langid')
        if spec is None or spec.origin is None or Path(spec.origin).resolve() != paths['py3langid/__init__.py']:
            raise DetectorUnavailable('A different language detector package shadows the reviewed installation.')
    except InterruptedError:
        raise
    except (metadata.PackageNotFoundError, ImportError, OSError) as exc:
        raise DetectorUnavailable('The pinned offline language detector is unavailable.') from exc
    return paths, contents


def detector_identity(remaining=lambda: None):
    _verified(remaining)
    return {'policy': POLICY, 'package': 'py3langid', 'version': VERSION,
            'model_sha256': MODEL_SHA256, 'model_bytes': MODEL_BYTES,
            'classification_scope': 'all shipped languages; never restricted to installed OCR models'}


@lru_cache(maxsize=1)
def _loaded_identifier(model_bytes):
    # Load only a private copy of bytes already matched to the independently
    # pinned model. The upstream loader uses np.load(allow_pickle=False).
    module = importlib.import_module('py3langid.langid')
    with tempfile.TemporaryDirectory(prefix='carr-language-model-') as temporary:
        path = Path(temporary) / 'model.npz.xz'
        path.write_bytes(model_bytes)
        return module.LanguageIdentifier.from_model_file(path, norm_probs=True,
                                                        min_confidence=MIN_CONFIDENCE)


class Detector:
    def __init__(self, *, remaining=lambda: None):
        self.remaining = remaining
        paths, contents = _verified(remaining)
        self._identifier = _loaded_identifier(contents[MODEL_NAME])
        remaining()
        module = importlib.import_module('py3langid.langid')
        if Path(module.__file__).resolve() != paths['py3langid/langid.py']:
            raise DetectorUnavailable('The active language detector has a different source identity.')
        # Keep the imported implementation and model checks within one observed
        # installation, including a mutation while loading the private model.
        _verified(remaining)

    def identify(self, raw):
        if type(raw) is not str:
            raise ValueError('Language detection needs text from the bounded OCR pass.')
        self.remaining()
        sample = raw[:MAX_SAMPLE_BYTES].encode('utf-8')[:MAX_SAMPLE_BYTES].decode('utf-8', 'ignore')
        raw_sample = sample.encode('utf-8')
        result = {'language': None, 'predicted_language': None, 'confidence': 0.0,
                  'margin': 0.0, 'sample_sha256': sha256(raw_sample).hexdigest(),
                  'sample_bytes': len(raw_sample), 'sample_truncated': len(sample) < len(raw),
                  'status': 'insufficient_text'}
        if sum(char.isalpha() for char in sample) < MIN_LETTERS:
            return result
        ranked = self._identifier.rank(sample)
        self.remaining()
        if not ranked or len(ranked) > 256:
            raise DetectorUnavailable('The pinned detector returned an invalid language inventory.')
        language, confidence = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if (type(language) is not str or not 1 <= len(language) <= 8
                or not all(math.isfinite(float(score)) and 0 <= float(score) <= 1
                           for _, score in ranked)):
            raise DetectorUnavailable('The pinned detector returned invalid confidence data.')
        confidence, margin = float(confidence), float(confidence - runner_up)
        result.update(predicted_language=language, confidence=confidence, margin=margin)
        if language in ('und', 'zxx'):
            result['status'] = 'nonlinguistic_or_undetermined'
        elif confidence < MIN_CONFIDENCE or margin < MIN_MARGIN:
            result['status'] = 'low_confidence'
        else:
            result.update(language=language, status='confident')
        return result


def choose_model(decision, selected):
    """Map a confident language only after unrestricted classification."""
    if decision['status'] != 'confident':
        return None, decision['status']
    candidates = [code for code in MODEL_CODES.get(decision['language'], ()) if code in selected]
    if len(candidates) == 1:
        return candidates[0], 'selected'
    return None, 'ambiguous_model_variant' if candidates else 'outside_selected_model_pool'
