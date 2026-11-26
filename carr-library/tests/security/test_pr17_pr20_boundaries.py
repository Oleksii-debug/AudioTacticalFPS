"""Independent synthetic-only regression probes; run against PR17 and PR20 source roots."""
import hashlib
import os
import sqlite3
from zipfile import ZipFile

import pytest
from bootstrap.universal_library import ResearchLibrary
from bootstrap.external_reuse import import_external, _check_zip
from carr_library.catalog.intake import snapshot, IntakeError

@pytest.fixture
def lib(tmp_path):
    db = ResearchLibrary(tmp_path / 'library.db')
    try:
        yield db
    finally:
        db.close()

def test_query_and_workspace_isolation(lib):
    a,b = lib.workspace('A'),lib.workspace('B')
    lib.add_book(a,'Same','A','1',[(None,'public token')])
    lib.add_book(b,'Same','B','1',[(None,'private synthetic token')])
    assert lib.search(a,'private') == []
    assert lib.search(a,'token')[0]['author'] == 'A'
    assert lib.search(a,'" OR private OR "') == []
    hit = lib.search(b,'private')[0]
    with pytest.raises(ValueError):
        lib.save(a,lib.topic(a,'Topic'),hit)
    assert lib.export_topic(a,lib.topic(b,'Topic')) == '[]'

@pytest.mark.parametrize('name',['../outside.xml','/absolute.xml','C:/drive.xml','folder\\..\\escape.xml'])
def test_pr17_archive_path_rejected(tmp_path,name):
    path=tmp_path/'test.docx'
    with ZipFile(path,'w') as z:
        z.writestr(name,'synthetic')
    with pytest.raises(ValueError,match='unsafe archive member'):
        _check_zip(path)

def test_pr17_html_script_not_indexed(tmp_path,lib):
    p=tmp_path/'test.html'
    p.write_text('<p>ordinary visible text</p><script>hiddenmarker</script>',encoding='utf-8')
    w=lib.workspace('A')
    import_external(lib,w,p,'T','A','E')
    assert lib.search(w,'ordinary')
    assert not lib.search(w,'hiddenmarker')

def test_pr20_snapshot_rejects_symlink(tmp_path):
    p=tmp_path/'source.txt'; p.write_text('synthetic',encoding='utf-8')
    link=tmp_path/'link.txt'
    try:
        link.symlink_to(p)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(IntakeError):
        snapshot(tmp_path,'link.txt')

def test_pr20_snapshot_is_immutable(tmp_path):
    p=tmp_path/'source.txt'; p.write_bytes(b'original')
    s=snapshot(tmp_path,'source.txt');p.write_bytes(b'changed')
    assert s.data==b'original' and s.digest==hashlib.sha256(b'original').hexdigest()

@pytest.mark.parametrize('suffix',['.txt','.html'])
def test_pr17_intake_must_reject_symlink(tmp_path,lib,suffix):
    # Release requirement, not a claim of bypass of a production sandbox.
    root=tmp_path/'staging';root.mkdir()
    p=tmp_path/('outside'+suffix);p.write_text('synthetic marker',encoding='utf-8')
    link=root/('source'+suffix)
    try:
        link.symlink_to(p)
    except OSError:
        pytest.skip('symlinks unavailable')
    w=lib.workspace('A')
    with pytest.raises((ValueError,OSError)):
        import_external(lib,w,link,'T','A','E')
    assert not lib.search(w,'marker')

def test_pr17_native_route_must_consume_bounded_snapshot(tmp_path,lib,monkeypatch):
    # Mock a stale stat on a regular synthetic file; no large data allocation.
    from pathlib import Path
    import bootstrap.external_reuse as adapter
    p=tmp_path/'source.txt';p.write_text('0123456789',encoding='utf-8')
    monkeypatch.setattr(adapter,'MAX_BYTES',5)
    real_stat=Path.stat
    class StaleStat:
        def __init__(self, real): self.real=real
        @property
        def st_size(self): return 1
        def __getattr__(self,name): return getattr(self.real,name)
    def fake_stat(self,*a,**kw):
        r=real_stat(self,*a,**kw)
        return StaleStat(r) if self==p else r
    monkeypatch.setattr(Path,'stat',fake_stat)
    w=lib.workspace('A')
    with pytest.raises(ValueError):
        import_external(lib,w,p,'T','A','E')
