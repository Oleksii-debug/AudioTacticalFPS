"""Actual FIFO/symlink swaps in an owned isolated child; no path injection."""
from hashlib import sha256
import json
from pathlib import Path
import sys
import sysconfig

import carr_library
import pytest

from carr_library.audio.models import AudioBudget
from carr_library.audio.process import run_process
from carr_library.speech import playlist


@pytest.mark.parametrize('kind', ['fifo', 'symlink'])
@pytest.mark.parametrize('operation', ['review', 'retain', 'retained-digest', 'saved-state'])
def test_playlist_opener_rejects_substitution_before_consuming_foreign_bytes(tmp_path, kind, operation):
    root = Path(carr_library.__file__).resolve().parent
    roots = [str(root)]
    checkout = Path(__file__).resolve().parents[2] / 'src' / 'carr_library'
    if root == checkout:
        roots.append(str((Path(sysconfig.get_path('purelib')) / 'carr_library').resolve()))
    inventory = {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob('*.py')}
    proof = tmp_path / 'selected-source.json'
    proof.write_text(json.dumps({'roots': sorted(set(roots)), 'files': inventory},
                               sort_keys=True), encoding='utf-8')
    code = r'''
from hashlib import sha256
import faulthandler, json, os, sys, wave
from pathlib import Path
faulthandler.dump_traceback_later(15, exit=True)
proof = Path(sys.argv[1]).read_bytes()
assert sha256(proof).hexdigest() == sys.argv[2]
proof = json.loads(proof)
import carr_library
from carr_library.speech import playlist, listening
from carr_library.studio import clips, backup_inputs
from carr_library.studio.render import ProductionService
from carr_library.audio import AudioRuntime
from carr_library.durable_state import encode, regular
selected = Path(carr_library.__file__).resolve().parent
assert str(selected) in proof['roots']
actual = {p.relative_to(selected).as_posix(): sha256(p.read_bytes()).hexdigest()
          for p in selected.rglob('*.py')}
assert actual == proof['files']
for module, relative in ((playlist,'speech/playlist.py'), (listening,'speech/listening.py'),
                         (clips,'studio/clips.py'), (backup_inputs,'studio/backup_inputs.py')):
    assert Path(module.__file__).resolve() == selected / relative
folder, kind, operation = Path(sys.argv[3]), sys.argv[4], sys.argv[5]
source, foreign = folder/'selected.wav', folder/'foreign.wav'
for path, sample in ((source,b'\0\0'),(foreign,b'\1\0')):
    with wave.open(str(path),'wb') as stream:
        stream.setparams((1,2,8000,0,'NONE','PCM'))
        stream.writeframes(sample * 8000)
foreign_before = foreign.read_bytes()
if operation == 'saved-state':
    source.write_bytes(encode(playlist.SavedPlaylist().to_dict()))
before = regular(source)
opened = []
original = playlist._open_json_file if operation == 'saved-state' else listening._open_stream
def substituted(path):
    if path == source:
        opened.append(True)
        path.unlink()
        if kind == 'fifo':
            os.mkfifo(path)
        else:
            path.symlink_to(foreign)
    return original(path)
if operation == 'saved-state':
    playlist._open_json_file = substituted
else:
    listening._open_stream = substituted
service = editor = None
try:
    try:
        if operation == 'review':
            playlist.prepare_playlist((source,))
        elif operation == 'retained-digest':
            backup_inputs._checked_digest(source, before, lambda:False)
        elif operation == 'saved-state':
            playlist._read_snapshot(source)
        else:
            service = ProductionService(folder/'library/state/studio', runtime_factory=AudioRuntime)
            editor = clips.ClipEditor(folder/'library', service=service)
            editor.retain(source)
    except (ValueError,OSError):
        pass
    else:
        raise AssertionError('A substituted queue file reached its consumer.')
    assert opened == [True]
    assert foreign.read_bytes() == foreign_before
    if service is not None:
        assert not any(path.suffix == '.audio' for path in service.inputs.iterdir())
        from filelock import FileLock
        with FileLock(service.inputs/'.selected-clip.lock', timeout=0, thread_local=False):
            pass  # The exact original retention lock was released.
finally:
    if editor is not None:
        assert editor.close() is not False
    if service is not None:
        assert service.close() is not False
faulthandler.cancel_dump_traceback_later()
sys.stdout.buffer.write(b'playlist-boundary-refused\n')
'''
    result = run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-X', 'utf8',
        '-c', code, str(proof), sha256(proof.read_bytes()).hexdigest(), str(tmp_path), kind, operation],
        cwd=tmp_path, owned_tree=True,
        budget=AudioBudget(timeout=20, max_log_bytes=32768, reserve_disk_bytes=0))
    assert result.stdout == b'playlist-boundary-refused\n' and result.stderr == b''
