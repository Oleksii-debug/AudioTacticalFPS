"""Real pinned detector plus bounded policy and file-identity failure cases."""
from hashlib import sha256
from pathlib import Path

import pytest

from carr_library.extract import language_detection as lid
from carr_library.extract.page_ocr import Config, ExtendedConfig

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


def test_descriptor_and_path_replacement_cannot_pass_pin_verification(tmp_path, monkeypatch):
    source = tmp_path / 'model'; original = b'reviewed model bytes'
    source.write_bytes(original)
    real_open = lid._open_json_file
    opened, replaced, blocked = [], [], []
    def replace_while_descriptor_is_held(path):
        stream = real_open(path)
        opened.append(stream)
        replacement = tmp_path / 'replacement-model'
        replacement.write_bytes(original)
        try:
            replacement.replace(source)
        except PermissionError:
            # Windows' actual shared-read opener prevents replacing a live file.
            # In that case the original remains valid; no replacement was read.
            if __import__('os').name != 'nt':
                stream.close()
                raise
            blocked.append(True)
        except BaseException:
            stream.close()
            raise
        else:
            replaced.append(True)
        return stream
    monkeypatch.setattr(lid, '_open_json_file', replace_while_descriptor_is_held)
    try:
        value = lid._read_pin(source, len(original), sha256(original).hexdigest(), lambda: None)
    except lid.DetectorUnavailable:
        assert replaced == [True] and not blocked
    else:
        assert blocked == [True] and not replaced and value == original
        assert (tmp_path / 'replacement-model').is_file()
    assert opened and all(stream.closed for stream in opened)


def test_actual_replacement_before_open_is_refused_before_reading_its_bytes(tmp_path, monkeypatch):
    source = tmp_path / 'model'; original = b'reviewed model bytes'
    source.write_bytes(original)
    real_open = lid._open_json_file
    opened = []
    class NoRead:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, *args): pytest.fail('A replaced model must fail before its first byte is read.')
    def replace_before_open(path):
        replacement = tmp_path / 'replacement-model'
        replacement.write_bytes(original)
        replacement.replace(source)
        stream = real_open(path)
        opened.append(stream)
        return NoRead(stream)
    monkeypatch.setattr(lid, '_open_json_file', replace_before_open)
    with pytest.raises(lid.DetectorUnavailable):
        lid._read_pin(source, len(original), sha256(original).hexdigest(), lambda: None)
    assert opened and all(stream.closed for stream in opened)


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
        ExtendedConfig(automatic_languages=value)


def test_manual_override_and_automatic_model_pool_bound():
    assert Config(languages='ukr').automatic_languages is False
    assert ExtendedConfig(languages='eng+ukr+slk', automatic_languages=True).automatic_languages
    with pytest.raises(ValueError, match='eight'):
        ExtendedConfig(languages='+'.join('model' + str(index) for index in range(9)), automatic_languages=True)


def test_automatic_runtime_identity_binds_actual_detector_model_and_implementation(monkeypatch):
    from carr_library.extract.runtime_identity import runtime_identity
    monkeypatch.setattr('carr_library.extract.ocr_runtime.discover_ocr_runtime', lambda *a, **k: None)
    value = runtime_identity(ExtendedConfig(automatic_languages=True))
    assert value['ocr']['state'] == 'missing'
    assert value['language_detector']['model_sha256'] == lid.MODEL_SHA256
    assert {'language_detection.py', 'ocr_regions.py', 'raster_pages.py'} <= value['source_sha256'].keys()


def test_manual_runtime_identity_does_not_require_detector(monkeypatch):
    from carr_library.extract.runtime_identity import runtime_identity
    monkeypatch.setattr('carr_library.extract.ocr_runtime.discover_ocr_runtime', lambda *a, **k: None)
    def forbidden(*args, **kwargs): pytest.fail('Manual configuration must not fingerprint a detector.')
    monkeypatch.setattr(lid, 'detector_identity', forbidden)
    value = runtime_identity(Config())
    assert 'language_detector' not in value
    assert 'language_detection.py' not in value['source_sha256']
