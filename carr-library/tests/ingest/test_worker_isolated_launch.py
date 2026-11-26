"""Real import and render subprocesses ignore cwd/PYTHONPATH substitute packages.

Both source-checkout and installed layouts execute the current shared supervisor.
The parent imports explicitly trusted application roots before adversarial cwd
and environment values are supplied. Substitute modules only write a harmless
marker and fail; no user data or shared interpreter environment is changed.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from carr_library.ingest import worker


SOURCE_ROOT = Path(worker.__file__).resolve().parents[2]
PROJECT_ROOT = SOURCE_ROOT.parent


def _poison_package(root, package, marker):
    target = root / package
    target.mkdir(parents=True)
    code = ('from pathlib import Path\n'
            f'Path({str(marker)!r}).write_text("untrusted import path executed")\n'
            'raise RuntimeError("untrusted import path executed")\n')
    (target / '__init__.py').write_text(code, encoding='utf-8')


@pytest.mark.parametrize('parent_isolated', [False, True])
@pytest.mark.parametrize('layout', ['source', 'installed'])
def test_real_import_and_render_ignore_substitute_modules(tmp_path, parent_isolated, layout):
    from reportlab.pdfgen.canvas import Canvas

    if layout == 'installed':
        installed = tmp_path / 'installed-site'
        shutil.copytree(SOURCE_ROOT / 'carr_library', installed / 'carr_library',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(PROJECT_ROOT / 'bootstrap', installed / 'bootstrap',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        roots = [str(installed)]
    else:
        roots = [str(SOURCE_ROOT), str(PROJECT_ROOT)]

    source = tmp_path / 'Оригінал Ω.pdf'
    canvas = Canvas(str(source))
    canvas.drawString(72, 700, 'Isolated railway source.')
    canvas.save()

    working = tmp_path / 'untrusted-working-directory'
    pythonpath = tmp_path / 'untrusted-pythonpath'
    markers = []
    for number, location in enumerate((working, pythonpath)):
        location.mkdir()
        for package in ('carr_library', 'bootstrap', 'pypdfium2'):
            marker = tmp_path / f'poison-{number}-{package}.txt'
            markers.append(marker)
            _poison_package(location, package, marker)
        marker = tmp_path / f'poison-{number}-sitecustomize.txt'
        markers.append(marker)
        (location / 'sitecustomize.py').write_text(
            'from pathlib import Path\n'
            f'Path({str(marker)!r}).write_text("untrusted sitecustomize executed")\n',
            encoding='utf-8')

    parent = tmp_path / 'trusted_parent.py'
    parent.write_text(
        'import json, os, sys\nfrom pathlib import Path\n'
        f'sys.path[:0] = {roots!r}\n'
        'from carr_library.application import LibraryApp\n'
        'from carr_library.ingest.worker import isolated_render_fingerprint\n'
        'from carr_library.citations.pdf_review_attestation import render_fingerprint\n'
        f'os.chdir({str(working)!r})\n'
        f'os.environ["PYTHONPATH"] = {str(pythonpath)!r}\n'
        'os.environ["PYTHONUTF8"] = "0"\n'
        'os.environ["PYTHONIOENCODING"] = "ascii"\n'
        f'source = Path({str(source)!r})\n'
        f'app = LibraryApp(Path({str(tmp_path / "private-data")!r}))\n'
        'workspace = app.create_workspace("Ізольована бібліотека Ω")\n'
        'result = app.import_file(workspace, source, title="Точне джерело Ω")\n'
        'if result.get("state") != "complete":\n'
        '    raise RuntimeError("Real child import failed: " + repr(result))\n'
        'hit, = app.search(workspace, "Isolated railway")\n'
        'expected = render_fingerprint(source.read_bytes(), 1)\n'
        'rendered = isolated_render_fingerprint(source.read_bytes(), 1)\n'
        'print(json.dumps({"parent_isolated": sys.flags.isolated, '
        '"state": result["state"], "isolated": result["isolated"], '
        '"title": app.browse(workspace)[0].title, "quote": hit.quote, '
        '"rendered": rendered, "expected": expected}, ensure_ascii=False))\n',
        encoding='utf-8')
    # The source-mode parent starts clean, then supplies poison only to its
    # children. Otherwise Python could legitimately run sitecustomize before
    # the production supervisor has any opportunity to establish its boundary.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('PYTHON')}
    command = [sys.executable, '-B', '-X', 'utf8']
    if parent_isolated:
        command.append('-I')
    command.append(str(parent))
    run = subprocess.run(command, cwd=tmp_path, env=environment,
                         capture_output=True, text=True, encoding='utf-8',
                         timeout=60, check=True)
    record = json.loads(run.stdout)
    assert record['parent_isolated'] == int(parent_isolated)
    assert record['state'] == 'complete' and record['isolated'] is True
    assert record['title'] == 'Точне джерело Ω'
    assert record['quote'] == 'Isolated railway'
    assert record['rendered'] == record['expected']
    assert record['rendered'][0]
    assert len(record['rendered'][1]) == 64
    assert all(not marker.exists() for marker in markers)
