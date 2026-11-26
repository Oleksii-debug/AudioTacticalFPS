import sqlite3
import pytest
from carr_library.storage.revisions import Storage, Page

A='a'*64

def publish(s,workspace,revision,pages):
    s.publish_revision(workspace,'edition',revision,A,'fixture',pages)

def hits(s,workspace,term):
    return [r[0] for r in s.db.execute('SELECT p.raw FROM page_fts JOIN active_page p ON p.id=page_fts.rowid WHERE page_fts MATCH ? AND p.workspace=?',(term,workspace))]

def test_active_and_history(tmp_path):
    s=Storage(tmp_path/'db')
    publish(s,'a','r1',[Page('Міст alpha',1,'iv',True)])
    publish(s,'b','r2',[Page('Міст alpha',1)])
    publish(s,'a','r3',[Page('beta',None)])
    assert hits(s,'a','alpha')==[] and hits(s,'b','alpha')==['Міст alpha']
    assert hits(s,'b','Міст')==['Міст alpha']
    assert s.get_page('a','r1',1)['printed']=='iv'
    assert s.get_page('b','r1',1) is None
    assert s.get_page('a','r3',1)['physical'] is None
    with pytest.raises(sqlite3.IntegrityError):
        s.db.execute("UPDATE page SET raw='tampered'")
    s.check(); s.close()

def test_interrupted_publish(tmp_path):
    s=Storage(tmp_path/'db'); publish(s,'a','r1',[Page('old')])
    def interrupted():
        yield Page('new')
        raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError): publish(s,'a','r2',interrupted())
    with pytest.raises(sqlite3.IntegrityError): publish(s,'a','r1',[Page('duplicate')])
    assert s.get_page('a','r2',1) is None and hits(s,'a','old')==['old']
    s.check(); s.close()

def test_stale_index_and_backup(tmp_path):
    s=Storage(tmp_path/'db'); publish(s,'a','r1',[Page('needle')])
    s.db.execute('DELETE FROM indexed')
    with pytest.raises(ValueError): s.check()
    s.rebuild(); s.rebuild(); assert hits(s,'a','needle')==['needle']
    s.db.execute("INSERT INTO page_fts(page_fts) VALUES('delete-all')")
    with pytest.raises(sqlite3.DatabaseError): s.check()
    # FTS-only damage is recoverable with native rebuild.
    s.rebuild(); s.backup(tmp_path/'copy')
    with pytest.raises(FileExistsError): s.backup(tmp_path/'copy')
    s.close(); c=Storage(tmp_path/'copy'); c.check()
    assert hits(c,'a','needle')==['needle']; c.close()

def test_schema_and_injection(tmp_path):
    s=Storage(tmp_path/'db'); publish(s,"a' OR 1=1 --",'r1',[Page('needle')])
    assert hits(s,'a','needle')==[]
    with pytest.raises(sqlite3.IntegrityError): publish(s,'a','r2',[Page('logical',None,'1',True)])
    s.db.execute('PRAGMA user_version=99'); s.close()
    with pytest.raises(ValueError): Storage(tmp_path/'db')
    p=tmp_path/'bad'; p.write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError): Storage(p)


def test_reader_atomic_visibility(tmp_path):
    path=tmp_path/'db'; s=Storage(path); publish(s,'a','r1',[Page('old')])
    reader=Storage(path)
    def pages():
        yield Page('new')
        assert hits(reader,'a','old')==['old'] and hits(reader,'a','new')==[]
    publish(s,'a','r2',pages())
    assert hits(reader,'a','old')==[] and hits(reader,'a','new')==['new']
    with pytest.raises(sqlite3.IntegrityError):
        s.db.execute("INSERT INTO page(revision,ordinal,verified,raw,digest) VALUES('r1',2,0,'extra','x')")
    reader.close(); s.close()
