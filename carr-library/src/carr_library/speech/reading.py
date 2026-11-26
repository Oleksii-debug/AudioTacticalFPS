"""Reading-view adapter; source offsets and edit replay stay in NarrationDraft."""
from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
import re

from ..reader.narration_text import NarrationDraft, decode_json, read_document
from ..research.export_file import write_export
from ._io import local_file, text_value
from .source import validate_source_binding


class ReadingDocument:
    def __init__(self, source_text, *, title='Reading copy', source_path=None,
                 source_id='manual', source_binding=None, private_root=None):
        text_value(source_text, 'reading source', empty=True)
        self.title = text_value(title, 'reading title', maximum=256)
        self.draft = NarrationDraft(source_text, source_id=source_id)
        self.source_path = str(source_path) if source_path is not None else None
        self.source_binding = source_binding
        if source_binding is not None and validate_source_binding(source_binding, source_text) != source_id:
            raise ValueError('The reading draft belongs to a different source revision.')
        if source_binding is None and source_id != 'manual':
            raise ValueError('Source-bound reading copies require their exact source positions.')
        self.private_root = Path(private_root) if private_root is not None else None
        self.path = None
        self._file_hash = None
        self._saved_signature = self._signature()

    @property
    def text(self):
        return self.draft.text

    @property
    def source_text(self):
        return self.draft.source_text

    @property
    def source_id(self):
        return self.draft.source_id

    @property
    def revision(self):
        return len(self.draft.edits) + 1

    @property
    def dirty(self):
        return self._signature() != self._saved_signature

    def _signature(self):
        return sha256(json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                                 allow_nan=False).encode('utf-8')).hexdigest()

    def set_text(self, value):
        text_value(value, 'reading copy', empty=True)
        return self.draft.edit_text(value)

    def undo(self):
        return self.draft.undo()

    def redo(self):
        return self.draft.redo()

    def cleanup(self):
        value = '\n'.join(re.sub(r'[ \t]+', ' ', line).rstrip() for line in self.text.splitlines())
        return self.draft.edit_text(value, reason='Explicit reading-copy whitespace cleanup')

    def diff(self):
        return self.draft.difference_text()

    def to_dict(self):
        return {'kind': 'research-live-reading-copy', 'version': 1, 'title': self.title,
                'source_path': self.source_path, 'source_binding': self.source_binding,
                'draft': self.draft.to_dict()}

    @classmethod
    def from_dict(cls, value, *, private_root=None):
        if (type(value) is not dict or set(value) != {'kind', 'version', 'title', 'source_path', 'source_binding', 'draft'}
                or value['kind'] != 'research-live-reading-copy' or type(value['version']) is not int
                or value['version'] != 1):
            raise ValueError('Unsupported reading-copy project.')
        source_path = value['source_path']
        if source_path is not None:
            text_value(source_path, 'original source path', maximum=8192)
            if source_path.startswith(('//', '\\\\')) or not (
                    Path(source_path).is_absolute() or PureWindowsPath(source_path).is_absolute()):
                raise ValueError('Invalid original source path.')
        draft = NarrationDraft.from_dict(value['draft'])
        result = cls(draft.source_text, title=value['title'], source_path=source_path,
                     source_id=draft.source_id, source_binding=value['source_binding'], private_root=private_root)
        result.draft = draft
        result._saved_signature = result._signature()
        return result

    def save(self, path):
        if self.private_root is None:
            raise ValueError('Choose an active library before saving the reading copy.')
        selected = local_file(path, must_exist=False)
        original = Path(self.source_path) if self.source_path is not None else None
        if original is not None and (selected == original
                or (selected.exists() and original.exists() and selected.samefile(original))):
            raise ValueError('The original source is immutable; choose a separate reading-project file.')
        if selected.exists():
            if selected != self.path:
                raise FileExistsError('Choose a new reading-project filename.')
            if sha256(read_document(selected)).hexdigest() != self._file_hash:
                raise ValueError('The saved reading project changed outside this window. Use Save As.')
        elif selected == self.path:
            raise ValueError('The saved reading project was removed. Use Save As.')
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n'
        write_export(selected, raw, private_root=self.private_root)
        self.path, self._file_hash = selected, sha256(raw.encode('utf-8')).hexdigest()
        self._saved_signature = self._signature()
        return selected

    @classmethod
    def load(cls, path, *, private_root=None):
        selected = local_file(path)
        raw = read_document(selected, byte_budget=8 * 1024 * 1024)
        result = cls.from_dict(decode_json(raw.decode('utf-8')), private_root=private_root)
        result.path, result._file_hash = selected, sha256(raw).hexdigest()
        return result
