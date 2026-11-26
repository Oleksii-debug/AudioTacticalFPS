"""Assemble a hash-locked Windows candidate without executing Windows binaries.

Run on a trusted source checkout. Uses pip's wheel selection/hash checks and
the reviewed setuptools build backend. Output is an installation kit for an
existing Windows x64 Python 3.12, not a tested Windows installer or release.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

ROOT = Path(__file__).resolve().parents[1]

INSTALL = r'''@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
  echo Cannot open the candidate folder. Extract this package into an accessible local folder.
  pause
  exit /b 1
)
if exist "installed.ok" (
  echo This candidate is already installed. Use Start Research Library.cmd.
  pause
  exit /b 1
)
if exist "runtime" (
  echo A previous installation is incomplete. Preserve it for diagnosis and use a new candidate folder.
  pause
  exit /b 1
)
py -3.12 -c "import struct,sys;sys.exit(0 if struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo Install Python 3.12 for Windows x64 from python.org first.
  pause
  exit /b 1
)
py -3.12 -m venv runtime
if errorlevel 1 goto failed
"runtime\Scripts\python.exe" -m pip install --no-index --only-binary=:all: --require-hashes --find-links=wheelhouse -r requirements.lock
if errorlevel 1 goto failed
"runtime\Scripts\python.exe" -c "import wx,carr_library.application,carr_library.desktop"
if errorlevel 1 goto failed
echo verified>installed.ok
if errorlevel 1 goto failed
echo Installed. Your research data is kept separately in your user data folder.
echo Use Start Research Library.cmd to open the program.
pause
exit /b 0
:failed
echo Installation failed. The installation folder was preserved for diagnosis.
pause
exit /b 1
'''

START = r'''@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
  echo Cannot open the candidate folder. Extract this package into an accessible local folder.
  pause
  exit /b 1
)
if not exist "installed.ok" (
  echo Run Install Research Library.cmd first.
  pause
  exit /b 1
)
"runtime\Scripts\python.exe" -m carr_library gui
if errorlevel 1 (
  pause
  exit /b 1
)
exit /b 0
'''


def build(destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    with TemporaryDirectory(prefix='carr-windows-build-', dir=destination.parent) as temporary:
        stage = Path(temporary)
        wheels = stage / 'wheelhouse'
        wheels.mkdir()
        lock = ROOT / 'packaging' / 'requirements-windows.lock'
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:',
                        '--platform', 'win_amd64', '--python-version', '3.12',
                        '--implementation', 'cp', '--abi', 'cp312', '--require-hashes',
                        '--dest', str(wheels), '-r', str(lock)], check=True)
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps',
                        '--wheel-dir', str(wheels), str(ROOT)], check=True)
        application, = wheels.glob('carr_research_library-*.whl')
        with zipfile.ZipFile(application) as archive:
            required = {'carr_library/__main__.py', 'carr_library/desktop.py',
                        'carr_library/application.py',
                        'bootstrap/vendor/cairn_extractors/LICENSE',
                        'carr_library/ui/_vendor/wx_grid/LICENSE',
                        'carr_library/search/_vendor/rifflux/LICENSE',
                        'carr_library/research/_vendor_rispy/LICENSE'}
            if not required <= set(archive.namelist()):
                raise ValueError('application wheel lacks runtime or license files')
        app_hash = sha256(application.read_bytes()).hexdigest()
        requirements = lock.read_text(encoding='utf-8')
        requirements += '\ncarr-research-library==0.1.0 --hash=sha256:' + app_hash + '\n'
        (stage / 'requirements.lock').write_text(requirements, encoding='utf-8')
        (stage / 'Install Research Library.cmd').write_text(INSTALL, encoding='utf-8', newline='\r\n')
        (stage / 'Start Research Library.cmd').write_text(START, encoding='utf-8', newline='\r\n')
        (stage / 'README.txt').write_text(
            'Research Library 0.1.0 development candidate\n\n'
            'Requires Windows x64 and Python 3.12. Run Install Research Library.cmd, '
            'then Start Research Library.cmd. Installation uses only included wheels.\n'
            'Research data is stored separately in the current user data directory.\n'
            'TXT exact citations, libraries, topics, notes, export and backup are available.\n'
            'Unverified PDF previews require an explicit Sources selection and cannot be cited.\n'
            'Neural search is not included in this installation kit: it needs the separately '
            'installed semantic runtime and a verified model bundle.\n'
            'Windows installation and NVDA acceptance have not been executed in this build.\n',
            encoding='utf-8')
        manifest = {'format': 1, 'state': 'DEVELOPMENT_CANDIDATE',
                    'target': 'CPython 3.12 / Windows x64', 'native_execution': 'NOT_RUN',
                    'files': {str(path.relative_to(stage)).replace('\\', '/'):
                              sha256(path.read_bytes()).hexdigest()
                              for path in sorted(stage.rglob('*')) if path.is_file()}}
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n',
                                             encoding='utf-8')
        # Caller chooses a fresh path; no installed program or library is replaced.
        destination.mkdir()
        for source in stage.iterdir():
            target = destination / source.name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copyfile(source, target)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(build(args.destination))


if __name__ == '__main__':
    main()
