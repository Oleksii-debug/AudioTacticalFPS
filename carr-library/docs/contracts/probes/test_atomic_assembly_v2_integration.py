"""Independent assembly probe: exact PR21/53/57 modules, synthetic files only."""
import sqlite3
from dataclasses import replace
from contextlib import closing
import pytest
from carr_library.storage.revisions import Storage, Page
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch


def record(ws, edition='e', work='w', collections=('c',)):
    return Registration(work, edition, dict(sha256=ws*64,mime='text/plain',size=3),
                        'Same title',(f'Author {ws}',),'uk',collections=collections)


@pytest.fixture
def library(tmp_path):
    path = tmp_path/'library.db'
    with closing(Storage(path,schema_version=2)) as s:
        with s.transaction():
            for ws in ('a','b'):
                s.db.execute('INSERT INTO workspace VALUES(?)',(ws,))
                c=Catalog(s.db,ws)
                c.create_collection('c','Research')
                c.create_collection('extra','Other')
                register_batch(c,(record(ws),))
        yield s,path


def stage(s,ws,rev,raw,edition='e'):
    s.stage_revision(ws,edition,rev,ws*64,'synthetic',[Page(raw,3,'iv',True)])


def test_atomic_visibility(library):
    s,path=library
    with s.transaction():
        register_batch(Catalog(s.db,'a'),(record('a','new','new'),))
        stage(s,'a','r','unique phrase','new')
        with closing(sqlite3.connect(path)) as other:
            assert other.execute("SELECT count(*) FROM catalog_edition WHERE id='new'").fetchone()[0]==0
            assert other.execute("SELECT count(*) FROM revision WHERE id='r'").fetchone()[0]==0
    with closing(sqlite3.connect(path)) as other:
        assert other.execute("SELECT count(*) FROM catalog_edition WHERE id='new'").fetchone()[0]==1
        assert other.execute("SELECT count(*) FROM revision WHERE id='r'").fetchone()[0]==1
    s.check()


def test_rollback_entire_import(library):
    s,_=library
    with pytest.raises(ValueError,match='invalid raw page'):
        with s.transaction():
            register_batch(Catalog(s.db,'a'),(record('a','new','new'),))
            s.stage_revision('a','new','bad','a'*64,'synthetic',[Page('valid'),Page('bad\x00')])
    assert s.db.execute("SELECT count(*) FROM catalog_edition WHERE id='new'").fetchone()[0]==0
    assert s.get_page('a','bad',1) is None
    s.check()


@pytest.mark.parametrize('ws,edition,asset',[('a','e','b'*64),('a','missing','a'*64),('unknown','e','a'*64)])
def test_wrong_identity(library,ws,edition,asset):
    s,_=library
    with pytest.raises(sqlite3.IntegrityError):
        s.publish_revision(ws,edition,'bad',asset,'synthetic',[Page('bad')])
    assert s.db.execute("SELECT count(*) FROM revision").fetchone()[0]==0
    s.check()


def test_retry_membership_history_and_backup(library):
    s,path=library
    with s.transaction():
        stage(s,'a','a1','архівний oldtoken')
        stage(s,'b','b1','інший othertoken')
        c=Catalog(s.db,'a')
        register_batch(c,(record('a',collections=('c','extra')),))
        assert c.remove('c','e')
        c.archive('c')
        assert len(c.editions('extra'))==1
    s.publish_revision('a','e','a2','a'*64,'synthetic',[Page('новий newtoken',3,'iv',True)])
    assert s.get_page('b','a1',1) is None
    assert s.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'oldtoken'").fetchone()[0]==0
    assert s.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'newtoken'").fetchone()[0]==1
    out=path.with_name('backup.db'); s.backup(out)
    with closing(Storage(out,schema_version=2)) as restored:
        restored.check()
        assert restored.get_page('a','a1',1)['raw']=='архівний oldtoken'
        assert restored.get_page('a','a1',1)['printed']=='iv'
        assert restored.get_page('b','a1',1) is None
        assert len(Catalog(restored.db,'a').editions('extra'))==1
        assert len(Catalog(restored.db,'b').editions('c'))==1
        assert restored.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0]==2
    with pytest.raises(FileExistsError): s.backup(out)


def test_savepoint_preserves_outer(library):
    s,_=library
    with s.transaction():
        c=Catalog(s.db,'a'); c.create_collection('prior','Prior')
        with pytest.raises(ValueError,match='invalid raw page'):
            s.stage_revision('a','e','bad','a'*64,'synthetic',[Page('ok'),Page('bad\x00')])
        assert s.db.in_transaction
        stage(s,'a','good','good')
    assert s.get_page('a','bad',1) is None
    assert s.get_page('a','good',1)['raw']=='good'
    assert Catalog(s.db,'a').editions('prior')==()
    s.check()


def test_fk_corruption_rejected_at_backup(library):
    s,path=library
    # Simulate external DB corruption, not a supported application write.
    s.db.execute('PRAGMA foreign_keys=OFF')
    s.db.execute("INSERT INTO revision VALUES('bad','a','e',?,'synthetic')",('b'*64,))
    s.db.execute('PRAGMA foreign_keys=ON')
    out=path.with_name('bad.db')
    with pytest.raises(ValueError,match='database integrity failure'): s.backup(out)
    assert not out.exists()


def test_no_implicit_v1_upgrade(tmp_path):
    path=tmp_path/'old.db'
    with closing(Storage(path)) as s:
        s.publish_revision('a','e','old','a'*64,'synthetic',[Page('old')])
    with pytest.raises(ValueError,match='no live upgrade'): Storage(path,schema_version=2)
    with closing(Storage(path)) as s:
        assert s.schema_version==1
        assert s.get_page('a','old',1)['raw']=='old'
        s.check()


def test_backup_refuses_active_tx(library):
    s,path=library
    with s.transaction():
        stage(s,'a','r','not committed')
        with pytest.raises(ValueError,match='backup forbidden'): s.backup(path.with_name('active.db'))
    assert not path.with_name('active.db').exists()
    s.check()
