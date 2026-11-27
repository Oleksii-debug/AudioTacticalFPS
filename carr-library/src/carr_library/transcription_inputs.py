"""Bounded file/folder selection for the existing transcription job service.

This preflight only reads directory entries and file metadata. The explicit
Submit/Run action still retains and verifies every source through the same
TranscriptionService; workers receive its finite managed file list, not folders.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .durable_state import local_path, regular
from .transcription import (AUDIO_EXTENSIONS, MAX_INPUT_BYTES, MAX_INPUT_FILES,
                            MAX_JOB_INPUT_BYTES, _stamp)

MAX_SCAN_ENTRIES = 20000
MAX_SCAN_DEPTH = 64
MAX_SKIP_DETAILS = 100


@dataclass(frozen=True)
class InputSelection:
    files: tuple[Path, ...]
    total_bytes: int
    skipped: tuple[str, ...]
    skipped_count: int

    def describe(self):
        lines = [f'{len(self.files)} audio input(s), {self.total_bytes} bytes selected. '
                 'Only this finite file list will be submitted; originals remain unchanged.']
        if self.skipped_count:
            lines.append(f'Skipped {self.skipped_count} entry/entries:')
            lines.extend(self.skipped)
            if self.skipped_count > len(self.skipped):
                lines.append(f'{self.skipped_count - len(self.skipped)} further skipped entries are not listed.')
        return '\n'.join(lines)


def select_inputs(files=(), *, folders=(), recursive=False, cancelled=lambda: False):
    """Expand only selected local folders, in stable name order, without links.

    Explicit files keep their given order; sorted folder contents follow them.
    Subfolders require an explicit recursion option. Unsupported entries and
    links are disclosed, while an incomplete or over-budget scan fails closed.
    No job, engine, persistent watch, or broader future folder access is created.
    """
    if (type(recursive) is not bool or not isinstance(files, (list, tuple))
            or not isinstance(folders, (list, tuple))
            or not 1 <= len(files) + len(folders) <= MAX_INPUT_FILES):
        raise ValueError('Choose 1–' + str(MAX_INPUT_FILES) + ' explicit local audio files or folders.')
    if recursive and not folders:
        raise ValueError('Choose a folder before enabling subfolders.')
    paths, seen, skipped = [], set(), []
    total = skipped_count = entries = 0

    def check():
        if cancelled():
            raise InterruptedError('Transcription input selection cancelled.')

    def skip(path, root, reason):
        nonlocal skipped_count
        skipped_count += 1
        if len(skipped) < MAX_SKIP_DETAILS:
            name = root.name + '/' + path.relative_to(root).as_posix()
            skipped.append((name + ': ' + reason)[:800])

    def add(value, *, root=None):
        nonlocal total
        check()
        path = local_path(value, root=root, exists=True)
        info = regular(path)
        if path in seen:
            if root is None:
                raise ValueError('Choose each explicit source file only once.')
            skip(path, root, 'Already selected; included only once.')
            return
        if path.suffix.casefold() not in AUDIO_EXTENSIONS or not 0 < info.st_size <= MAX_INPUT_BYTES:
            raise ValueError('Choose each supported nonempty local audio file within the per-file byte budget: ' + str(path))
        if len(paths) >= MAX_INPUT_FILES:
            raise ValueError('Folder selection exceeds the ' + str(MAX_INPUT_FILES) + '-file job budget. Select a smaller folder.')
        total += info.st_size
        if total > MAX_JOB_INPUT_BYTES:
            raise ValueError('Selected transcription inputs exceed the job byte budget.')
        seen.add(path)
        paths.append(path)

    def visit(directory, root, depth):
        nonlocal entries
        check()
        directory = local_path(directory, root=root, exists=True)
        before = regular(directory, directory=True)
        names = []
        with os.scandir(directory) as children:
            for child in children:
                check()
                entries += 1
                if entries > MAX_SCAN_ENTRIES:
                    raise ValueError('Folder selection exceeds the directory-entry scan budget. Select a smaller folder.')
                names.append(child.name)
        if _stamp(regular(directory, directory=True)) != _stamp(before):
            raise ValueError('A selected folder changed during selection. Choose it again.')
        for name in sorted(names, key=lambda value: (value.casefold(), value)):
            check()
            path = directory / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                skip(path, root, 'Link or reparse point; not followed.')
            elif stat.S_ISDIR(info.st_mode):
                if not recursive:
                    skip(path, root, 'Subfolder excluded; enable Include subfolders to select it.')
                elif depth + 1 >= MAX_SCAN_DEPTH:
                    raise ValueError('Folder selection exceeds the depth budget. Select a nearer folder.')
                else:
                    visit(path, root, depth + 1)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                skip(path, root, 'Not a regular unlinked local file; excluded.')
            elif path.suffix.casefold() not in AUDIO_EXTENSIONS:
                skip(path, root, 'Unsupported audio/video extension; excluded.')
            else:
                add(path, root=root)
        local_path(directory, root=root, exists=True)
        if _stamp(regular(directory, directory=True)) != _stamp(before):
            raise ValueError('A selected folder changed during selection. Choose it again.')
        # Native directory timestamps need not expose a late entry immediately.
        # Verify the exact bounded name set as well; do not accept a partial
        # snapshot or silently incorporate sources added during review. At most
        # len(names) matching entries and one unexpected entry are examined.
        remaining = set(names)
        with os.scandir(directory) as children:
            for child in children:
                check()
                if child.name not in remaining:
                    raise ValueError('A selected folder changed during selection. Choose it again.')
                remaining.remove(child.name)
        check()
        local_path(directory, root=root, exists=True)
        if remaining or _stamp(regular(directory, directory=True)) != _stamp(before):
            raise ValueError('A selected folder changed during selection. Choose it again.')

    check()
    for path in files:
        add(path)
    roots = set()
    for value in folders:
        check()
        root = local_path(value, exists=True)
        regular(root, directory=True)
        roots.add(root)
    for root in sorted(roots, key=lambda path: (str(path).casefold(), str(path))):
        visit(root, root, 0)
    check()
    selection = InputSelection(tuple(paths), total, tuple(skipped), skipped_count)
    if not paths:
        raise ValueError('No supported nonempty audio inputs were selected.\n' + selection.describe())
    return selection
