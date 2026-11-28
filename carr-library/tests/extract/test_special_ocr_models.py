"""Special OCR detectors stay inventoried but cannot become reading languages."""
import pytest

from carr_library.extract import language_manager
from carr_library.extract.language_manager import LanguageModel, installed_languages, language_selection
from carr_library.extract.ocr_runtime import OcrRuntime, OcrRuntimeError
from carr_library.extract.page_ocr import Config


def _runtime(*, portable=True):
    return OcrRuntime('fixture-tesseract', manifest_sha256='a' * 64 if portable else None,
                      languages=('eng', 'equ', 'osd', 'script/Latin'))


@pytest.mark.parametrize('portable', [True, False])
def test_production_inventory_classifies_special_models_without_hiding_them(monkeypatch, portable):
    runtime = _runtime(portable=portable)
    if not portable:
        def list_languages(command, *, stdout, **kwargs):
            assert command == ['fixture-tesseract', '--list-langs']
            stdout.write(b'List of available languages in "fixture" (4):\neng\nequ\nosd\nscript/Latin\n')
        monkeypatch.setattr(language_manager.subprocess, 'run', list_languages)
    models = {model.code: model for model in installed_languages(runtime)}
    assert set(models) == {'eng', 'equ', 'osd', 'script/Latin'}
    assert models['eng'].kind == 'language'
    assert models['script/Latin'].kind == 'script'
    assert models['osd'].kind == 'orientation'
    assert models['equ'].kind == 'equation'
    assert 'not a reading language' in models['equ'].name


@pytest.mark.parametrize('codes', [('equ',), ('osd',), ('eng', 'equ'), ('script/Latin', 'osd')])
def test_reading_selection_rejects_special_codes_even_in_old_classified_inventory(codes):
    # A cached inventory from an older version must not re-enable these codes.
    available = (LanguageModel('eng', 'English', 'language'),
                 LanguageModel('script/Latin', 'Latin', 'script'),
                 LanguageModel('equ', 'Equation', 'language'),
                 LanguageModel('osd', 'Orientation', 'language'))
    with pytest.raises(ValueError, match='reading'):
        language_selection(codes, available)


@pytest.mark.parametrize('kind', ['equation', 'orientation', 'special', 'unknown'])
def test_reading_selection_requires_an_explicit_reading_kind(kind):
    with pytest.raises(ValueError, match='reading'):
        language_selection(('detector',), (LanguageModel('detector', 'Detector', kind),))


@pytest.mark.parametrize('languages', ['equ', 'osd', 'eng+equ', 'script/Latin+osd'])
def test_direct_ocr_config_cannot_bypass_reading_selection(languages):
    with pytest.raises(ValueError, match='reading'):
        Config(languages=languages)


@pytest.mark.parametrize('portable', [True, False])
@pytest.mark.parametrize('languages', ['equ', 'eng+osd'])
def test_runtime_reading_requirements_reject_special_codes(portable, languages):
    with pytest.raises(OcrRuntimeError, match='reading'):
        _runtime(portable=portable).require_languages(languages)


def test_valid_language_script_and_separate_orientation_remain_usable():
    runtime = _runtime()
    models = installed_languages(runtime)
    selected = language_selection(('eng', 'script/Latin'), models)
    assert selected == 'eng+script/Latin'
    config = Config(languages=selected, orientation=True)
    runtime.require_languages(config.languages, orientation=config.orientation)


def test_special_reading_preset_is_rejected_without_persisting_state(tmp_path):
    path = tmp_path / 'presets.json'
    with pytest.raises(ValueError, match='reading'):
        language_manager.save_preset(path, 'Invalid detector preset', ['equ'],
                                     available=installed_languages(_runtime()))
    assert not path.exists()
