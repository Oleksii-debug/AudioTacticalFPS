"""Ordered citation editing over the existing scoped CSL and DOCX services.

Drafts are presentation data. They never approve a quotation, infer a locator,
change a bibliographic record or acquire filesystem/network authority.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import uuid4

from .bibliography_workbench import BibliographyWorkbench, METADATA_NOTICE, records_for_style
from .csl import RenderBudget, _clusters, _records, render
from ..research.export_file import capture_export_destination, write_binary_export


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class CitationSelection:
    workspace: str
    identity: str | None
    snapshot_sha256: str
    name_mode: str
    records_json: str
    warnings: tuple[str, ...]

    @property
    def records(self):
        return json.loads(self.records_json)


def normalized_clusters(selection, clusters):
    if not isinstance(selection, CitationSelection):
        raise TypeError('Choose a captured reference selection.')
    records = selection.records
    identities = {record['id'] for record in records}
    rows, _manual, contexts, _cost = _clusters(
        clusters, records, identities, RenderBudget(), note_style=False)
    result = []
    for items, context in zip(rows, contexts):
        value = {'id': context['id'], 'items': items}
        if context['note_index']:
            value['note_index'] = context['note_index']
        result.append(value)
    return _json(result)


class CitationDraft:
    """Bounded editing with stable occurrence IDs and atomic validation."""
    def __init__(self, selection, clusters=()):
        self.selection = selection
        self._value = normalized_clusters(selection, clusters)

    @property
    def clusters(self):
        return json.loads(self._value)

    @property
    def captured(self):
        return self._value

    def _save(self, value):
        # Validate the complete replacement before changing a valid draft.
        encoded = normalized_clusters(self.selection, value)
        self._value = encoded

    @staticmethod
    def _index(value, length):
        if type(value) is not int or not 0 <= value < length:
            raise ValueError('Choose an existing citation or reference item.')
        return value

    def add(self, item, *, group=None, note_index=None):
        clusters = self.clusters
        if group is None:
            group = len(clusters)
            clusters.append({'id': 'citation-' + uuid4().hex, 'items': [item]})
        else:
            self._index(group, len(clusters))
            clusters[group]['items'].append(item)
        if note_index is not None:
            clusters[group]['note_index'] = note_index
        self._save(clusters)
        return group

    def replace_item(self, group, item_index, item, *, note_index=None):
        clusters = self.clusters
        self._index(group, len(clusters))
        self._index(item_index, len(clusters[group]['items']))
        clusters[group]['items'][item_index] = item
        if note_index is None:
            clusters[group].pop('note_index', None)
        else:
            clusters[group]['note_index'] = note_index
        self._save(clusters)

    def remove_item(self, group, item_index):
        clusters = self.clusters
        self._index(group, len(clusters))
        self._index(item_index, len(clusters[group]['items']))
        clusters[group]['items'].pop(item_index)
        if not clusters[group]['items']:
            clusters.pop(group)
        self._save(clusters)

    def remove_group(self, group):
        clusters = self.clusters
        self._index(group, len(clusters))
        clusters.pop(group)
        self._save(clusters)

    def move_group(self, group, step):
        clusters = self.clusters
        self._index(group, len(clusters))
        if type(step) is not int or step not in (-1, 1):
            raise ValueError('Move a citation one position up or down.')
        target = group + step
        self._index(target, len(clusters))
        clusters[group], clusters[target] = clusters[target], clusters[group]
        self._save(clusters)
        return target


@dataclass(frozen=True)
class CitationPreview:
    selection: CitationSelection
    clusters_json: str
    style: object
    locale: str
    result: object

    @property
    def bibliography(self):
        return '\n\n'.join(self.result.entries).strip() + '\n'

    @property
    def citations(self):
        return '\n'.join(self.result.citations).strip() + '\n'

    @property
    def explanation(self):
        warnings = tuple(dict.fromkeys((*self.selection.warnings, *self.result.warnings)))
        groups = json.loads(self.clusters_json)
        lines = []
        for index, (group, citation) in enumerate(zip(groups, self.result.citations), 1):
            note = self.result.note_indices[index - 1]
            label = f'{index}. ' + (f'Note {note}. ' if self.result.style.kind == 'note' else '')
            lines.append(label + citation)
        parts = [self.result.style.title, METADATA_NOTICE,
                 'Citations in document order:\n' + '\n\n'.join(lines),
                 'Bibliography:\n' + self.bibliography]
        if warnings:
            parts.append('Review notes:\n' + '\n'.join(warnings))
        parts.append(self.result.attribution)
        return '\n\n'.join(parts)


class CitationClusterWorkbench:
    def __init__(self, controller):
        self.controller = controller
        self.base = BibliographyWorkbench(controller)

    def selection(self, *, identity=None, name_mode='literal', cancelled=lambda: False):
        if cancelled():
            raise TimeoutError('Reference loading cancelled.')
        snapshot = self.base._snapshot(identity)
        records, name_warnings = records_for_style(snapshot, name_mode=name_mode)
        rows, _identities, record_warnings = _records(records, RenderBudget())
        selected = CitationSelection(self.controller.workspace, identity,
            sha256(snapshot.encode('utf-8')).hexdigest(), name_mode, _json(rows),
            tuple(dict.fromkeys((*name_warnings, *record_warnings))))
        self.check_selection(selected)
        if cancelled():
            raise TimeoutError('Reference loading cancelled.')
        return selected

    def check_selection(self, selected):
        if (not isinstance(selected, CitationSelection)
                or selected.workspace != self.controller.workspace):
            raise ValueError('These citations belong to another library.')
        snapshot = self.base._snapshot(selected.identity)
        if sha256(snapshot.encode('utf-8')).hexdigest() != selected.snapshot_sha256:
            raise ValueError('The references changed. Reopen the citation editor and review the locators again.')
        return selected

    def preview(self, selected, clusters_json, *, style='apa', locale='en-US',
                cancelled=lambda: False):
        self.check_selection(selected)
        if cancelled():
            raise TimeoutError('Citation formatting cancelled.')
        if not isinstance(clusters_json, str) or len(clusters_json.encode('utf-8')) > RenderBudget().max_input_bytes:
            raise ValueError('The citation draft exceeds the formatting byte budget.')
        clusters = json.loads(clusters_json)
        captured = normalized_clusters(selected, clusters)
        if not clusters:
            raise ValueError('Add at least one citation in document order.')
        result = render(selected.records, citations=json.loads(captured),
                        style=style, locale=locale, output='text')
        self.check_selection(selected)
        if cancelled():
            raise TimeoutError('Citation formatting cancelled.')
        return CitationPreview(selected, captured, style, locale, result)

    def check(self, preview):
        if not isinstance(preview, CitationPreview):
            raise ValueError('Preview the current citation order first.')
        self.check_selection(preview.selection)
        if normalized_clusters(preview.selection, json.loads(preview.clusters_json)) != preview.clusters_json:
            raise ValueError('The citation order changed after formatting.')
        return preview

    def copy_text(self, preview, *, part='citations'):
        self.check(preview)
        if part == 'citations':
            return preview.citations
        if part == 'bibliography':
            return preview.bibliography
        raise ValueError('Choose citations or bibliography to copy.')

    def export_document(self, preview, destination, *, cancelled=lambda: False, approval=None):
        from .documents import CitationParagraph, citation_document
        self.check(preview)
        if approval is None:
            approval = capture_export_destination(destination, private_root=self.controller.library.root)
        if not approval.require_absent:
            raise FileExistsError('Choose a new filename; citation documents are never overwritten.')

        def permitted():
            if cancelled():
                return True
            self.check(preview)
            return False

        clusters = json.loads(preview.clusters_json)
        paragraphs = tuple(CitationParagraph('', (group['id'],)) for group in clusters)
        content = citation_document(preview.selection.records, style=preview.style,
            locale=preview.locale, citations=clusters, paragraphs=paragraphs,
            cancelled=permitted)
        write_binary_export(destination, content, private_root=self.controller.library.root,
            cancelled=permitted, require_absent=True, approval=approval)
        return content

    def load_document_order(self, selected, source, *, cancelled=lambda: False):
        from .documents import read_citation_docx
        self.check_selection(selected)
        if cancelled():
            raise TimeoutError('Citation document loading cancelled.')
        saved = read_citation_docx(source)
        if _json(saved['records']) != selected.records_json:
            raise ValueError('The saved document references differ from the current selection. '
                             'Review the current metadata and locators before rebuilding its citations.')
        draft = CitationDraft(selected, saved['clusters'])
        self.check_selection(selected)
        if cancelled():
            raise TimeoutError('Citation document loading cancelled.')
        return draft
