"""Bounded browsing of the existing immutable Studio revision files.

There is no second history database. A restore saves the selected content as a
new current revision through ProjectStore's existing compare-and-swap writer.
"""
from dataclasses import dataclass, replace
import heapq
import os
import re

from ..durable_state import local_path, read_json, regular
from .project import Project


@dataclass(frozen=True)
class RevisionRef:
    revision: int
    fingerprint: str

    def __post_init__(self):
        if type(self.revision) is not int or not 1 <= self.revision < 10**16:
            raise ValueError('Invalid saved project revision.')
        if not isinstance(self.fingerprint, str) or re.fullmatch(r'[0-9a-f]{64}', self.fingerprint) is None:
            raise ValueError('Invalid saved project identity.')


def revisions(store, project, *, before_revision=None, limit=100, cancelled=lambda: False):
    """Return newest references first, with an explicit cursor for earlier rows."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Choose between one and two hundred history rows.')
    boundary = project.revision if before_revision is None else before_revision
    if type(boundary) is not int or not 1 <= boundary <= project.revision:
        raise ValueError('History cursor is outside this project.')
    directory = store.directory(project.id) / 'revisions'
    if not os.path.lexists(directory):
        return ()
    directory = local_path(directory, root=store.root, exists=True)
    regular(directory, directory=True)
    chosen = []
    # Only bounded references are kept in memory, even for long project history.
    with os.scandir(directory) as entries:
        for entry in entries:
            if cancelled():
                raise InterruptedError('Project history review cancelled.')
            match = re.fullmatch(r'([0-9]{8,16})-([0-9a-f]{64})[.]json', entry.name)
            if not match:
                raise ValueError('Project history contains an unrecognized revision entry.')
            revision = int(match[1])
            reference = RevisionRef(revision, match[2])
            if revision >= project.revision:
                raise ValueError('Project history contains a future or current revision.')
            regular(directory / entry.name)
            if revision < boundary:
                item = (revision, reference.fingerprint)
                if len(chosen) < limit:
                    heapq.heappush(chosen, item)
                elif item > chosen[0]:
                    heapq.heapreplace(chosen, item)
    result = tuple(RevisionRef(number, fingerprint)
                   for number, fingerprint in sorted(chosen, reverse=True))
    if len({item.revision for item in result}) != len(result):
        raise ValueError('Project history has ambiguous saved revision identities.')
    return result


def restored_project(store, current, reference):
    """Read exactly the reviewed historical identity; never accept supplied content."""
    if not isinstance(reference, RevisionRef) or reference.revision >= current.revision:
        raise ValueError('Select an earlier saved revision of this project.')
    directory = local_path(store.directory(current.id), root=store.root, exists=True)
    path = local_path(directory / 'revisions' /
                      f'{reference.revision:08d}-{reference.fingerprint}.json',
                      root=directory, exists=True)
    saved = Project.from_dict(read_json(path))
    if (saved.id != current.id or saved.revision != reference.revision
            or saved.fingerprint != reference.fingerprint):
        raise ValueError('Saved project content differs from its reviewed revision identity.')
    return replace(saved, revision=current.revision + 1)
