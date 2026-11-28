"""Bounded language-specific alternatives over the existing OCR image and TSV.

Paragraph crops retain their original pixel origin and independent raw outputs.
They are never concatenated into a claimed source transcription. A coherent
whole-page second pass may replace the displayed OCR candidate; all baseline
bytes and the unverified review state remain explicit.
"""
from hashlib import sha256
import json
import subprocess
import time

from .ocr_languages import model_ids
from .language_detection import (Detector, DetectorUnavailable, POLICY, MODEL_SHA256,
    VERSION, MIN_CONFIDENCE, MIN_MARGIN, MIN_LETTERS, choose_model)

MAX_MODELS = 8
MAX_REGIONS = 12
MAX_REGION_WORDS = 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_BASELINE_BYTES = 1024 * 1024
MAX_SECOND_PASS_BYTES = 512 * 1024
MAX_REGIONAL_PIXELS = 20_000_000
MAX_EXTRA_SECONDS = 12.0


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8'))


def _regions(blocks):
    groups, order = {}, []
    for word in blocks:
        key = tuple(word['line'][:2])
        if key not in groups:
            if len(groups) >= MAX_REGIONS:
                return tuple(groups[key] for key in order), True
            groups[key] = {'words': [], 'bbox': None, 'source_paragraph': list(key),
                           'word_limit': False}
            order.append(key)
        group = groups[key]
        if len(group['words']) >= MAX_REGION_WORDS:
            group['word_limit'] = True
            continue
        group['words'].append(word)
        left, top, width, height = word['bbox']
        right, bottom = left + width, top + height
        if group['bbox'] is None:
            group['bbox'] = [left, top, right, bottom]
        else:
            box = group['bbox']
            group['bbox'] = [min(box[0], left), min(box[1], top),
                             max(box[2], right), max(box[3], bottom)]
    return tuple(groups[key] for key in order), False


def language_candidates(image, raw, tsv, config, remaining, recognize):
    """Return (observations, optional whole-page raw/TSV candidate).

    recognize remains the caller's one verified Tesseract runtime; this helper
    cannot launch a process, choose another executable, or discover new models.
    """
    from .page_ocr import _matching_ocr_blocks
    selected = model_ids(config.languages)
    result = {'policy': POLICY, 'package_version': VERSION,
              'model_sha256': MODEL_SHA256, 'status': 'not_run',
              'selected_model_pool': list(selected), 'max_regions': MAX_REGIONS,
              'minimum_letters': MIN_LETTERS, 'minimum_confidence': MIN_CONFIDENCE,
              'minimum_margin': MIN_MARGIN, 'regions': [],
              'notice': 'Model confidence is not OCR accuracy. Every alternative needs visual source review.'}
    if len(selected) > MAX_MODELS:
        result['status'] = 'model_pool_limit'
        return result, None
    if _bytes({'raw': raw, 'tsv': tsv}) > MAX_BASELINE_BYTES:
        result['status'] = 'baseline_evidence_budget'
        return result, None
    # Reserve time for the existing runtime identity check after optional work.
    available = remaining()
    if available <= 2.0:
        result['status'] = 'insufficient_remaining_time'
        return result, None
    deadline = time.monotonic() + min(MAX_EXTRA_SECONDS, max(0.1, available - 2.0))
    def extra_remaining():
        global_remaining = remaining()
        seconds = min(global_remaining, deadline - time.monotonic())
        if seconds <= 0:
            raise subprocess.TimeoutExpired('automatic OCR language review', MAX_EXTRA_SECONDS)
        return seconds
    def append_observation(observation):
        if _bytes({**result, 'regions': [*result['regions'], observation]}) > MAX_EVIDENCE_BYTES:
            result['status'] = 'regional_evidence_budget'
            return False
        result['regions'].append(observation)
        return True
    try:
        extra_remaining()
        blocks = _matching_ocr_blocks(tsv, image.size, raw)
        grouped, truncated = _regions(blocks)
        result['region_inventory_truncated'] = truncated
        detector = Detector(remaining=extra_remaining)
        result['document'] = detector.identify(raw)
        result['document_model'], result['document_selection'] = choose_model(result['document'], selected)
        accepted_languages = set()
        deferred = bool(truncated or not grouped)
        total_pixels = 0
        for index, group in enumerate(grouped, 1):
            extra_remaining()
            words, box = group['words'], group['bbox']
            text = ' '.join(word['text'] for word in words)
            observation = {'source_paragraph': group['source_paragraph'],
                           'bbox': [box[0], box[1], box[2] - box[0], box[3] - box[1]], 'coordinate_frame': 'source_render_pixels_top_left',
                           'first_pass_text': text, 'first_pass_text_sha256': sha256(text.encode('utf-8')).hexdigest(),
                           'status': 'not_run', 'word_inventory_truncated': group['word_limit']}
            decision = detector.identify(text)
            observation['detection'] = decision
            model, selection = choose_model(decision, selected)
            observation.update(selected_model=model, selection=selection)
            mean_confidence = sum(max(0, word['confidence']) for word in words) / max(1, len(words))
            observation['mean_first_pass_ocr_confidence'] = mean_confidence
            # Weak OCR is not evidence of another language. Keep the explicit
            # bootstrap candidate rather than forcing a model from garbled text.
            if group['word_limit'] or model is None or mean_confidence < 40:
                observation['status'] = ('first_pass_word_limit' if group['word_limit'] else
                    'weak_first_pass_ocr' if mean_confidence < 40 else selection)
                deferred = True
                if not append_observation(observation):
                    return result, None
                continue
            accepted_languages.add(decision['language'])
            # Include a small verified in-image margin for ascenders/descenders.
            left, top = max(0, box[0] - 4), max(0, box[1] - 4)
            right, bottom = min(image.width, box[2] + 4), min(image.height, box[3] + 4)
            pixels = (right - left) * (bottom - top)
            if total_pixels + pixels > min(MAX_REGIONAL_PIXELS, config.max_pixels):
                observation['status'] = 'regional_pixel_budget'
                deferred = True
                if not append_observation(observation):
                    return result, None
                continue
            total_pixels += pixels
            with image.crop((left, top, right, bottom)) as crop:
                observation.update(crop_bbox=[left, top, right - left, bottom - top],
                    crop_rgb_sha256=sha256(crop.tobytes()).hexdigest(),
                    crop_coordinate_frame='crop_pixels_top_left; add crop_bbox left/top for source pixels')
                candidate_raw, candidate_tsv = recognize(crop, f'language-region-{index}',
                    languages=model, psm=6, maximum=MAX_SECOND_PASS_BYTES, budget=extra_remaining)
                # Validate all boxes before presenting any regional coordinates.
                candidate_blocks = _matching_ocr_blocks(candidate_tsv, crop.size, candidate_raw)
                observation.update(status='ocr_candidate', raw_text=candidate_raw,
                                   raw_tsv=candidate_tsv, blocks=candidate_blocks)
            if not append_observation(observation):
                return result, None
        result['regional_pixels'] = total_pixels
        result['status'] = 'mixed_or_partial_region_candidates' if deferred or len(accepted_languages) != 1 else 'region_candidates'
        # Only a consistent, complete paragraph inventory can choose a new
        # coherent full-page candidate. Mixed regions remain independent evidence.
        document = result['document']
        chosen = result['document_model']
        if (not deferred and chosen is not None and len(accepted_languages) == 1
                and document['language'] in accepted_languages
                and config.languages != chosen):
            candidate_raw, candidate_tsv = recognize(image, 'language-page',
                languages=chosen, psm=3, maximum=MAX_SECOND_PASS_BYTES, budget=extra_remaining)
            _matching_ocr_blocks(candidate_tsv, image.size, candidate_raw)
            extra_remaining()
            if _bytes({**result, 'first_pass_raw_text': raw, 'first_pass_raw_tsv': tsv,
                       'candidate_raw_text': candidate_raw, 'candidate_raw_tsv': candidate_tsv}) > MAX_EVIDENCE_BYTES:
                result['status'] = 'whole_page_evidence_budget'
                return result, None
            result.update(status='single_language_page_candidate',
                          first_pass_raw_text=raw, first_pass_raw_tsv=tsv,
                          chosen_page_model=chosen)
            return result, (candidate_raw, candidate_tsv)
        return result, None
    except InterruptedError:
        raise
    except DetectorUnavailable as exc:
        result.update(status='detector_unavailable', detail=str(exc)[:500])
    except (ImportError, OSError, RuntimeError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        result.update(status='language_review_failed_or_timed_out', detail=str(exc)[:500])
    return result, None
