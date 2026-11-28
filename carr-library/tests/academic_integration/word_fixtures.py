"""Synthetic Word protocol fixtures, never actual Office qualification."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time

SESSION = 'a' * 32
DOCUMENT = 'b' * 32
CITATION = 'c' * 32


def capture(session=SESSION, *, content='Unchanged source text.\r', point=0):
    return {'schema': 'resonance-word-capture/1', 'session': session,
        'document': {'hwnd': 1, 'pid': 2, 'creation_time': 3,
            'word_version': 'synthetic/no Office', 'name': 'Synthetic.docx',
            'full_name': 'Synthetic.docx', 'text': content,
            'outside_main_sha256': sha256(content.encode('utf-8')).hexdigest(), 'readonly': False,
            'protection': -1, 'track_revisions': False, 'undo_level': 0},
        'selection': {'start': point, 'end': point, 'story': 1},
        'controls': [], 'fields': [], 'endnotes': [], 'metadata': None,
        'metadata_xml_sha256': None, 'legacy_parts': ['f' * 64]}


def reference(identifier='ref'):
    return {'id': identifier, 'type': 'book', 'title': 'Source methods',
            'author': [{'family': 'Smith', 'given': 'Jane'}],
            'issued': {'date-parts': [[2020]]}, 'publisher': 'Example Press'}


def interchange(*, identifier='ref', citation_id=CITATION):
    return {'schema': 1, 'references': [reference(identifier)],
        'clusters': [{'id': citation_id, 'items': [{'id': identifier}], 'note_index': None,
            'locator_evidence': [{'reference_id': identifier, 'locator_verified': None}]}],
        'locale': 'en-US', 'style': {'id': 'apa', 'title': 'APA synthetic protocol fixture',
            'kind': 'in-text', 'sha256': 'd' * 64, 'source': 'https://www.zotero.org/styles/apa',
            'license': 'CC-BY-SA-3.0'},
        'processor': {'name': 'synthetic fixture, not a native/CSL result'},
        'attribution': 'Protocol fixture only.', 'bibliography_parameters': {},
        'bibliography': [{'reference_ids': [identifier], 'text': 'Smith. Source methods.',
                          'html': 'Smith. <i>Source methods.</i>'}],
        'citations': [{'cluster_id': citation_id, 'reference_ids': [identifier],
                       'text': '(Smith, 2020)', 'html': '(Smith, 2020)', 'note_index': None}],
        'warnings': []}


def proposal(current=None):
    from carr_library.academic_integration.word_records import fingerprint
    current = current or capture()
    data = interchange()
    return {'schema': 'resonance-word-proposal/1', 'session': current['session'],
        'capture_sha256': fingerprint(current), 'action': 'insert_citation',
        'source_scope': {'workspace': 'fixture-workspace', 'snapshot_sha256': 'e' * 64, 'name_mode': 'literal'},
        'document_id': DOCUMENT, 'revision': 1, 'interchange': data,
        'operations': [{'tag': 'resonance:cite:' + CITATION, 'native_id': None,
            'text': '(Smith, 2020)', 'runs': [{'text': '(Smith, 2020)', 'styles': []}],
            'note': False, 'note_index': None}]}


def receipt(value):
    from carr_library.academic_integration.word_records import fingerprint
    bindings = [{'tag': row['tag'], 'native_id': row['native_id'] or ('fixture-' + str(index)),
                 'text_sha256': sha256(row['text'].encode('utf-8')).hexdigest(),
                 'story': 3 if row['note'] else 1}
                for index, row in enumerate(value['operations'], 1)]
    by_tag = {row['tag']: row for row in bindings}
    metadata = {'schema': 'resonance-word-citations/1', 'document_id': value['document_id'],
        'revision': value['revision'], 'interchange': value['interchange'],
        'bindings': [by_tag['resonance:cite:' + row['id']] for row in value['interchange']['clusters']],
        'bibliography': by_tag.get('resonance:biblio:' + value['document_id']),
        'source_scope': value['source_scope'], 'last_request': value['session']}
    return {'schema': 'resonance-word-completed/1', 'session': value['session'],
        'proposal_sha256': fingerprint(value), 'document_id': value['document_id'],
        'revision': value['revision'], 'metadata_sha256': fingerprint(metadata),
        'bindings': sorted(bindings, key=lambda row: row['tag']), 'warnings': [], 'saved_by_bridge': False}


def installed_capture(value):
    from carr_library.academic_integration.word_records import fingerprint
    done = receipt(value)
    data = value['interchange']
    current = capture(value['session'])
    by_tag = {row['tag']: row for row in done['bindings']}
    offset = 0
    for operation in value['operations']:
        binding = by_tag[operation['tag']]
        current['controls'].append(dict(native_id=binding['native_id'], tag=operation['tag'],
            story=binding['story'], start=offset, end=offset + len(operation['text']),
            anchor=offset, text=operation['text'], locked=False, note_index=operation['note_index']))
        offset += len(operation['text'])
    current['document']['text'] = ''.join(row['text'] for row in value['operations']) + '\r'
    current['document']['outside_main_sha256'] = sha256(b'\r').hexdigest()
    current['selection'] = {'start': offset, 'end': offset, 'story': 1}
    current['metadata'] = {'schema': 'resonance-word-citations/1', 'document_id': value['document_id'],
        'revision': value['revision'], 'interchange': deepcopy(data),
        'bindings': [by_tag['resonance:cite:' + row['id']] for row in data['clusters']],
        'bibliography': by_tag.get('resonance:biblio:' + value['document_id']),
        'source_scope': dict(value['source_scope']), 'last_request': value['session']}
    current['metadata_xml_sha256'] = fingerprint(current['metadata'])
    return current


class SyntheticNative:
    def __init__(self, session, *, root=None, mode='complete', observation=None):
        self.session = session
        self.root = Path(root or Path.cwd())
        self.mode = mode
        self.observation = Path(observation) if observation is not None else self.root
        self.captures = 0
        self.writes = 0
        self.closed = False

    def _record(self, name, value):
        from carr_library.durable_state import atomic_json
        atomic_json(self.observation / name, value)

    def capture(self):
        self.captures += 1
        value = capture(self.session)
        if self.mode == 'changed-word' and self.captures > 1:
            value['document']['text'] = 'Changed while preview was open.\r'
        return value

    def apply(self, value, current, *, check, first_write):
        if self.mode == 'slow-first-write':
            time.sleep(5.1)
        first_write()
        check()
        self.writes += 1
        self._record('mutation.json', {'count': self.writes, 'proposal': value, 'actual_word': False})
        if self.mode == 'after-mutation-hang':
            while True:
                time.sleep(.05)
        result = receipt(value)
        if self.mode == 'bad-receipt':
            result['metadata_sha256'] = '0' * 64
        return result

    def close(self):
        self.closed = True
        if self.mode == 'close-error':
            raise OSError('Synthetic close error after receipt publication.')


def child_main():
    # Test-only fixed wrapper. Product worker still has its original fixed argv.
    package_root, expected_worker, digest, mode, observation = sys.argv[1:]
    sys.path.insert(0, package_root)
    from carr_library.academic_integration import word_worker
    if Path(word_worker.__file__).resolve() != Path(expected_worker).resolve():
        raise ValueError('The synthetic wrapper imported a different product worker.')
    Path(observation, 'pid.txt').write_text(str(os.getpid()), encoding='ascii')
    result = word_worker.run_protocol(Path.cwd(), digest,
        backend_factory=lambda session: SyntheticNative(session, mode=mode, observation=observation))
    raise SystemExit(result)


if __name__ == '__main__':
    child_main()
