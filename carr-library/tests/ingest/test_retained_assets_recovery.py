"""Crash recovery of retained originals, without general staging cleanup."""
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from carr_library.ingest.retained_assets import RetainedAssets, RetentionError
from carr_library.ingest.writer_guard import exclusive_writer


DATA = 'Original railway source.\r\nУкраїна\fSecond segment.'.encode('utf-8')
DIGEST = sha256(DATA).hexdigest()


@pytest.fixture
def private(tmp_path):
    library = tmp_path / 'private library'
    library.mkdir()
    assets = library / 'originals'
    assets.mkdir()
    return library, RetainedAssets(assets)


def abandoned(store):
    """The exact on-disk state between link publication and temp cleanup."""
    directory = Path(tempfile.mkdtemp(prefix='.carr-original-', dir=store.root))
    original = directory / 'original'
    original.write_bytes(DATA)
    target = store.root / (DIGEST + '.txt')
    os.link(original, target)
    return directory, original, target


def retain(private, data=DATA, mime='text/plain', digest=DIGEST):
    library, store = private
    with exclusive_writer(library):
        return store.retain(data, mime, digest)


def child(private, body):
    library, store = private
    project = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join((str(project / 'src'), str(project)))
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    code = '''
import os, sys
from pathlib import Path
from carr_library.ingest.retained_assets import RetainedAssets
from carr_library.ingest.writer_guard import exclusive_writer
library, assets, digest, text = sys.argv[1:]
data = bytes.fromhex(text)
''' + body
    return subprocess.run([sys.executable, '-c', code, str(library), str(store.root),
                           DIGEST, DATA.hex()], env=environment, check=False,
                          capture_output=True, timeout=15)


def test_process_exit_after_link_recovers_without_source_loss(private):
    _, store = private
    process = child(private, '''
real_link = os.link
def crash_after_link(source, target):
    real_link(source, target)
    os._exit(77)
os.link = crash_after_link
with exclusive_writer(library):
    RetainedAssets(assets).retain(data, 'text/plain', digest)
''')
    assert process.returncode == 77, process.stderr.decode(errors='replace')
    target = store.root / (DIGEST + '.txt')
    directories = tuple(store.root.glob('.carr-original-*'))
    assert len(directories) == 1 and target.stat().st_nlink == 2
    original = directories[0] / 'original'
    assert original.samefile(target) and target.read_bytes() == DATA

    # Read-only lookup must neither repair nor bless a hardlinked original.
    with pytest.raises(RetentionError, match='invalid retained asset file'):
        store.resolve(DIGEST)
    assert original.exists() and target.stat().st_nlink == 2

    assert retain(private) == target
    assert target.read_bytes() == DATA and target.stat().st_nlink == 1
    assert tuple(store.root.iterdir()) == (target,)
    assert store.resolve(DIGEST) == target
    assert retain(private) == target
    assert tuple(store.root.iterdir()) == (target,)


def test_multiple_accounted_twins_recover_together(private):
    _, store = private
    first, _, target = abandoned(store)
    second = Path(tempfile.mkdtemp(prefix='.carr-original-', dir=store.root))
    os.link(target, second / 'original')
    assert target.stat().st_nlink == 3
    assert retain(private) == target
    assert target.stat().st_nlink == 1 and target.read_bytes() == DATA
    assert not first.exists() and not second.exists()


def test_recovery_process_exit_can_retry_remaining_twin(private):
    _, store = private
    first, _, target = abandoned(store)
    second = Path(tempfile.mkdtemp(prefix='.carr-original-', dir=store.root))
    os.link(target, second / 'original')
    process = child(private, '''
real_unlink = Path.unlink
def crash_after_unlink(path, *args, **kwargs):
    result = real_unlink(path, *args, **kwargs)
    if path.name == 'original':
        os._exit(78)
    return result
Path.unlink = crash_after_unlink
with exclusive_writer(library):
    RetainedAssets(assets).retain(data, 'text/plain', digest)
''')
    assert process.returncode == 78, process.stderr.decode(errors='replace')
    assert target.stat().st_nlink == 2
    empty = next(directory for directory in (first, second)
                 if not tuple(directory.iterdir()))
    assert retain(private) == target
    assert target.read_bytes() == DATA and target.stat().st_nlink == 1
    # No generalized cleanup: an empty directory cannot be bound to this asset.
    assert empty.is_dir() and not tuple(empty.iterdir())
    assert retain(private) == target


@pytest.mark.parametrize('known_twin', [False, True])
def test_external_hardlink_rejected_before_any_deletion(private, tmp_path, known_twin):
    _, store = private
    if known_twin:
        directory, original, target = abandoned(store)
    else:
        directory = original = None
        target = retain(private)
    external = tmp_path / 'outside-original'
    os.link(target, external)
    count = target.stat().st_nlink
    with pytest.raises(RetentionError, match='unaccounted retained asset links'):
        retain(private)
    assert target.stat().st_nlink == count
    assert external.samefile(target) and external.read_bytes() == DATA
    if directory is not None:
        assert tuple(directory.iterdir()) == (original,)
        assert original.samefile(target)


@pytest.mark.parametrize('entry_kind', ['file', 'directory'])
def test_extra_staging_entry_preserves_every_twin(private, entry_kind):
    _, store = private
    directory, original, target = abandoned(store)
    extra = directory / 'unexpected'
    extra.write_bytes(b'keep me') if entry_kind == 'file' else extra.mkdir()
    other = Path(tempfile.mkdtemp(prefix='.carr-original-', dir=store.root))
    other_original = other / 'original'
    os.link(target, other_original)
    with pytest.raises(RetentionError, match='ambiguous retained asset staging'):
        retain(private)
    assert target.stat().st_nlink == 3
    assert original.samefile(target) and other_original.samefile(target)
    assert extra.exists() and target.read_bytes() == DATA


def symlink_or_skip(source, target, *, directory=False):
    try:
        target.symlink_to(source, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f'symbolic links unavailable: {exc}')


def test_symlinked_staging_directory_is_not_followed(private, tmp_path):
    _, store = private
    directory, original, target = abandoned(store)
    outside = tmp_path / 'moved-original-stage'
    directory.rename(outside)
    symlink_or_skip(outside, directory, directory=True)
    with pytest.raises(RetentionError, match='invalid retained asset staging directory'):
        retain(private)
    assert directory.is_symlink() and (outside / 'original').samefile(target)
    assert target.stat().st_nlink == 2 and target.read_bytes() == DATA


def test_symlinked_staging_original_is_not_followed(private, tmp_path):
    _, store = private
    directory, original, target = abandoned(store)
    outside = tmp_path / 'moved-original'
    original.rename(outside)
    symlink_or_skip(outside, original)
    with pytest.raises(RetentionError, match='invalid retained asset staging original'):
        retain(private)
    assert original.is_symlink() and outside.samefile(target)
    assert target.stat().st_nlink == 2 and target.read_bytes() == DATA


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permission bits required')
@pytest.mark.parametrize('mode', [0o755, 0o500, 0o1700])
def test_nonprivate_staging_directory_is_not_modified(private, mode):
    _, store = private
    directory, original, target = abandoned(store)
    directory.chmod(mode)
    with pytest.raises(RetentionError, match='invalid retained asset staging directory'):
        retain(private)
    assert original.samefile(target) and target.stat().st_nlink == 2
    assert directory.stat().st_mode & 0o7777 == mode


@pytest.mark.parametrize('entry', ['target', 'directory', 'original'])
def test_reparse_point_attributes_refuse_before_cleanup(private, monkeypatch, entry):
    _, store = private
    directory, original, target = abandoned(store)
    selected = {'target': target, 'directory': directory, 'original': original}[entry]
    original_lstat = Path.lstat

    def reparse_lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path != selected:
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
        fields['st_file_attributes'] = fields.get('st_file_attributes', 0) | 0x400
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, 'lstat', reparse_lstat)
    with pytest.raises(RetentionError):
        retain(private)
    assert tuple(directory.iterdir()) == (original,)
    assert original.samefile(target) and target.stat().st_nlink == 2
    assert target.read_bytes() == DATA


def test_unknown_staging_name_is_not_cleaned(private):
    _, store = private
    directory, original, target = abandoned(store)
    renamed = directory.with_name('.carr-original-unrecognized-long-name')
    directory.rename(renamed)
    with pytest.raises(RetentionError, match='unaccounted retained asset links'):
        retain(private)
    assert (renamed / 'original').samefile(target)
    assert target.stat().st_nlink == 2


def test_same_bytes_other_inode_is_not_an_accounted_twin(private, tmp_path):
    _, store = private
    directory, original, target = abandoned(store)
    outside = tmp_path / 'real-twin'
    original.rename(outside)
    original.write_bytes(DATA)
    with pytest.raises(RetentionError, match='unaccounted retained asset links'):
        retain(private)
    assert not original.samefile(target) and original.read_bytes() == DATA
    assert outside.samefile(target) and target.stat().st_nlink == 2


@pytest.mark.parametrize('failure', ['payload', 'published_bytes', 'mime', 'ambiguous_mime'])
def test_mismatched_bytes_and_types_refuse_before_cleanup(private, failure):
    _, store = private
    directory, original, target = abandoned(store)
    options = {}
    if failure == 'payload':
        options['data'] = b'Different submitted snapshot'
    elif failure == 'published_bytes':
        target.write_bytes(b'Changed retained bytes')
    elif failure == 'mime':
        options['mime'] = 'application/pdf'
    else:
        target.with_suffix('.pdf').write_bytes(DATA)
    before = target.read_bytes()
    names = set(store.root.iterdir())
    with pytest.raises(RetentionError):
        retain(private, **options)
    assert set(store.root.iterdir()) == names
    assert tuple(directory.iterdir()) == (original,)
    assert original.samefile(target) and target.stat().st_nlink == 2
    assert target.read_bytes() == before


def test_unrelated_private_staging_original_is_preserved(private):
    _, store = private
    _, _, target = abandoned(store)
    unrelated = Path(tempfile.mkdtemp(prefix='.carr-original-', dir=store.root))
    other_source = unrelated / 'original'
    other_source.write_bytes(b'Unpublished separate source')
    assert retain(private) == target
    assert target.stat().st_nlink == 1
    assert other_source.read_bytes() == b'Unpublished separate source'
