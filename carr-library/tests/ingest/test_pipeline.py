import sqlite3
from pathlib import Path
import pytest
from carr_library.ingest import pipeline
from carr_library.storage.revisions import Storage
from carr_library.catalog.intake import IntakeError


@pytest.fixture
def local(tmp_path):
    storage = Storage(tmp_path / 'library.sqlite')
    ingest = pipeline.Ingest(tmp_path / 'jobs.sqlite', storage)
    yield tmp_path, storage, ingest
    ingest.close()
    storage.close()


def put(root, text='Перший запис\r\n\fSecond railway passage'):
    (root / 'book.txt').write_bytes(text.encode())
    return 'book.txt'


def active(storage):
    return storage.db.execute('SELECT * FROM active_page ORDER BY ordinal').fetchall()


def test_resume_dedup_exact_text_and_isolation(local):
    root, s, j = local
    path = put(root)
    assert j.dry_run(root, path)['segments'] == 2
    job = j.start('A', 'edition1', root, path)
    assert j.start('A', 'edition1', root, path) == job
    assert j.run('A', job, root, path, max_steps=1)['completed'] == 1
    assert not active(s)
    with pytest.raises(pipeline.IngestError):
        j.inspect('B', job)
    j.close()
    j.db = sqlite3.connect(root / 'jobs.sqlite', isolation_level=None)
    j.db.row_factory = sqlite3.Row
    j.db.execute('PRAGMA foreign_keys=ON')
    assert j.run('A', job, root, path)['state'] == 'complete'
    assert [(r['physical'], r['raw']) for r in active(s)] == [(None,'Перший запис\r\n'),(None,'Second railway passage')]
    rows = s.db.execute("SELECT p.raw FROM page_fts JOIN active_page p ON p.id=page_fts.rowid WHERE page_fts MATCH ? AND p.workspace=?", ('railway','A')).fetchall()
    assert [r[0] for r in rows] == ['Second railway passage']
    assert j.run('A', job, root, path)['state'] == 'complete'
    assert len(active(s)) == 2
    assert j.start('B','edition1',root,path) != job


def test_cancel_and_retry(local):
    root,s,j=local; path=put(root); job=j.start('A','E',root,path)
    j.run('A',job,root,path,max_steps=1)
    assert j.run('A',job,root,path,cancelled=lambda:True)['state']=='cancelled'
    assert not active(s)
    j.retry('A',job)
    assert j.run('A',job,root,path)['state']=='complete'


def test_changed_original_rejected(local):
    root,s,j=local; path=put(root); job=j.start('A','E',root,path)
    j.run('A',job,root,path,max_steps=1)
    put(root,'Changed source')
    with pytest.raises(pipeline.IngestError,match='changed'):
        j.run('A',job,root,path)
    assert not active(s)
    assert j.inspect('A',job)['completed']==1


def test_failure_before_commit_and_recovery(local, monkeypatch):
    root,s,j=local; path=put(root); job=j.start('A','E',root,path)
    publish=s.stage_revision
    def fail(*args):
        raise sqlite3.OperationalError('simulated disk full')
    monkeypatch.setattr(s,'stage_revision',fail)
    with pytest.raises(sqlite3.OperationalError): j.run('A',job,root,path)
    assert not active(s)
    assert j.inspect('A',job)['completed']==2
    j.retry('A',job)
    monkeypatch.setattr(s,'stage_revision',publish)
    assert j.run('A',job,root,path)['state']=='complete'


def test_crash_after_storage_commit(local, monkeypatch):
    root,s,j=local; path=put(root); job=j.start('A','E',root,path)
    publish=j._publish
    def crash(*args):
        publish(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(j,'_publish',crash)
    with pytest.raises(KeyboardInterrupt): j.run('A',job,root,path)
    assert len(active(s))==2
    monkeypatch.setattr(j,'_publish',publish)
    assert j.run('A',job,root,path)['state']=='complete'
    assert len(active(s))==2


def test_checkpoint_tamper(local):
    root,s,j=local; path=put(root); job=j.start('A','E',root,path)
    j.run('A',job,root,path,max_steps=1)
    j.db.execute('UPDATE checkpoint SET payload=? WHERE job=?',('{}',job))
    with pytest.raises(pipeline.IngestError,match='integrity'): j.run('A',job,root,path)
    assert not active(s)


def test_pdf_native_and_quarantine(local, monkeypatch):
    from reportlab.pdfgen import canvas
    root,s,j=local
    p=root/'book.pdf'; c=canvas.Canvas(str(p)); c.drawString(80,700,'Synthetic railway passage'); c.showPage(); c.showPage(); c.save()
    job=j.start('A','E',root,p.name)
    assert j.run('A',job,root,p.name,max_steps=1)['completed']==1
    assert not active(s)
    real=pipeline.extract_page
    def missing(path,index,config):
        r=real(path,index,config); r['status']='review_required'; r['warnings']=['ocr_engine_missing']; return r
    monkeypatch.setattr(pipeline,'extract_page',missing)
    assert j.run('A',job,root,p.name)['state']=='quarantined'
    assert not active(s)
    j.retry('A',job)
    assert j.inspect('A',job)['completed']==1
    monkeypatch.setattr(pipeline,'extract_page',real)
    assert j.run('A',job,root,p.name)['state']=='complete'
    assert [(r['physical'],r['printed']) for r in active(s)]==[(1,None),(2,None)]


def test_stale_job_cannot_replace_new_revision(local):
    root,s,j=local; path=put(root); old=j.start('A','E',root,path)
    other=put(root,'New edition content'); new=j.start('A','E',root,other)
    assert j.run('A',new,root,other)['state']=='complete'
    put(root)
    assert j.run('A',old,root,path)['state']=='conflict'
    assert [r['raw'] for r in active(s)]==['New edition content']


def test_invalid_path_and_mismatched_extension(local):
    root,s,j=local
    with pytest.raises(IntakeError): j.start('A','E',root,'../book.txt')
    (root/'fake.pdf').write_text('not a pdf')
    with pytest.raises(IntakeError): j.start('A','E',root,'fake.pdf')
    assert not active(s)
