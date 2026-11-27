"""Pronunciation drafts for canonical spoken Lines and existing project revisions."""
from dataclasses import dataclass, replace
import stat

from ..durable_state import encode, local_path, regular
from ..speech import ReadingDocument
from ..speech._io import check_cancelled
from .project import Project, ProjectStore

MAX_PROJECTS = 1000
MAX_PROJECT_LIST_BYTES = 32 * 1024 * 1024


def spoken_line(project, scene_id, line_id):
    if not isinstance(project, Project):
        raise ValueError('Choose a saved Studio project.')
    scene = next((item for item in project.scenes if item.id == scene_id), None)
    line = next((item for item in scene.lines if item.id == line_id), None) if scene else None
    if line is None:
        raise ValueError('Choose an existing spoken line in the selected scene.')
    role = next(item for item in project.roles if item.id == line.role)
    return scene, line, role


@dataclass(frozen=True)
class SpokenLineDraft:
    project: Project
    scene_id: str
    line_id: str
    document: ReadingDocument

    @property
    def line(self):
        return spoken_line(self.project, self.scene_id, self.line_id)[1]

    @property
    def role(self):
        return spoken_line(self.project, self.scene_id, self.line_id)[2]

    @property
    def dirty(self):
        return self.document.text != self.line.synthesis_text


@dataclass(frozen=True)
class SpokenLineChange:
    before: Project
    after: Project
    scene_id: str
    line_id: str


class StudioPronunciations:
    """No synthesis or new dictionary engine; publish through ProjectStore only."""
    def __init__(self, library_root):
        self.root = local_path(library_root, exists=True)
        self.projects = ProjectStore(self.root / 'state' / 'studio' / 'projects')

    def list_projects(self, *, cancelled=lambda: False):
        identities, inspected_bytes = [], 0
        for path in self.projects.root.iterdir():
            check_cancelled(cancelled)
            value = path.lstat()
            if not stat.S_ISDIR(value.st_mode):
                # Links/reparse entries are never followed during enumeration.
                if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Studio project paths must not be linked.')
                continue
            regular(path, directory=True)
            if len(identities) >= MAX_PROJECTS:
                raise ValueError('The Studio project list exceeds its review budget.')
            saved = local_path(path / 'project.json', root=self.projects.root, exists=True)
            size = regular(saved).st_size
            inspected_bytes += size
            if inspected_bytes > MAX_PROJECT_LIST_BYTES:
                raise ValueError('The Studio project list exceeds its aggregate review byte budget '
                                 f'({MAX_PROJECT_LIST_BYTES} bytes); the whole collection cannot be loaded.')
            identities.append((path.name, size))
        result, loaded_bytes = [], 0
        for identity, inspected_size in sorted(identities):
            check_cancelled(cancelled)
            project = self.projects.load(identity)
            # The preflight avoids constructing a large collection. Recheck
            # actual parsed content in case a project grew after that preflight.
            loaded_bytes += max(inspected_size, len(encode(project.to_dict())))
            if loaded_bytes > MAX_PROJECT_LIST_BYTES:
                raise ValueError('The Studio project list grew beyond its aggregate review byte budget '
                                 f'({MAX_PROJECT_LIST_BYTES} bytes).')
            result.append(project)
        check_cancelled(cancelled)
        return tuple(result)

    def open_line(self, project, scene_id, line_id, *, cancelled=lambda: False):
        check_cancelled(cancelled)
        scene, line, role = spoken_line(project, scene_id, line_id)
        self._current(project)
        document = ReadingDocument(line.synthesis_text, title=f'{project.title} — {scene.title} — {role.name}')
        check_cancelled(cancelled)
        return SpokenLineDraft(project, scene_id, line_id, document)

    def _current(self, project):
        current = self.projects.load(project.id)
        if current.fingerprint != project.fingerprint:
            raise ValueError('The Studio project changed. Reload the spoken line before editing it.')
        return current

    def save_line(self, draft, *, cancelled=lambda: False, guard=lambda: None):
        if not isinstance(draft, SpokenLineDraft) or not isinstance(draft.document, ReadingDocument):
            raise ValueError('Open an existing spoken-line reading draft.')
        guard(); check_cancelled(cancelled)
        scene, line, _ = spoken_line(draft.project, draft.scene_id, draft.line_id)
        if draft.document.source_text != line.synthesis_text:
            raise ValueError('The reading draft belongs to a different spoken line.')
        self._current(draft.project)
        if not draft.dirty:
            return SpokenLineChange(draft.project, draft.project, scene.id, line.id)
        edited = (replace(line, spoken_text=draft.document.text) if line.spoken_text else
                  replace(line, text=draft.document.text))
        changed_scene = replace(scene, lines=tuple(edited if item.id == line.id else item for item in scene.lines))
        updated = replace(draft.project, revision=draft.project.revision + 1,
            scenes=tuple(changed_scene if item.id == scene.id else item for item in draft.project.scenes))
        guard(); check_cancelled(cancelled)
        saved = self.projects.save(updated, expected_revision=draft.project.revision,
                                   expected_fingerprint=draft.project.fingerprint)
        # Cancellation after the atomic project publication cannot undo a save.
        return SpokenLineChange(draft.project, saved, scene.id, line.id)

    def undo_line(self, change, *, cancelled=lambda: False, guard=lambda: None):
        if (not isinstance(change, SpokenLineChange) or change.before.id != change.after.id
                or change.before.fingerprint == change.after.fingerprint):
            raise ValueError('There is no saved pronunciation change to undo.')
        guard(); check_cancelled(cancelled)
        previous = spoken_line(change.before, change.scene_id, change.line_id)[1]
        current = self.open_line(change.after, change.scene_id, change.line_id, cancelled=cancelled)
        current.document.set_text(previous.synthesis_text, label='Undo saved pronunciation')
        return self.save_line(current, cancelled=cancelled, guard=guard)
