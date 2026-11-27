"""Bounded, source-preserving results for the existing provider gateway.

This module does not send requests, read credentials or change a library's
original text. Basic v2 is deliberately a plain-text excerpt capability; it
does not advertise document layout, glossaries, Advanced v3 or every language.

Protocol references:
https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate
https://docs.cloud.google.com/docs/authentication/api-keys-use
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from .providers import (MAX_TRANSLATION_RESPONSE, ProviderError, ProviderProfile,
                        ProviderRequest, _LANGUAGE, _json)


@dataclass(frozen=True)
class TranslationResult:
    text: str
    detected_source_language: str | None
    reported_model: str | None


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('Duplicate response field.')
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError('Non-finite response value.')


def decode_google_translation(raw: bytes, request: ProviderRequest) -> TranslationResult:
    """Validate exactly one response for exactly one reviewed source excerpt.

    Provider text is retained literally. It is never evaluated as HTML or
    normalized into a source quotation. The raw request selects ``format=text``.
    """
    if not isinstance(request, ProviderRequest) or request.capability != 'translation':
        raise ProviderError('A typed translation request is required.')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_TRANSLATION_RESPONSE:
        raise ProviderError('Invalid or oversized translation response.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
        if type(value) is not dict or set(value) != {'data'}:
            raise ValueError
        data = value['data']
        if type(data) is not dict or set(data) != {'translations'}:
            raise ValueError
        translations = data['translations']
        if type(translations) is not list or len(translations) != 1:
            raise ValueError
        result = translations[0]
        if (type(result) is not dict or 'translatedText' not in result
                or set(result) - {'translatedText', 'detectedSourceLanguage', 'model'}):
            raise ValueError
        translated = result['translatedText']
        if (not isinstance(translated, str) or not translated.strip()
                or '\x00' in translated or len(translated.encode('utf-8')) > MAX_TRANSLATION_RESPONSE):
            raise ValueError
        detected = result.get('detectedSourceLanguage')
        if (detected is not None and (not isinstance(detected, str)
                or not _LANGUAGE.fullmatch(detected) or detected.lower() == 'auto')):
            raise ValueError
        if request.source_language is None and detected is None:
            raise ValueError
        if (request.source_language is not None and detected is not None
                and request.source_language.casefold() != detected.casefold()):
            raise ValueError
        reported_model = result.get('model')
        if reported_model is not None and reported_model != 'nmt':
            raise ValueError
        return TranslationResult(translated, detected, reported_model)
    except (ValueError, TypeError, KeyError, IndexError, RecursionError, UnicodeError):
        raise ProviderError('Unsupported translation response; no source text was replaced.') from None


def translation_document(profile: ProviderProfile, request: ProviderRequest, raw: bytes,
                         *, operation_id: str, estimated_micros: int) -> dict:
    """A portable derivative links exact source, result, model and request.

    The document intentionally includes the source selected by the user and is
    written only to the chosen local export. It contains no credential or key
    reference. This is a translation record, never an original-source citation.
    """
    if not isinstance(profile, ProviderProfile) or profile.provider != 'google-translate-v2':
        raise ProviderError('This translation result requires the Google Basic v2 profile.')
    result = decode_google_translation(raw, request)
    return {
        'format': 'resonance.translation',
        'version': 1,
        'kind': 'derived_translation',
        'source': {'text': request.text,
                   'sha256': sha256(request.text.encode('utf-8')).hexdigest(),
                   'language': request.source_language,
                   'detected_language': result.detected_source_language},
        'translation': {'text': result.text,
                        'sha256': sha256(result.text.encode('utf-8')).hexdigest(),
                        'language': request.target_language},
        'provider': {'adapter': profile.provider, 'endpoint': profile.endpoint,
                     'requested_model': profile.model, 'reported_model': result.reported_model,
                     'profile_sha256': profile.fingerprint},
        'operation': {'id': operation_id, 'request_sha256': request.fingerprint(profile),
                      'response_sha256': sha256(raw).hexdigest(),
                      'estimated_micros': estimated_micros, 'currency': 'USD',
                      'billed_cost': 'unknown'},
    }


def encode_translation_document(profile, request, raw, *, operation_id, estimated_micros):
    return _json(translation_document(profile, request, raw, operation_id=operation_id,
                                     estimated_micros=estimated_micros))
