"""Private original-byte retention for cooperative ingestion writers.

Internal service only: asset hashes are not authorization tokens. The caller
must hold the shared writer guard and supply a stable private directory.
"""
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from ..extract.document_formats import MIME_SUFFIXES
from ..catalog.intake import FileSnapshot, source_snapshot, source_limit, _hash_stream

MAX_BYTES = 64 * 1024 * 1024
SUFFIXES = dict(MIME_SUFFIXES)


class RetentionError(ValueError):
    pass


class RetainedAssets:
    def __init__(self, root):
        path = Path(root)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RetentionError('private asset root must be a local directory')
        self.root = path.resolve(strict=True)

    def _paths(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RetentionError('invalid asset digest')
        return [self.root / (digest + suffix) for suffix in SUFFIXES.values()]

    def resolve(self, digest, *, cancelled=lambda: False):
        """Internal verified lookup; never expose directly to unscoped UI."""
        paths = [p for p in self._paths(digest) if os.path.lexists(p)]
        if len(paths) != 1:
            raise RetentionError('missing or ambiguous retained asset')
        path = paths[0]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                getattr(info, 'st_file_attributes', 0) & 0x400 or
                not 0 < info.st_size <= source_limit(next(mime for mime, suffix in SUFFIXES.items()
                                                            if suffix == path.suffix))):
            raise RetentionError('invalid retained asset file')
        if self._digest(path, cancelled=cancelled) != digest:
            raise RetentionError('retained asset digest mismatch')
        return path

    @staticmethod
    def _digest(path, *, cancelled=lambda: False):
        before = path.stat()
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
                               value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        with path.open('rb') as stream:
            if stamp(os.fstat(stream.fileno())) != stamp(before):
                raise RetentionError('retained asset changed while opening')
            digest, size = _hash_stream(stream, cancelled=cancelled, maximum=before.st_size)
            if (stamp(os.fstat(stream.fileno())) != stamp(before)
                    or stamp(path.lstat()) != stamp(before) or size != before.st_size):
                raise RetentionError('retained asset changed during verification')
        return digest

    def retain_snapshot(self, asset, *, cancelled=lambda: False):
        """Reuse the exact private publication transaction for file-backed originals."""
        if not isinstance(asset, FileSnapshot):
            if cancelled():
                raise InterruptedError('Original retention cancelled.')
            return self.retain(asset.data, asset.mime, asset.digest)
        if asset.mime not in SUFFIXES or not 0 < asset.size <= source_limit(asset.mime):
            raise RetentionError('unsupported or oversized file-backed original')
        target = self.root / (asset.digest + SUFFIXES[asset.mime])
        paths = self._paths(asset.digest)
        existing = [path for path in paths if os.path.lexists(path)]
        if existing:
            if len(existing) != 1 or existing[0] != target:
                raise RetentionError('missing, ambiguous or differently typed retained original')
            asset.verify(cancelled=cancelled)
            self._recover_staged_publication(target, asset.digest)
            return self.resolve(asset.digest, cancelled=cancelled)
        with TemporaryDirectory(prefix='.carr-original-', dir=self.root) as tmp:
            stage = Path(tmp) / 'original'
            asset.copy_to(stage, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Original retention cancelled before publication.')
            # Keep the existing no-replace link and abandoned-twin recovery.
            os.link(stage, target)
        return self.resolve(asset.digest, cancelled=cancelled)

    def _recover_staged_publication(self, target, digest):
        """Finish only a fully accounted link publication under the writer guard.

        A process can die after linking the target but before TemporaryDirectory
        removes its original. Readers still reject extra links. A writer may
        remove those exact staging twins only after validating the complete set;
        an unknown link or ambiguous directory leaves every entry untouched.
        """
        published = target.lstat()
        if (not stat.S_ISREG(published.st_mode) or
                getattr(published, 'st_file_attributes', 0) & 0x400 or
                not 0 < published.st_size <= source_limit(next(mime for mime, suffix in SUFFIXES.items()
                                                                 if suffix == target.suffix))):
            raise RetentionError('invalid retained asset file')
        if published.st_nlink == 1:
            return
        if published.st_nlink < 2 or not published.st_ino:
            raise RetentionError('invalid retained asset links')
        if hash_file(target) != digest:
            raise RetentionError('retained asset digest mismatch')

        def private_directory(path):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or
                    getattr(info, 'st_file_attributes', 0) & 0x400 or
                    (os.name == 'posix' and
                     (stat.S_IMODE(info.st_mode) != 0o700 or
                      info.st_uid != os.geteuid()))):
                raise RetentionError('invalid retained asset staging directory')
            return info

        def unchanged(before, after):
            return all(getattr(before, field, 0) == getattr(after, field, 0)
                       for field in ('st_dev', 'st_ino', 'st_mode', 'st_nlink',
                                     'st_size', 'st_mtime_ns', 'st_ctime_ns',
                                     'st_uid', 'st_file_attributes'))

        twins = []
        for directory in self.root.iterdir():
            # The fixed prefix and eight-character suffix are the namespace
            # created by this module's TemporaryDirectory, not arbitrary paths.
            if not re.fullmatch(r'\.carr-original-[a-z0-9_]{8}', directory.name):
                continue
            directory_info = private_directory(directory)
            entries = tuple(directory.iterdir())
            if not entries:
                # A recovery itself may die after unlinking its original.
                # Empty directories have no proven target binding; leave them.
                continue
            if len(entries) != 1 or entries[0].name != 'original':
                raise RetentionError('ambiguous retained asset staging directory')
            original = entries[0]
            original_info = original.lstat()
            if (not stat.S_ISREG(original_info.st_mode) or
                    getattr(original_info, 'st_file_attributes', 0) & 0x400):
                raise RetentionError('invalid retained asset staging original')
            if os.path.samestat(published, original_info):
                if not unchanged(published, original_info):
                    raise RetentionError('retained asset changed during recovery')
                twins.append((directory, directory_info, original, original_info))
        if len(twins) != published.st_nlink - 1:
            raise RetentionError('unaccounted retained asset links')

        # Complete all checks before removing even one staging entry. The caller
        # owns the cooperative writer guard and a stable private directory.
        if not unchanged(published, target.lstat()):
            raise RetentionError('retained asset changed during recovery')
        for directory, directory_info, original, original_info in twins:
            if (not unchanged(directory_info, private_directory(directory)) or
                    tuple(directory.iterdir()) != (original,) or
                    not unchanged(original_info, original.lstat())):
                raise RetentionError('retained asset staging changed during recovery')
        for directory, _, original, _ in twins:
            original.unlink()
            directory.rmdir()

    def retain(self, data, mime, digest):
        """Preserve source bytes; recover only proven abandoned staging links."""
        paths = self._paths(digest)
        if mime not in SUFFIXES or not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
            raise RetentionError('unsupported or oversized asset')
        if sha256(data).hexdigest() != digest:
            raise RetentionError('snapshot digest mismatch')
        target = self.root / (digest + SUFFIXES[mime])
        existing = [p for p in paths if os.path.lexists(p)]
        if existing:
            if len(existing) != 1:
                raise RetentionError('missing or ambiguous retained asset')
            if existing[0] != target:
                raise RetentionError('retained asset type mismatch')
            self._recover_staged_publication(target, digest)
            return self.resolve(digest)
        with TemporaryDirectory(prefix='.carr-original-', dir=self.root) as tmp:
            stage = Path(tmp) / 'original'
            with stage.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if hash_file(stage) != digest:
                raise RetentionError('staged asset digest mismatch')
            # Same-filesystem atomic publication, never replace. A crashed
            # publication is recoverable by retain after exact link accounting.
            os.link(stage, target)
        return self.resolve(digest)

    def start(self, ingest, workspace, edition, source_root, relative, **kwargs):
        """Reuse bounded catalog intake and existing Ingest.start."""
        asset = source_snapshot(source_root, relative, cancelled=kwargs.get('cancelled', lambda: False))
        retained = self.retain_snapshot(asset, cancelled=kwargs.get('cancelled', lambda: False))
        job = ingest.start(workspace, edition, self.root, retained.name, **kwargs)
        if ingest.inspect(workspace, job)['asset'] != asset.digest:
            raise RetentionError('job asset mismatch')
        return job

    def run(self, ingest, workspace, job, **kwargs):
        """Scope-check via existing journal first; resume from original bytes."""
        record = ingest.inspect(workspace, job)
        retained = self.resolve(record['asset'], cancelled=kwargs.get('cancelled', lambda: False))
        return ingest.run(workspace, job, self.root, retained.name, **kwargs)
