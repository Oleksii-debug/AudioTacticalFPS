"""Official script-path compatibility without permitting filesystem paths."""
import pytest

from carr_library.extract.ocr_languages import model_ids
from carr_library.extract.page_ocr import Config
from carr_library.extract.pinned_tessdata import POLICY


@pytest.mark.parametrize('selection', [
    'eng', 'chi_tra_vert', 'Latin', 'script/Latin', 'script/Cyrillic',
    'script/Japanese_vert', 'eng+script/Latin', 'script/Cyrillic+ukr+eng',
])
def test_configuration_preserves_root_and_script_model_identity(selection):
    assert Config(languages=selection).languages == selection
    assert model_ids(selection) == tuple(selection.split('+'))


@pytest.mark.parametrize('selection', [
    '', None, 1, True, ['eng'], '+eng', 'eng+', 'eng++ukr',
    '../eng', './eng', '/eng', 'C:/eng', 'C:\\eng',
    'script/', 'script/../eng', 'script/./Latin', 'script//Latin',
    'script/Latin/eng', 'other/Latin', 'Script/Latin', 'script\\Latin',
    'script/Latin.traineddata', 'eng --psm 1', 'eng\n', 'eng\x00',
    'eng\u200b', ' script/Latin', 'script/Latin ',
])
def test_configuration_rejects_paths_controls_options_and_empty_selections(selection):
    with pytest.raises(ValueError, match='language selection'):
        Config(languages=selection)


@pytest.mark.parametrize(('selection', 'message'), [
    pytest.param('script/Latin', 'unsupported pinned OCR language', id='script/Latin'),
    pytest.param('eng+script/Cyrillic', 'unsupported pinned OCR language', id='eng+script/Cyrillic'),
    pytest.param('fra', 'unsupported pinned OCR language', id='fra'),
    pytest.param('eng+eng', 'duplicate or oversized language selection', id='eng+eng'),
])
def test_script_support_does_not_expand_existing_pinned_policy(selection, message):
    with pytest.raises(ValueError, match=f'^{message}$'):
        Config(languages=selection, model_policy=POLICY)
    assert Config(languages='eng+ukr+slk', model_policy=POLICY).model_policy == POLICY
