"""Bounded inert records for the optional held-session Word bridge.

These records describe a preview or a completed observation. They never grant
source access, open a document, select a COM object, or authorize a later run.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from xml.etree import ElementTree as ET

NAMESPACE = 'urn:resonance:word-citations:1'
LEGACY_NAMESPACE = 'urn:carr:citations:1'
SCHEMA = 'resonance-word-citations/1'
MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 2_000_000
MAX_CONTROLS = 512
MAX_CLUSTERS = 128
MAX_REFERENCES = 1000
TAG_CITATION = 'resonance:cite:'
TAG_BIBLIOGRAPHY = 'resonance:biblio:'
STYLES = frozenset(('bold', 'italic', 'underline', 'superscript', 'subscript', 'small_caps'))
INTERCHANGE_KEYS = frozenset((
    'schema', 'references', 'clusters', 'locale', 'style', 'processor',
    'attribution', 'bibliography_parameters', 'bibliography', 'citations', 'warnings',
))


def require(value, message):
    if not value:
        raise ValueError(message)


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), 'Invalid ' + label + ' fields.')
    return value


def text(value, label, maximum=4096, *, empty=False, multiline=False):
    require(type(value) is str and len(value) <= maximum and (empty or bool(value.strip())),
            'Invalid ' + label + '.')
    require(not any(ord(c) < 32 and c not in ('\r\n\t\v' if multiline else '') for c in value),
            'Invalid control character in ' + label + '.')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError('Invalid Unicode in ' + label + '.') from error
    return value


def raw_text(value, label, maximum=MAX_TEXT):
    require(type(value) is str and len(value) <= maximum and '\0' not in value,
            'Invalid ' + label + '.')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError('Invalid Unicode in ' + label + '.') from error
    return value


def integer(value, label, low=0, high=2**63 - 1):
    require(type(value) is int and low <= value <= high, 'Invalid ' + label + '.')
    return value


def identity(value, label='Word identity'):
    require(type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None,
            'Invalid ' + label + '.')
    return value


def digest(value, label='Word digest'):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None,
            'Invalid ' + label + '.')
    return value


def canonical(value):
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode('ascii')
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError('The Word record is not bounded plain JSON.') from error
    require(len(raw) <= MAX_BYTES, 'The Word record exceeds its byte allowance.')
    return raw


def fingerprint(value):
    return sha256(canonical(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate Word metadata key.')
        result[key] = value
    return result


def parse_json(raw):
    require(type(raw) is bytes and len(raw) <= MAX_BYTES, 'Oversized Word record.')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite Word value.')))
        canonical(result)
        return result
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError('The Word record is not strict bounded JSON.') from error


def utf16_length(value):
    return len(value.encode('utf-16-le')) // 2


def citation_tag(identifier):
    return TAG_CITATION + identity(identifier, 'citation ID')


def bibliography_tag(identifier):
    return TAG_BIBLIOGRAPHY + identity(identifier, 'document ID')


def owned_tag(value):
    return type(value) is str and value.startswith((TAG_CITATION, TAG_BIBLIOGRAPHY))


def validate_interchange(value):
    exact(value, INTERCHANGE_KEYS, 'CSL interchange')
    require(type(value['schema']) is int and value['schema'] == 1, 'Unsupported CSL interchange.')
    references = value['references']
    require(type(references) is list and 1 <= len(references) <= MAX_REFERENCES,
            'Choose a bounded nonempty reference selection.')
    ids = []
    for row in references:
        require(type(row) is dict, 'Invalid stored reference.')
        ids.append(text(row.get('id'), 'reference ID', 256))
    require(len(set(ids)) == len(ids), 'Duplicate stored reference IDs.')
    clusters = value['clusters']
    require(type(clusters) is list and len(clusters) <= MAX_CLUSTERS, 'Too many Word citations.')
    cluster_ids = []
    for cluster in clusters:
        exact(cluster, ('id', 'items', 'note_index', 'locator_evidence'), 'citation cluster')
        cluster_ids.append(identity(cluster['id'], 'citation ID'))
        items = cluster['items']
        evidence = cluster['locator_evidence']
        require(type(items) is list and 1 <= len(items) <= 100
                and type(evidence) is list and len(items) == len(evidence), 'Invalid citation items.')
        for item, proof in zip(items, evidence):
            require(type(item) is dict and 'id' in item
                    and set(item) <= {'id', 'locator', 'label', 'prefix', 'suffix', 'suppress-author'}
                    and item['id'] in ids, 'Citation names an unavailable reference.')
            exact(proof, ('reference_id', 'locator_verified'), 'locator evidence')
            require(proof['reference_id'] == item['id'], 'Locator reference differs.')
            if 'locator' in item:
                text(item['locator'], 'verified locator', 200)
                text(item.get('label'), 'locator label', 40)
                require(proof['locator_verified'] is True, 'A pinpoint needs explicit source verification.')
            else:
                require(proof['locator_verified'] is None, 'Unexpected locator evidence.')
            for key in ('prefix', 'suffix'):
                if key in item:
                    text(item[key], key, 1000, empty=True)
            if 'suppress-author' in item:
                require(type(item['suppress-author']) is bool, 'Invalid author suppression flag.')
        if cluster['note_index'] is not None:
            integer(cluster['note_index'], 'note index', 1, 1_000_000)
    require(len(set(cluster_ids)) == len(cluster_ids), 'Duplicate Word citation IDs.')
    style = value['style']
    exact(style, ('id', 'title', 'kind', 'sha256', 'source', 'license'), 'CSL style')
    for key in ('id', 'title', 'source', 'license'):
        text(style[key], 'CSL style ' + key, 4096)
    require(style['kind'] in ('note', 'in-text'), 'Unsupported citation style family.')
    digest(style['sha256'], 'CSL style digest')
    text(value['locale'], 'citation locale', 64)
    require(type(value['processor']) is dict, 'Invalid processor identity.')
    text(value['attribution'], 'processor attribution', 16_000, multiline=True)
    require(type(value['bibliography_parameters']) is dict, 'Invalid bibliography parameters.')
    require(type(value['warnings']) is list and len(value['warnings']) <= 1000, 'Invalid CSL warnings.')
    for warning in value['warnings']:
        text(warning, 'CSL warning', 4000, empty=True, multiline=True)
    require(type(value['citations']) is list and len(value['citations']) == len(clusters),
            'Rendered citation count differs.')
    for cluster, rendered in zip(clusters, value['citations']):
        exact(rendered, ('cluster_id', 'reference_ids', 'text', 'html', 'note_index'), 'rendered citation')
        require(rendered['note_index'] is None or type(rendered['note_index']) is int,
                'Rendered note index has an invalid type.')
        require(rendered['cluster_id'] == cluster['id']
                and rendered['reference_ids'] == [item['id'] for item in cluster['items']]
                and rendered['note_index'] == cluster['note_index'], 'Rendered citation identity differs.')
        text(rendered['text'], 'rendered citation', 200_000, empty=True, multiline=True)
        text(rendered['html'], 'rendered markup', 400_000, empty=True, multiline=True)
    require(type(value['bibliography']) is list and len(value['bibliography']) <= MAX_REFERENCES,
            'Invalid rendered bibliography.')
    for item in value['bibliography']:
        exact(item, ('reference_ids', 'text', 'html'), 'bibliography entry')
        require(type(item['reference_ids']) is list and item['reference_ids']
                and all(identifier in ids for identifier in item['reference_ids']), 'Unknown bibliography reference.')
        text(item['text'], 'bibliography text', 200_000, empty=True, multiline=True)
        text(item['html'], 'bibliography markup', 400_000, empty=True, multiline=True)
    canonical(value)
    return value


def validate_binding(value, tag):
    exact(value, ('tag', 'native_id', 'text_sha256', 'story'), 'managed Word binding')
    require(value['tag'] == tag, 'Managed Word tag differs.')
    text(value['native_id'], 'native content control ID', 128)
    digest(value['text_sha256'], 'managed text digest')
    require(type(value['story']) is int and value['story'] in (1, 3), 'Unsupported Word story.')
    return value


def validate_metadata(value):
    exact(value, ('schema', 'document_id', 'revision', 'interchange', 'bindings',
                  'bibliography', 'source_scope', 'last_request'), 'Word citation metadata')
    require(value['schema'] == SCHEMA, 'Unsupported Word metadata schema.')
    document_id = identity(value['document_id'], 'document ID')
    integer(value['revision'], 'Word metadata revision', 1, 1_000_000)
    identity(value['last_request'], 'last request ID')
    data = validate_interchange(value['interchange'])
    bindings = value['bindings']
    require(type(bindings) is list and len(bindings) == len(data['clusters']),
            'Managed citation bindings are incomplete.')
    native_ids = []
    for cluster, binding in zip(data['clusters'], bindings):
        validate_binding(binding, citation_tag(cluster['id']))
        native_ids.append(binding['native_id'])
        require(binding['story'] == (3 if data['style']['kind'] == 'note' else 1),
                'Stored citation story differs from its style family.')
    if value['bibliography'] is not None:
        validate_binding(value['bibliography'], bibliography_tag(document_id))
        require(value['bibliography']['story'] == 1, 'Bibliography must be in the main story.')
        native_ids.append(value['bibliography']['native_id'])
    require(len(set(native_ids)) == len(native_ids), 'A native content control is bound more than once.')
    scope = exact(value['source_scope'], ('workspace', 'snapshot_sha256', 'name_mode'),
                  'non-authorizing source description')
    text(scope['workspace'], 'source workspace', 256)
    digest(scope['snapshot_sha256'], 'source snapshot digest')
    require(scope['name_mode'] in ('literal', 'family-given'), 'Unsupported author convention.')
    canonical(value)
    return value


def metadata_xml(value):
    raw = canonical(validate_metadata(value))
    node = ET.Element('{' + NAMESPACE + '}citations',
                      {'schema': '1', 'sha256': sha256(raw).hexdigest()})
    node.text = raw.decode('ascii')
    return ET.tostring(node, encoding='unicode')


def parse_metadata_xml(value):
    text(value, 'Word metadata XML', MAX_BYTES, multiline=True)
    raw = value.encode('utf-8')
    require(len(raw) <= MAX_BYTES and b'<!DOCTYPE' not in raw.upper()
            and b'<!ENTITY' not in raw.upper(), 'Unsafe or oversized Word metadata XML.')
    try:
        node = ET.fromstring(value)
    except (ET.ParseError, RecursionError) as error:
        raise ValueError('Invalid Word metadata XML.') from error
    require(node.tag == '{' + NAMESPACE + '}citations' and not list(node)
            and set(node.attrib) == {'schema', 'sha256'} and node.get('schema') == '1',
            'Unsupported Word metadata element.')
    payload = (node.text or '').encode('utf-8')
    require(sha256(payload).hexdigest() == node.get('sha256'), 'Word metadata hash differs.')
    return validate_metadata(parse_json(payload))


def validate_capture(value, session=None):
    exact(value, ('schema', 'session', 'document', 'selection', 'controls', 'fields',
                  'endnotes', 'metadata', 'metadata_xml_sha256', 'legacy_parts'),
          'Word capture')
    require(value['schema'] == 'resonance-word-capture/1', 'Unsupported Word capture.')
    identity(value['session'], 'held session')
    if session is not None:
        require(value['session'] == session, 'Word capture belongs to another held session.')
    document = exact(value['document'], ('hwnd', 'pid', 'creation_time', 'word_version', 'name', 'full_name',
                     'text', 'outside_main_sha256', 'readonly', 'protection', 'track_revisions', 'undo_level'), 'Word document')
    for key in ('hwnd', 'pid', 'creation_time'):
        integer(document[key], key, 1)
    text(document['word_version'], 'Word application version', 64)
    text(document['name'], 'Word document name', 1024)
    text(document['full_name'], 'Word document identity', 8192)
    raw_text(document['text'], 'Word main-story text')
    digest(document['outside_main_sha256'], 'unmanaged main-story digest')
    for key in ('readonly', 'track_revisions'):
        require(type(document[key]) is bool, 'Invalid Word capability flag.')
    integer(document['protection'], 'Word protection', -1, 100)
    integer(document['undo_level'], 'Word undo level', 0, 100)
    selection = exact(value['selection'], ('start', 'end', 'story'), 'Word selection')
    integer(selection['start'], 'selection start', 0, MAX_TEXT)
    integer(selection['end'], 'selection end', selection['start'], MAX_TEXT)
    integer(selection['story'], 'selection story', 1, 20)
    require(type(value['controls']) is list and len(value['controls']) <= MAX_CONTROLS,
            'Too many Word content controls.')
    native_ids, owned = set(), {}
    for control in value['controls']:
        exact(control, ('native_id', 'tag', 'story', 'start', 'end', 'anchor',
                        'text', 'locked', 'note_index'), 'Word control')
        native_id = text(control['native_id'], 'native control ID', 128)
        require(native_id not in native_ids, 'Duplicate native content control ID.')
        native_ids.add(native_id)
        text(control['tag'], 'Word control tag', 256, empty=True)
        integer(control['story'], 'control story', 1, 20)
        integer(control['start'], 'control start', 0, MAX_TEXT)
        integer(control['end'], 'control end', control['start'], MAX_TEXT)
        integer(control['anchor'], 'control main-story anchor', 0, MAX_TEXT)
        raw_text(control['text'], 'control text')
        require(type(control['locked']) is bool, 'Invalid Word control lock.')
        if control['note_index'] is not None:
            integer(control['note_index'], 'control note index', 1, 1000)
        if owned_tag(control['tag']):
            require(control['tag'] not in owned, 'Duplicate managed Word tag.')
            owned[control['tag']] = control
    require(type(value['fields']) is list and len(value['fields']) <= 4096, 'Too many Word fields.')
    for field in value['fields']:
        exact(field, ('start', 'end', 'story', 'code_sha256', 'result_sha256'), 'Word field')
        integer(field['start'], 'field start', 0, MAX_TEXT)
        integer(field['end'], 'field end', field['start'], MAX_TEXT)
        integer(field['story'], 'field story', 1, 20)
        digest(field['code_sha256']); digest(field['result_sha256'])
    require(type(value['endnotes']) is list and len(value['endnotes']) <= 1000, 'Too many Word endnotes.')
    for index, note in enumerate(value['endnotes'], 1):
        exact(note, ('index', 'anchor', 'start', 'end'), 'Word endnote')
        require(type(note['index']) is int and note['index'] == index, 'Endnote order differs.')
        for key in ('anchor', 'start', 'end'):
            integer(note[key], 'endnote range', 0, MAX_TEXT)
        require(note['start'] <= note['end'], 'Invalid endnote range.')
    require(type(value['legacy_parts']) is list and len(value['legacy_parts']) <= 8,
            'Too many legacy metadata parts.')
    for legacy in value['legacy_parts']:
        digest(legacy, 'legacy metadata digest')
    if value['metadata'] is None:
        require(value['metadata_xml_sha256'] is None and not owned,
                'Unbound managed Word controls require manual review.')
    else:
        metadata = validate_metadata(value['metadata'])
        digest(value['metadata_xml_sha256'], 'metadata XML digest')
        bindings = [*metadata['bindings']]
        if metadata['bibliography'] is not None:
            bindings.append(metadata['bibliography'])
        require(set(owned) == {binding['tag'] for binding in bindings},
                'Word controls and stored citation metadata disagree.')
        for binding in bindings:
            control = owned[binding['tag']]
            require(control['native_id'] == binding['native_id']
                    and control['story'] == binding['story']
                    and sha256(control['text'].encode('utf-8')).hexdigest() == binding['text_sha256'],
                    'A managed Word range was edited; review it in Word before continuing.')
    canonical(value)
    return value


def mutation_allowed(capture, *, insertion):
    document = capture['document']
    require(not document['readonly'] and document['protection'] == -1
            and not document['track_revisions'] and document['undo_level'] == 0,
            'Word must be writable, unprotected, outside Track Changes and outside another custom undo operation.')
    managed = [control for control in capture['controls'] if owned_tag(control['tag'])]
    require(not any(control['locked'] for control in managed), 'A managed Word citation is locked.')
    for target in managed:
        for other in capture['controls']:
            if other['native_id'] != target['native_id'] and other['story'] == target['story']:
                require(target['end'] <= other['start'] or other['end'] <= target['start'],
                        'A managed Word range overlaps another content control.')
        for field in capture['fields']:
            if field['story'] == target['story']:
                require(target['end'] <= field['start'] or field['end'] <= target['start'],
                        'A managed Word range contains or overlaps a foreign field.')
    if insertion:
        selection = capture['selection']
        require(selection['story'] == 1 and selection['start'] == selection['end'],
                'Place a collapsed insertion point in the main Word document, then inspect again.')
        point = selection['start']
        require(not any(row['story'] == 1 and row['start'] <= point <= row['end']
                        for row in (*capture['controls'], *capture['fields'])),
                'Choose an insertion point outside existing content controls and fields.')


def validate_runs(value, expected_text):
    require(type(value) is list and len(value) <= 8192, 'Too many citation typography runs.')
    for item in value:
        exact(item, ('text', 'styles'), 'typography run')
        text(item['text'], 'typography text', MAX_TEXT, empty=True, multiline=True)
        require(type(item['styles']) is list and all(type(style) is str for style in item['styles'])
                and len(set(item['styles'])) == len(item['styles']), 'Invalid citation typography styles.')
        require(all(style in STYLES for style in item['styles']), 'Unsupported citation typography.')
        require(not {'superscript', 'subscript'} <= set(item['styles']), 'Conflicting citation typography.')
    require(''.join(item['text'] for item in value) == expected_text, 'Typography changes the reviewed text.')


def validate_proposal(value, capture):
    exact(value, ('schema', 'session', 'capture_sha256', 'action', 'source_scope',
                  'document_id', 'revision', 'interchange', 'operations'), 'Word proposal')
    require(value['schema'] == 'resonance-word-proposal/1'
            and value['session'] == capture['session']
            and value['capture_sha256'] == fingerprint(capture), 'Word preview does not bind its exact capture.')
    require(value['action'] in ('insert_citation', 'edit_citation', 'regenerate_bibliography'),
            'Unknown Word action.')
    identity(value['document_id'], 'document ID')
    integer(value['revision'], 'Word revision', 1, 1_000_000)
    previous = capture['metadata']
    require(previous is None or (value['document_id'] == previous['document_id']
            and value['revision'] == previous['revision'] + 1), 'Word metadata generation changed.')
    data = validate_interchange(value['interchange'])
    if previous is not None and previous['bindings']:
        require(data['style']['kind'] == previous['interchange']['style']['kind'],
                'Switching between in-text and endnote families needs a separate explicit document workflow.')
    scope = exact(value['source_scope'], ('workspace', 'snapshot_sha256', 'name_mode'), 'source scope')
    text(scope['workspace'], 'source workspace', 256); digest(scope['snapshot_sha256'])
    require(scope['name_mode'] in ('literal', 'family-given'), 'Unsupported name convention.')
    operations = value['operations']
    require(type(operations) is list and 1 <= len(operations) <= MAX_CLUSTERS + 1,
            'Invalid Word operation count.')
    existing = {row['tag']: row for row in capture['controls'] if owned_tag(row['tag'])}
    tags, insertions = [], 0
    for operation in operations:
        exact(operation, ('tag', 'native_id', 'text', 'runs', 'note', 'note_index'), 'Word operation')
        tag = operation['tag']
        require(tag == bibliography_tag(value['document_id']) or
                tag in {citation_tag(cluster['id']) for cluster in data['clusters']}, 'Unmanaged Word target.')
        require(type(operation['note']) is bool, 'Invalid Word note flag.')
        require(operation['note'] is (data['style']['kind'] == 'note' and tag.startswith(TAG_CITATION)),
                'Word target story differs from its style.')
        text(operation['text'], 'Word replacement', MAX_TEXT, multiline=True)
        validate_runs(operation['runs'], operation['text'])
        if tag.startswith(TAG_CITATION):
            rendered = next(item for item in data['citations']
                            if citation_tag(item['cluster_id']) == tag)
            expected_text = rendered['text'].replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r')
            require(operation['note_index'] == rendered['note_index'], 'Citation note number differs.')
        else:
            expected_text = '\r\r'.join(item['text'].replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r')
                                         for item in data['bibliography'])
        require(operation['text'] == expected_text, 'Word operation differs from the complete CSL preview.')
        if tag in existing:
            require(operation['native_id'] == existing[tag]['native_id'], 'Native Word target changed.')
        else:
            require(operation['native_id'] is None, 'New Word target has an invented native ID.')
            insertions += 1
        if operation['note']:
            integer(operation['note_index'], 'rendered note index', 1, 1000)
        else:
            require(operation['note_index'] is None, 'Unexpected note index.')
        tags.append(tag)
    require(len(set(tags)) == len(tags) and set(existing) <= set(tags),
            'Word preview omits, duplicates or discards an existing managed target.')
    require(insertions <= 1, 'Review one new Word range per action.')
    new_tags = set(tags) - set(existing)
    if value['action'] == 'insert_citation':
        require(insertions == 1 and next(iter(new_tags)).startswith(TAG_CITATION),
                'Insert must add exactly one reviewed citation.')
    elif value['action'] == 'edit_citation':
        require(insertions == 0 and previous is not None and previous['bindings'],
                'Edit needs an existing managed citation.')
    else:
        require(not new_tags or new_tags == {bibliography_tag(value['document_id'])},
                'Regeneration may add only the reviewed bibliography.')
    required_citations = {citation_tag(cluster['id']) for cluster in data['clusters']}
    require(required_citations == {tag for tag in tags if tag.startswith(TAG_CITATION)},
            'The complete citation sequence must be updated together.')
    mutation_allowed(capture, insertion=bool(insertions))
    canonical(value)
    return value
