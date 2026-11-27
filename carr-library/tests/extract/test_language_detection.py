"""Real pinned detector plus bounded policy and file-identity failure cases."""
from hashlib import sha256
from pathlib import Path

import pytest

from carr_library.extract import language_detection as lid
from carr_library.extract.page_ocr import Config

PARAGRAPHS = {
    'en': 'The research library preserves the original document and its exact words. '
          'A reader can compare every proposed quotation with the printed page before approving it.',
    'uk': 'Дослідницька бібліотека зберігає оригінальний документ та його точні слова. '
          'Читач може порівняти кожну запропоновану цитату з друкованою сторінкою перед підтвердженням.',
    'sk': 'Výskumná knižnica uchováva pôvodný dokument a jeho presné slová. '
          'Čitateľ môže porovnať každú navrhovanú citáciu s vytlačenou stranou pred jej schválením.',
}


@pytest.fixture(scope='module')
def detector():
    # Required production dependency and actual shipped model: no stub/skip.
    return lid.Detector()


@pytest.mark.parametrize('language,model', [('en', 'eng'), ('uk', 'ukr'), ('sk', 'slk')])
def test_actual_pinned_offline_model_classifies_long_reviewed_samples(detector, language, model):
    result = detector.identify(PARAGRAPHS[language])
    assert result['status'] == 'confident', result
    assert result['language'] == language
    assert result['confidence'] >= lid.MIN_CONFIDENCE
    assert result['margin'] >= lid.MIN_MARGIN
    assert lid.choose_model(result, ('eng', 'ukr', 'slk')) == (model, 'selected')
    assert result['sample_sha256'] == sha256(PARAGRAPHS[language].encode('utf-8')).hexdigest()
    identity = lid.detector_identity()
    assert identity['model_sha256'] == lid.MODEL_SHA256
    assert identity['version'] == '0.4.0'


def test_installed_pool_does_not_force_detector_to_name_an_available_language(detector):
    result = detector.identify(PARAGRAPHS['uk'])
    assert result['language'] == 'uk'
    assert lid.choose_model(result, ('eng',)) == (None, 'outside_selected_model_pool')
    assert result['language'] == 'uk'  # Choosing a model never narrows/re-runs inference.


@pytest.mark.parametrize('text', ['', 'ok', '1234 / 5678', '--- () [] {}', 'x'])
def test_short_or_nonlinguistic_input_abstains_without_fabricating_language(detector, text):
    result = detector.identify(text)
    assert result['language'] is None and result['status'] == 'insufficient_text'
    assert lid.choose_model(result, ('eng',))[0] is None


def test_language_variant_and_script_are_not_guessed():
    decision = {'status': 'confident', 'language': 'zh'}
    assert lid.choose_model(decision, ('chi_sim', 'chi_tra')) == (None, 'ambiguous_model_variant')
    assert lid.choose_model(decision, ('script/HanS', 'eng')) == (None, 'outside_selected_model_pool')
    assert lid.choose_model({'status': 'low_confidence', 'language': None}, ('eng',)) == (None, 'low_confidence')


def test_confidence_and_margin_are_required_before_model_selection(monkeypatch):
    detector = object.__new__(lid.Detector)
    detector.remaining = lambda: None
    class Classifier:
        def rank(self, text): return [('en', .51), ('uk', .49)]
    detector._identifier = Classifier()
    result = detector.identify(PARAGRAPHS['en'])
    assert result['predicted_language'] == 'en' and result['language'] is None
    assert result['status'] == 'low_confidence'
    assert lid.choose_model(result, ('eng',)) == (None, 'low_confidence')


def test_sample_is_bounded_and_exact_prefix_is_recorded(detector):
    value = PARAGRAPHS['uk'] * 1000
    result = detector.identify(value)
    sample = value[:lid.MAX_SAMPLE_BYTES].encode('utf-8')[:lid.MAX_SAMPLE_BYTES].decode('utf-8', 'ignore')
    assert result['sample_truncated'] and result['sample_bytes'] <= lid.MAX_SAMPLE_BYTES
    assert result['sample_sha256'] == sha256(sample.encode('utf-8')).hexdigest()


def test_descriptor_and_path_replacement_cannot_pass_pin_verification(tmp_path):
    source = tmp_path / 'model'; original = b'reviewed model bytes'
    source.write_bytes(original)
    expected = sha256(original).hexdigest()
    calls = 0
    def replace_after_open():
        nonlocal calls
        calls += 1
        if calls == 2:  # After the held descriptor closes, on both Windows and POSIX.
            next_path = tmp_path / 'new-model'
            next_path.write_bytes(original)
            next_path.replace(source)
    with pytest.raises(lid.DetectorUnavailable, match='identity'):
        lid._read_pin(source, len(original), expected, replace_after_open)


def test_wrong_model_bytes_and_cancelled_pin_read_are_refused(tmp_path):
    source = tmp_path / 'model'; source.write_bytes(b'actual')
    with pytest.raises(lid.DetectorUnavailable):
        lid._read_pin(source, 6, '0' * 64, lambda: None)
    def stopped():
        raise InterruptedError('cancelled before model admission')
    with pytest.raises(InterruptedError):
        lid._read_pin(source, 6, sha256(b'actual').hexdigest(), stopped)


@pytest.mark.parametrize('value', [1, None, 'auto', [], {}])
def test_config_requires_explicit_boolean_automatic_policy(value):
    with pytest.raises(ValueError, match='boolean'):
        Config(automatic_languages=value)


def test_manual_override_and_automatic_model_pool_bound():
    assert Config(languages='ukr').automatic_languages is False
    assert Config(languages='eng+ukr+slk', automatic_languages=True).automatic_languages
    with pytest.raises(ValueError, match='eight'):
        Config(languages='+'.join('model' + str(index) for index in range(9)), automatic_languages=True)


def test_automatic_runtime_identity_binds_actual_detector_model_and_implementation(monkeypatch):
    from carr_library.extract.runtime_identity import runtime_identity
    monkeypatch.setattr('carr_library.extract.ocr_runtime.discover_ocr_runtime', lambda *a, **k: None)
    value = runtime_identity(Config(automatic_languages=True))
    assert value['ocr']['state'] == 'missing'
    assert value['language_detector']['model_sha256'] == lid.MODEL_SHA256
    assert {'language_detection.py', 'ocr_regions.py', 'page_ocr.py'} <= value['source_sha256'].keys()


def test_manual_runtime_identity_does_not_require_detector(monkeypatch):
    from carr_library.extract.runtime_identity import runtime_identity
    monkeypatch.setattr('carr_library.extract.ocr_runtime.discover_ocr_runtime', lambda *a, **k: None)
    def forbidden(*args, **kwargs): pytest.fail('Manual configuration must not fingerprint a detector.')
    monkeypatch.setattr(lid, 'detector_identity', forbidden)
    value = runtime_identity(Config())
    assert 'language_detector' not in value
    assert 'language_detection.py' not in value['source_sha256']


def test_verified_source_buffers_ignore_a_valid_but_unreviewed_bytecode_cache(tmp_path, monkeypatch):
    """A real timestamp-valid .pyc must not replace the pinned implementation."""
    import importlib.machinery
    import importlib.util
    import marshal
    import struct
    import sys
    from types import SimpleNamespace

    _, captured = lid._verified(lambda: None)
    for name, data in captured.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    source = tmp_path / 'py3langid/langid.py'
    before = source.stat()
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    poisoned = compile("raise RuntimeError('UNVERIFIED_BYTECODE_EXECUTED')", str(source), 'exec')
    header = importlib.util.MAGIC_NUMBER + struct.pack(
        '<III', 0, int(before.st_mtime) & 0xffffffff, before.st_size & 0xffffffff)
    cache.write_bytes(header + marshal.dumps(poisoned))

    # Independent counterfactual proves the cache is valid for the original
    # source timestamp/length; simply adding -B would not prevent this load.
    ordinary = importlib.machinery.SourceFileLoader('unreviewed_cache_probe', str(source))
    with pytest.raises(RuntimeError, match='UNVERIFIED_BYTECODE_EXECUTED'):
        exec(ordinary.get_code('unreviewed_cache_probe'), {})

    distribution = lid.metadata.distribution
    find_spec = lid.importlib.util.find_spec
    selected = SimpleNamespace(version=lid.VERSION,
        locate_file=lambda name: tmp_path / name)
    monkeypatch.setattr(lid.metadata, 'distribution',
        lambda name: selected if name == 'py3langid' else distribution(name))
    monkeypatch.setattr(lid.importlib.util, 'find_spec',
        lambda name, *a, **k: SimpleNamespace(origin=str(tmp_path / 'py3langid/__init__.py'))
        if name == 'py3langid' else find_spec(name, *a, **k))
    lid._loaded_identifier.cache_clear()
    try:
        detector = lid.Detector()
        result = detector.identify(PARAGRAPHS['en'])
        assert result['language'] == 'en' and result['status'] == 'confident'
        assert source.read_bytes() == captured['py3langid/langid.py']
        assert cache.read_bytes() == header + marshal.dumps(poisoned)
        assert not any(name == '_carr_verified_py3langid_040'
                       or name.startswith('_carr_verified_py3langid_040.')
                       for name in sys.modules)
    finally:
        lid._loaded_identifier.cache_clear()


def test_private_classifier_namespace_collision_fails_before_source_execution(monkeypatch):
    import sys
    marker = object()
    monkeypatch.setitem(sys.modules, '_carr_verified_py3langid_040.langid', marker)
    lid._loaded_identifier.cache_clear()
    try:
        with pytest.raises(lid.DetectorUnavailable, match='namespace'):
            lid.Detector()
        assert sys.modules['_carr_verified_py3langid_040.langid'] is marker
    finally:
        lid._loaded_identifier.cache_clear()
