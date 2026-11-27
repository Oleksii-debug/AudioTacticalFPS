"""Bounded client-side mapping for the explicit local LibreTranslate API.

Protocol reviewed at LibreTranslate v1.10.0, commit
29aea44cc80100401497fb62db813a3f3a504e84. No upstream engine code, model
or server process is embedded/launched. A response is a provider declaration,
not proof of installed model identity, offline egress or translation quality.
"""
import json
import math
import re

from .models import text

ADAPTER = 'libretranslate-local'
LANGUAGE = r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}'


def language(value, *, auto=False):
    if type(value) is not str or not (auto and value == 'auto' or re.fullmatch(LANGUAGE, value)):
        raise ValueError('Choose an explicit declared local translation language.')
    return value


def request(operation, payload):
    if operation == 'translation.languages':
        if set(payload) != {'target'}:
            raise ValueError('Local language discovery accepts only the display-language selection.')
        language(payload['target'])
        # The selected protocol returns its own display names; it has no
        # Google-style display-language query or cloud model selector.
        return '/languages', {}, 'application/json'
    if (operation != 'translation.text' or set(payload) - {'q', 'source', 'target'}
            or not {'q', 'target'} <= set(payload)):
        raise ValueError('Local translation accepts selected text and a source/target pair; no glossary fallback.')
    return '/translate', {
        'q': text(payload['q'], 'selected local translation text', maximum=5000),
        'source': language(payload.get('source', 'auto'), auto=True),
        'target': language(payload['target']), 'format': 'text',
    }, 'application/json'


def validate_languages(rows):
    if type(rows) is not list or not 1 <= len(rows) <= 1000:
        raise ValueError('The local language inventory is invalid.')
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != {'language', 'name', 'targets'}:
            raise ValueError('A local language declaration needs its exact target pairs.')
        code = language(row['language'])
        text(row['name'], 'local language display name', maximum=512, empty=True)
        targets = row['targets']
        if type(targets) is not list or len(targets) > 1000:
            raise ValueError('The local translation pair inventory is invalid.')
        for target in targets:
            language(target)
        if len(set(targets)) != len(targets):
            raise ValueError('Duplicate local translation target.')
        if code in seen:
            raise ValueError('Duplicate local translation language.')
        seen.add(code)
    if any(target not in seen for row in rows for target in row['targets']):
        raise ValueError('The local server declared a target missing from its language inventory.')
    return rows


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate local translation response field.')
        result[key] = value
    return result


def response(raw, operation, body, *, maximum):
    value = json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite local translation response.')))
    if operation == 'translation.languages':
        if type(value) is not list:
            raise ValueError('The local language response is invalid.')
        rows = []
        for item in value:
            if type(item) is not dict or set(item) != {'code', 'name', 'targets'}:
                raise ValueError('The local language response does not identify supported pairs.')
            rows.append({'language': item['code'], 'name': item['name'], 'targets': item['targets']})
        return {'languages': validate_languages(rows)}
    if (type(value) is not dict or set(value) - {'translatedText', 'detectedLanguage'}
            or 'translatedText' not in value):
        raise ValueError('The local translation response schema is invalid.')
    output = text(value['translatedText'], 'local translation output', maximum=maximum)
    # LibreTranslate has already unescaped its text response. Preserve literal
    # entity strings exactly; no second HTML decoding or markup execution.
    detected = value.get('detectedLanguage')
    if body['source'] == 'auto':
        if (type(detected) is not dict or set(detected) != {'language', 'confidence'}
                or type(detected['confidence']) not in (int, float)
                or not math.isfinite(detected['confidence'])
                or not 0 <= detected['confidence'] <= 100):
            raise ValueError('The local automatic translation omitted a valid detected-language receipt.')
        detected = language(detected['language'])
    elif detected is not None:
        raise ValueError('The local server returned unrequested language detection.')
    return {'text': output, 'detected_source': detected}


def require_pair(rows, source, target, *, detected=None):
    """A target list is directional; it is never an all-to-all promise."""
    validate_languages(rows)
    language(source, auto=True)
    language(target)
    directions = {row['language']: frozenset(row['targets']) for row in rows}
    if source == 'auto':
        if not any(code != target and target in targets for code, targets in directions.items()):
            raise ValueError('No installed source language declares this automatic target direction.')
        if detected is not None and (detected == target or target not in directions.get(detected, ())):
            raise ValueError('The detected source does not declare this selected target direction.')
    elif source == target or target not in directions.get(source, ()):
        raise ValueError('This local source-to-target direction is not declared supported.')
    return rows
