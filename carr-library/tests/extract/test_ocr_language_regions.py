"""Actual paragraph crops and independent second-pass evidence; synthetic OCR callback."""
from copy import deepcopy
from hashlib import sha256

import pytest
from PIL import Image

from carr_library.extract.page_ocr import Config, _TSV_FIELDS
from carr_library.extract import ocr_regions as regions
from carr_library.extract.language_detection import MIN_CONFIDENCE


EN = 'The research library keeps the original document and exact wording for every source quotation.'
UK = 'Дослідницька бібліотека зберігає оригінальний документ і точні слова для кожної цитати.'


def layout(texts, width=1000):
    rows = ['\t'.join(_TSV_FIELDS)]
    for paragraph, text in enumerate(texts, 1):
        rows.append('\t'.join(map(str, [5, 1, paragraph, 1, 1, 1,
            20, 20 + (paragraph - 1) * 130, width - 40, 70, 95, text])))
    return '\n'.join(rows) + '\n'


def fixture_recognize(calls, *, fail_at=None):
    def recognize(image, name, *, languages, psm, maximum, budget):
        budget()
        calls.append({'name': name, 'languages': languages, 'psm': psm,
                      'size': image.size, 'sha256': sha256(image.tobytes()).hexdigest()})
        if fail_at is not None and len(calls) == fail_at:
            raise OSError('synthetic later OCR failure')
        text = 'Second pass preserves an independent regional candidate.'
        rows = ['\t'.join(_TSV_FIELDS), '\t'.join(map(str,
            [5, 1, 1, 1, 1, 1, 1, 1, image.width - 2, image.height - 2, 96, text]))]
        return text, '\n'.join(rows) + '\n'
    return recognize


def test_real_detector_mixed_regions_keep_original_offsets_and_independent_raw_outputs():
    with Image.new('RGB', (1000, 400), 'white') as image:
        calls = []
        raw, tsv = EN + '\n\n' + UK, layout([EN, UK])
        observations, selected = regions.language_candidates(image, raw, tsv,
            Config(languages='eng+ukr', automatic_languages=True), lambda: 30,
            fixture_recognize(calls))
    assert selected is None  # No invented concatenation or exact mixed-page ordering.
    assert [row['detection']['language'] for row in observations['regions']] == ['en', 'uk']
    assert [call['languages'] for call in calls] == ['eng', 'ukr']
    assert all(call['psm'] == 6 for call in calls)
    assert observations['status'] == 'mixed_or_partial_region_candidates'
    assert observations['regions'][0]['bbox'] == [20, 20, 960, 70]
    assert observations['regions'][1]['crop_bbox'] == [16, 146, 968, 78]
    assert all(row['status'] == 'ocr_candidate' and row['raw_tsv'] and row['blocks']
               for row in observations['regions'])


def test_single_language_page_candidate_preserves_exact_baseline_text_and_tsv():
    with Image.new('RGB', (1000, 200), 'white') as image:
        calls, raw, tsv = [], EN, layout([EN])
        observations, selected = regions.language_candidates(image, raw, tsv,
            Config(languages='eng+ukr', automatic_languages=True), lambda: 30,
            fixture_recognize(calls))
    assert selected is not None
    assert observations['status'] == 'single_language_page_candidate'
    assert observations['first_pass_raw_text'] == raw and observations['first_pass_raw_tsv'] == tsv
    assert observations['chosen_page_model'] == 'eng'
    assert [call['psm'] for call in calls] == [6, 3]


def test_outside_selected_pool_never_substitutes_an_installed_but_wrong_language():
    with Image.new('RGB', (1000, 200), 'white') as image:
        calls = []
        observations, selected = regions.language_candidates(image, UK, layout([UK]),
            Config(languages='eng', automatic_languages=True), lambda: 30,
            fixture_recognize(calls))
    assert not calls and selected is None
    assert observations['regions'][0]['detection']['language'] == 'uk'
    assert observations['regions'][0]['selection'] == 'outside_selected_model_pool'


def test_failed_second_pass_retains_completed_evidence_without_changing_baseline():
    with Image.new('RGB', (1000, 400), 'white') as image:
        calls, raw, tsv = [], EN + '\n' + UK, layout([EN, UK])
        observations, selected = regions.language_candidates(image, raw, tsv,
            Config(languages='eng+ukr', automatic_languages=True), lambda: 30,
            fixture_recognize(calls, fail_at=2))
    assert selected is None and len(calls) == 2
    assert len(observations['regions']) == 1 and observations['regions'][0]['raw_text']
    assert observations['status'] == 'language_review_failed_or_timed_out'
    assert raw == EN + '\n' + UK and tsv == layout([EN, UK])


def test_empty_weak_and_over_budget_regions_keep_review_candidate(monkeypatch):
    monkeypatch.setattr(regions, 'MAX_REGIONAL_PIXELS', 1)
    with Image.new('RGB', (1000, 200), 'white') as image:
        calls = []
        observations, selected = regions.language_candidates(image, EN, layout([EN]),
            Config(languages='eng', automatic_languages=True), lambda: 30,
            fixture_recognize(calls))
    assert selected is None and not calls
    assert observations['regions'][0]['status'] == 'regional_pixel_budget'


def test_remaining_time_is_shared_and_never_extended_for_extra_passes():
    with Image.new('RGB', (1000, 200), 'white') as image:
        calls = []
        observations, selected = regions.language_candidates(image, EN, layout([EN]),
            Config(automatic_languages=True), lambda: 1,
            fixture_recognize(calls))
    assert observations['status'] == 'insufficient_remaining_time'
    assert not calls and selected is None


def test_bad_layout_and_unavailable_detector_do_not_create_alternative(monkeypatch):
    with Image.new('RGB', (1000, 200), 'white') as image:
        calls = []
        observations, selected = regions.language_candidates(image, EN, 'not-tsv',
            Config(automatic_languages=True), lambda: 30, fixture_recognize(calls))
        assert observations['status'] == 'language_review_failed_or_timed_out'
        def missing(**kwargs):
            raise regions.DetectorUnavailable('reviewed detector absent')
        monkeypatch.setattr(regions, 'Detector', missing)
        observations, selected = regions.language_candidates(image, EN, layout([EN]),
            Config(automatic_languages=True), lambda: 30, fixture_recognize(calls))
    assert observations['status'] == 'detector_unavailable'
    assert selected is None and not calls

def test_cancellation_is_not_reclassified_as_an_optional_detector_fallback():
    with Image.new('RGB', (1000, 200), 'white') as image:
        def stopped(image, name, **kwargs):
            raise InterruptedError('cancelled during region review')
        with pytest.raises(InterruptedError, match='cancelled'):
            regions.language_candidates(image, EN, layout([EN]),
                Config(automatic_languages=True), lambda: 30, stopped)


def test_deferred_region_evidence_still_obeys_the_shared_byte_bound(monkeypatch):
    monkeypatch.setattr(regions, 'MAX_EVIDENCE_BYTES', 100)
    with Image.new('RGB', (1000, 200), 'white') as image:
        calls = []
        result, candidate = regions.language_candidates(image, UK, layout([UK]),
            Config(automatic_languages=True), lambda: 30, fixture_recognize(calls))
    assert result['status'] == 'regional_evidence_budget'
    assert not calls and not result['regions'] and candidate is None


def test_disagreeing_baseline_layout_cannot_choose_regions_or_initialize_detector(monkeypatch):
    raw, tsv = EN, layout([UK])
    calls = []

    def forbidden_detector(**kwargs):
        pytest.fail('A conflicting baseline cannot reach language detection.')

    monkeypatch.setattr(regions, 'Detector', forbidden_detector)
    with Image.new('RGB', (1000, 400), 'white') as image:
        original = sha256(image.tobytes()).hexdigest()
        observations, selected = regions.language_candidates(image, raw, tsv,
            Config(languages='eng+ukr', automatic_languages=True), lambda: 30,
            fixture_recognize(calls))
        assert sha256(image.tobytes()).hexdigest() == original

    assert selected is None and calls == []
    assert observations['status'] == 'language_review_failed_or_timed_out'
    assert 'OCR layout and raw text disagree' in observations['detail']
    assert observations['regions'] == []
    assert raw == EN and tsv == layout([UK])


@pytest.mark.parametrize('phase', ['region', 'page'])
def test_conflicting_alternative_layout_cannot_replace_baseline_or_expose_word_boxes(phase):
    raw, tsv, calls = EN, layout([EN]), []
    recognize = fixture_recognize(calls)

    def conflicting(image, name, **kwargs):
        actual_raw, actual_tsv = recognize(image, name, **kwargs)
        if ((phase == 'region' and name.startswith('language-region-'))
                or (phase == 'page' and name == 'language-page')):
            return actual_raw + ' changed', actual_tsv
        return actual_raw, actual_tsv

    with Image.new('RGB', (1000, 200), 'white') as image:
        original = sha256(image.tobytes()).hexdigest()
        observations, selected = regions.language_candidates(image, raw, tsv,
            Config(languages='eng+ukr', automatic_languages=True), lambda: 30, conflicting)
        assert sha256(image.tobytes()).hexdigest() == original

    assert selected is None
    assert observations['status'] == 'language_review_failed_or_timed_out'
    assert 'OCR layout and raw text disagree' in observations['detail']
    assert raw == EN and tsv == layout([EN])
    assert 'chosen_page_model' not in observations
    if phase == 'region':
        assert [call['name'] for call in calls] == ['language-region-1']
        assert observations['regions'] == []
    else:
        assert [call['name'] for call in calls] == ['language-region-1', 'language-page']
        assert len(observations['regions']) == 1
        accepted = observations['regions'][0]
        assert accepted['status'] == 'ocr_candidate'
        assert accepted['coordinate_frame'] == 'source_render_pixels_top_left'
        assert accepted['crop_coordinate_frame'].startswith('crop_pixels_top_left;')
        assert ''.join(accepted['raw_text'].split()) == ''.join(
            part for word in accepted['blocks'] for part in word['text'].split())
