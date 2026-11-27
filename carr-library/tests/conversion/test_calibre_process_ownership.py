"""Real child processes prove the adapter inherits the existing native family.

The executables here are Python fixtures. This does not qualify any Calibre
installation or document format; that is the separate acceptance script.
"""
import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

import pytest

from carr_library.audio.process import run_process
from carr_library.audio.models import AudioBudget, AudioCleanupPending


def _alive(pid):
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # No such process.
                return False
            raise ctypes.WinError(error)
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258
        finally:
            assert kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path('/proc') / str(pid) / 'stat'
    if status.is_file():
        try:
            text = status.read_text()
        except FileNotFoundError:
            return False
        return text.rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
    return True


@pytest.mark.parametrize('mode', ['completed-leader', 'diagnostic-cap', 'adapter-timeout'])
def test_inner_converter_keeps_descendant_in_existing_outer_owner(tmp_path, mode):
    import carr_library
    package_file = str(Path(carr_library.__file__).resolve())
    selected_root = Path(package_file).parent
    selected_proof = tmp_path / 'selected-package-proof.json'
    selected_proof.write_text(json.dumps({
        'source_selection': selected_root == (Path(__file__).parents[2] / 'src' / 'carr_library').resolve(),
        'files': {path.relative_to(selected_root).as_posix(): sha256(path.read_bytes()).hexdigest()
                  for path in selected_root.rglob('*.py')},
    }, sort_keys=True), encoding='utf-8')
    ready = tmp_path / 'descendant.pid'
    child = (
        "import os,pathlib,subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(12)'])\n"
        "marker=pathlib.Path(sys.argv[1]);temporary=marker.with_suffix('.tmp')\n"
        "temporary.write_text(str(p.pid));os.replace(temporary,marker)\n"
        "mode=sys.argv[2]\n"
        "if mode=='diagnostic-cap':\n"
        " sys.stdout.buffer.write(b'x'*300000);sys.stdout.buffer.flush();time.sleep(8)\n"
        "elif mode=='adapter-timeout': time.sleep(8)\n"
    )
    outer = (
        "import json,pathlib,sys,sysconfig,time,carr_library\n"
        "from hashlib import sha256\n"
        "expected=pathlib.Path(sys.argv[1]).resolve().parent\n"
        "actual=pathlib.Path(carr_library.__file__).resolve().parent\n"
        "proof=json.loads(pathlib.Path(sys.argv[5]).read_text(encoding='utf-8'))\n"
        "allowed={expected}\n"
        "if proof['source_selection'] is True:\n"
        " allowed.add((pathlib.Path(sysconfig.get_path('purelib'))/'carr_library').resolve())\n"
        "assert actual in allowed,(actual,expected)\n"
        "files={path.relative_to(actual).as_posix():sha256(path.read_bytes()).hexdigest() "
        "for path in actual.rglob('*.py')}\n"
        "assert files==proof['files'],'Selected child package differs from the parent file/hash inventory.'\n"
        "from carr_library.conversion import calibre\n"
        "assert pathlib.Path(calibre.__file__).resolve()==actual/'conversion'/'calibre.py'\n"
        "_run,_environment=calibre._run,calibre._environment\n"
        "stage=pathlib.Path(sys.argv[2]);mode=sys.argv[3]\n"
        "real=time.monotonic;deadline=real()+5\n"
        "if mode=='adapter-timeout':\n"
        " time.monotonic=lambda: deadline if (stage/'descendant.pid').is_file() else real()\n"
        "try:\n"
        " _run([sys.executable,'-I','-c',sys.argv[4],str(stage/'descendant.pid'),mode],"
        "stage,deadline,"
        "environment=_environment(stage,stage))\n"
        "except (ValueError,TimeoutError):\n"
        " assert mode!='completed-leader'\n"
        "else: assert mode=='completed-leader'\n"
        "assert (stage/'descendant.pid').is_file()\n"
        "print('adapter settled')\n"
    )
    # Ordinary child imports may select this interpreter's exact installed copy
    # when pytest selected checkout src. Bind its complete bytes before native
    # work; installed-parent mode stays on the exact configured root.
    args = [str(Path(sys.executable).absolute()), '-B', '-c', outer,
            package_file, str(tmp_path), mode, child, str(selected_proof)]
    try:
        result = run_process(args, owned_tree=True, cwd=tmp_path, watch=(tmp_path,),
            budget=AudioBudget(timeout=15, max_log_bytes=1024 * 1024,
                               max_output_bytes=2 * 1024 * 1024))
    except AudioCleanupPending as error:
        assert error.cleanup_owner.quiescent_event.wait(20), 'Owned descendant cleanup remained pending.'
        raise
    assert 'adapter settled' in result.stdout.decode('utf-8')
    pid = int(ready.read_text())
    assert not _alive(pid), 'An inner Calibre-style child escaped the existing native family.'
