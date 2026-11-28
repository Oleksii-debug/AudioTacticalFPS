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

    def resolve(self, digest, *, profile=None, cancelled=lambda: False):
        """Internal verified lookup; never expose directly to unscoped UI."""
        maximum = MAX_BYTES
        if profile is not None:
            from .streamed_text import profile_from_value
            profile = profile_from_value(profile)
            maximum = profile.maximum_bytes
        paths = [p for p in self._paths(digest) if os.path.lexists(p)]
        if len(paths) != 1:
            raise RetentionError('missing or ambiguous retained asset')
        path = paths[0]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                getattr(info, 'st_file_attributes', 0) & 0x400 or
                not 0 < info.st_size <= maximum or
                profile is not None and path.suffix != profile.suffix):
            raise RetentionError('invalid retained asset file')
        if profile is None:
            actual = hash_file(path)
        else:
            from .streamed_text import file_digest
            actual = file_digest(path, maximum_bytes=maximum, cancelled=cancelled)
        if actual != digest:
            raise RetentionError('retained asset digest mismatch')
        return path

    def retain_file(self, source, mime, digest, size, *, profile=None,
                    expected_identity=None, cancelled=lambda: False):
        """Copy an explicitly admitted source into the same immutable store.

        The ordinary byte API keeps its old limit. This file-backed API uses
        bounded reads and the same private staging/recovery namespace. Only
        this attempt's exact two-link publication can release its staging twin.
        """
        from tempfile import mkdtemp
        import shutil
        from ..durable_state import local_path, sync_directory
        from .streamed_text import (check_cancelled, file_digest, file_identity,
                                    profile_from_value, regular_file, verified_reader)
        maximum = MAX_BYTES
        if profile is not None:
            profile = profile_from_value(profile)
            maximum = profile.maximum_bytes
        if (mime not in SUFFIXES or type(size) is not int or not 0 < size <= maximum
                or profile is not None and mime != profile.mime):
            raise RetentionError('unsupported or oversized streamed asset')
        paths = self._paths(digest)
        source = local_path(source, exists=True)
        before = regular_file(source)
        if before.st_size != size:
            raise RetentionError('invalid streamed source identity')
        identity = file_identity(before)
        if expected_identity is not None and tuple(expected_identity) != identity:
            raise RetentionError('streamed source changed after validation')
        target = self.root / (digest + SUFFIXES[mime])
        existing = [path for path in paths if os.path.lexists(path)]
        if existing:
            if existing != [target]:
                raise RetentionError('missing or ambiguous retained asset')
            self._recover_staged_publication(target, digest, maximum_bytes=maximum,
                                             cancelled=cancelled)
            result = self.resolve(digest, profile=profile, cancelled=cancelled)
            if result.stat().st_size != size:
                raise RetentionError('retained asset size changed')
            sync_directory(self.root)
            return result
        check_cancelled(cancelled)
        if shutil.disk_usage(self.root).free < size + 16 * 1024**2:
            raise RetentionError('not enough disk space to retain the selected original')
        root_info = self.root.lstat()
        if (not stat.S_ISDIR(root_info.st_mode)
                or getattr(root_info, 'st_file_attributes', 0) & 0x400):
            raise RetentionError('the retained original directory changed')
        temporary = Path(mkdtemp(prefix='.carr-original-', dir=self.root))
        directory_info = temporary.lstat()
        stage = temporary / 'original'
        created, staged, publication_started = None, None, False

        def same_directory(path, expected):
            info = path.lstat()
            return (stat.S_ISDIR(info.st_mode)
                    and not getattr(info, 'st_file_attributes', 0) & 0x400
                    and os.path.samestat(info, expected))

        def same_payload(info, expected, links):
            return (stat.S_ISREG(info.st_mode) and info.st_nlink == links
                    and not getattr(info, 'st_file_attributes', 0) & 0x400
                    and os.path.samestat(info, expected)
                    and info.st_size == expected.st_size
                    and info.st_mtime_ns == expected.st_mtime_ns)

        try:
            hashed, copied = sha256(), 0
            with verified_reader(source, expected_identity=identity) as incoming, stage.open('xb') as output:
                created = os.fstat(output.fileno())
                while True:
                    check_cancelled(cancelled)
                    block = incoming.read(min(1024 * 1024, size + 1 - copied))
                    if not block:
                        break
                    copied += len(block)
                    if copied > size:
                        raise RetentionError('streamed source grew during retention')
                    hashed.update(block)
                    output.write(block)
                if copied != size or hashed.hexdigest() != digest:
                    raise RetentionError('streamed source checksum changed during retention')
                output.flush()
                os.fsync(output.fileno())
                staged = os.fstat(output.fileno())
            check_cancelled(cancelled)
            if (not same_directory(self.root, root_info)
                    or not same_directory(temporary, directory_info)
                    or not same_payload(stage.lstat(), staged, 1)
                    or file_digest(stage, maximum_bytes=maximum, cancelled=cancelled) != digest):
                raise RetentionError('staged original changed before publication')
            check_cancelled(cancelled)
            publication_started = True
            os.link(stage, target)
            # A cancellation after this commit cannot delete or relabel the
            # retained original. Complete only this owned pair; unknown links
            # or a replaced target remain intact for explicit recovery.
            if (not same_directory(self.root, root_info)
                    or not same_directory(temporary, directory_info)
                    or not same_payload(stage.lstat(), staged, 2)
                    or not same_payload(target.lstat(), staged, 2)
                    or file_digest(target, maximum_bytes=maximum, expected_links=2) != digest):
                raise RetentionError('retained original publication ownership changed')
            sync_directory(self.root)
            if (not same_payload(stage.lstat(), staged, 2)
                    or not same_payload(target.lstat(), staged, 2)):
                raise RetentionError('retained original links changed before staging release')
            stage.unlink()
            if (not same_directory(self.root, root_info)
                    or not same_payload(target.lstat(), staged, 1)
                    or file_digest(target, maximum_bytes=maximum) != digest):
                raise RetentionError('retained original changed after staging release')
            sync_directory(temporary)
            return self.resolve(digest, profile=profile, cancelled=lambda: False)
        finally:
            if not publication_started and created is not None and os.path.lexists(stage):
                info = stage.lstat()
                if (same_directory(temporary, directory_info)
                        and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                        and not getattr(info, 'st_file_attributes', 0) & 0x400
                        and os.path.samestat(info, created)):
                    stage.unlink()
            if (same_directory(temporary, directory_info)
                    and not any(temporary.iterdir())):
                temporary.rmdir()
                sync_directory(self.root)

    def _recover_staged_publication(self, target, digest, *, maximum_bytes=MAX_BYTES,
                                    cancelled=lambda: False):
        """Finish only a fully accounted link publication under the writer guard.

        A process can die after linking the target but before TemporaryDirectory
        removes its original. Readers still reject extra links. A writer may
        remove those exact staging twins only after validating the complete set;
        an unknown link or ambiguous directory leaves every entry untouched.
        """
        from .streamed_text import check_cancelled, file_digest
        check_cancelled(cancelled)
        published = target.lstat()
        if (not stat.S_ISREG(published.st_mode) or
                getattr(published, 'st_file_attributes', 0) & 0x400 or
                not 0 < published.st_size <= maximum_bytes):
            raise RetentionError('invalid retained asset file')
        if published.st_nlink == 1:
            return
        if published.st_nlink < 2 or not published.st_ino:
            raise RetentionError('invalid retained asset links')
        if file_digest(target, maximum_bytes=maximum_bytes,
                       expected_links=published.st_nlink, cancelled=cancelled) != digest:
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
            check_cancelled(cancelled)
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
        from carr_library.catalog.intake import snapshot
        asset = snapshot(source_root, relative)
        retained = self.retain(asset.data, asset.mime, asset.digest)
        job = ingest.start(workspace, edition, self.root, retained.name, **kwargs)
        if ingest.inspect(workspace, job)['asset'] != asset.digest:
            raise RetentionError('job asset mismatch')
        return job

    def run(self, ingest, workspace, job, **kwargs):
        """Scope-check via existing journal first; resume from original bytes."""
        record = ingest.inspect(workspace, job)
        retained = self.resolve(record['asset'])
        return ingest.run(workspace, job, self.root, retained.name, **kwargs)
