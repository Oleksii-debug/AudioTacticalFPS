"""Real local CLI/model tests; source fixtures isolate fingerprint boundary."""
from dataclasses import dataclass, replace
from pathlib import Path
import json
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest
from carr_library.extract import runtime_identity as ri
from carr_library.extract.pinned_tessdata import MODELS, OSD_MODEL, POLICY


@dataclass(frozen=True)
class Settings:
    languages: str = 'eng'
    timeout: int = 30
    orientation: bool = False
    model_policy: str = 'default'


@pytest.fixture(autouse=True)
def source_fixture(tmp_path):
    # Exercise real file hashing with synthetic source files; the pinned
    # model staging module is the exact upstream candidate, not a double.
    for name in ('page_ocr.py', 'orientation.py', 'ocr_orientation_bridge.py',
                 'pinned_tessdata.py', 'runtime_identity.py'):
        (tmp_path / name).write_bytes(name.encode())
    with patch.object(ri, '__file__', str(tmp_path / 'runtime_identity.py')):
        yield tmp_path


def test_real_pinned_multilingual_and_osd():
    if not shutil.which('tesseract'):
        pytest.skip('Tesseract unavailable')
    cfg = Settings('eng+ukr+slk', orientation=True, model_policy=POLICY)
    a = ri.runtime_identity(cfg)
    assert a == ri.runtime_identity(cfg)
    assert a['ocr']['model_sha256'] == {**{k: v[1] for k, v in MODELS.items()}, 'osd': OSD_MODEL[1]}
    assert a['ocr']['version'].startswith('tesseract ')
    assert str(Path(shutil.which('tesseract')).parent) not in json.dumps(a)
    assert len(a['ocr']['executable_sha256']) == 64
    assert a != ri.runtime_identity(replace(cfg, orientation=False))


def test_real_default_model_change_and_pinned_rejection(tmp_path, monkeypatch):
    exe = shutil.which('tesseract')
    if not exe:
        pytest.skip('Tesseract unavailable')
    report = subprocess.check_output([exe, '--list-langs'], text=True)
    origin = Path(re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.M)[1])
    dest = tmp_path / 'models'
    dest.mkdir()
    shutil.copyfile(origin / 'eng.traineddata', dest / 'eng.traineddata')
    monkeypatch.setenv('TESSDATA_PREFIX', str(dest))
    a = ri.runtime_identity(Settings())
    assert a['ocr']['model_sha256']['eng'] == MODELS['eng'][1]
    with (dest / 'eng.traineddata').open('ab') as f:
        f.write(b'synthetic change')
    b = ri.runtime_identity(Settings())
    assert a != b
    with pytest.raises(ri.IdentityError):
        ri.runtime_identity(Settings(model_policy=POLICY))
    (dest / 'eng.traineddata').unlink()
    with pytest.raises(ri.IdentityError):
        ri.runtime_identity(Settings())


def test_missing_engine_and_source_change(source_fixture):
    with patch.object(ri.shutil, 'which', return_value=None):
        a = ri.runtime_identity(Settings())
        assert a['ocr'] == {'state': 'missing'}
        (source_fixture / 'page_ocr.py').write_bytes(b'new adapter behavior')
        assert a != ri.runtime_identity(Settings())
        assert a != ri.runtime_identity(Settings(timeout=10))


def test_resource_limit(tmp_path):
    p = tmp_path / 'bounded'
    p.write_bytes(b'12345')
    with pytest.raises(ri.IdentityError):
        ri._hash(p, limit=4)


def test_engine_version_change_detected():
    # Only version response is injected; no unknown executable is run.
    with patch.object(ri, '_hash', return_value='a'*64), \
         patch.object(ri.shutil, 'which', return_value='trusted-fixture'):
        def result(version):
            with patch.object(ri.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, stdout=version.encode()),
                subprocess.CompletedProcess([], 0, stdout=b'List of available languages in "fixture" (1):\neng\n')]):
                return ri.runtime_identity(Settings())
        assert result('tesseract 5.5.0') != result('tesseract 5.6.0')
