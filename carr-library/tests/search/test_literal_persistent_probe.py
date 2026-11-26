"""Standalone native SQLite candidate probe; not the production storage schema.
No private sources, application imports, or new dependencies. CARR-05 owns
persistent schema and transaction integration; this file is evidence only.
"""
import sqlite3, re, unicodedata as ud, tempfile, pathlib, time, statistics, hashlib

def fold(t):
    return re.sub(r'\s+', ' ', ud.normalize('NFD', ud.normalize('NFD', t).casefold()))

def match(t):
    return '"'+t.replace('"','""')+'"'

def query(db, workspace, text, table='norm_fts'):
    return db.execute(f'SELECT p.id,p.raw,p.revision,p.digest FROM {table} JOIN active_page p ON p.id={table}.rowid WHERE {table} MATCH ? AND p.workspace=? ORDER BY p.id', (match(fold(text)),workspace)).fetchall()

def update(db, ws, edition, revision, raw):
    with db:
        old=db.execute('SELECT id FROM active_page WHERE workspace=? AND edition=?',(ws,edition)).fetchall()
        db.executemany('DELETE FROM norm_fts WHERE rowid=?',old)
        db.executemany('DELETE FROM raw_fts WHERE rowid=?',old)
        db.execute('DELETE FROM active WHERE workspace=? AND edition=?',(ws,edition))
        row=db.execute('INSERT INTO page(workspace,edition,revision,raw,digest) VALUES(?,?,?,?,?)',(ws,edition,revision,raw,hashlib.sha256(raw.encode()).hexdigest())).lastrowid
        db.execute('INSERT INTO active VALUES(?,?,?)',(ws,edition,revision))
        db.execute('INSERT INTO norm_fts(rowid,t) VALUES(?,?)',(row,fold(raw)))
        db.execute('INSERT INTO raw_fts(rowid,t) VALUES(?,?)',(row,raw))
        return row

def test_persistent_candidate():
    with tempfile.TemporaryDirectory() as d:
        path=pathlib.Path(d)/'probe.db'; db=sqlite3.connect(path)
        db.executescript('''CREATE TABLE page(id INTEGER PRIMARY KEY,workspace,edition,revision,raw,digest);
        CREATE TABLE active(workspace,edition,revision,PRIMARY KEY(workspace,edition));
        CREATE VIEW active_page AS SELECT p.* FROM page p JOIN active a USING(workspace,edition,revision);
        CREATE VIRTUAL TABLE norm_fts USING fts5(t, tokenize='trigram case_sensitive 1');
        CREATE VIRTUAL TABLE raw_fts USING fts5(t, tokenize='unicode61 remove_diacritics 0');''')
        a=update(db,'A','book','r1','Straße café French\nRevolution; "x"')
        b=update(db,'B','book','r2','Straße café French\nRevolution; "x"')
        for q in ('STRASSE','CAFE\u0301','French Revolution','"x"'):
            hit=query(db,'A',q); assert [r[0] for r in hit]==[a], (q,hit)
        assert not query(db,'A','STRASSE','raw_fts')
        print('PASS normalized casefold, decomposed accents, whitespace, quoted punctuation; raw unicode61 misses STRASSE')
        assert [r[0] for r in query(db,'B','STRASSE')]==[b]
        print('PASS workspace isolation')
        # Simulate a failed revision swap; SQLite rolls back index and active pointer together.
        try:
            with db:
                db.execute('DELETE FROM norm_fts WHERE rowid=?',(a,)); db.execute("DELETE FROM active WHERE workspace='A'"); raise RuntimeError('interrupt')
        except RuntimeError: pass
        assert [r[0] for r in query(db,'A','STRASSE')]==[a]
        print('PASS rollback preserves active normalized index')
        n=update(db,'A','book','r3','New source text only')
        assert not query(db,'A','STRASSE') and query(db,'A','source')[0][0]==n
        assert db.execute('SELECT raw FROM page WHERE id=?',(a,)).fetchone()[0].startswith('Straße')
        # Stale candidate is harmless to scope if joined against active_page.
        with db: db.execute('INSERT INTO norm_fts(rowid,t) VALUES(?,?)',(a,fold('Straße café')))
        assert not query(db,'A','STRASSE') and query(db,'B','STRASSE')[0][0]==b
        stale=db.execute('SELECT rowid FROM norm_fts EXCEPT SELECT id FROM active_page').fetchall(); assert stale==[(a,)]
        with db: db.execute('DELETE FROM norm_fts WHERE rowid=?',(a,))
        db.close(); db=sqlite3.connect(path)
        assert query(db,'A','source')[0][0]==n and query(db,'B','STRASSE')[0][0]==b
        print('PASS revision swap, historical raw preservation, stale-candidate rejection, reopen')
        with db: db.execute('DELETE FROM norm_fts WHERE rowid=?',(n,))
        assert not query(db,'A','source')
        assert db.execute('SELECT id FROM active_page EXCEPT SELECT rowid FROM norm_fts').fetchall()==[(n,)]
        with db: db.execute('INSERT INTO norm_fts(rowid,t) VALUES(?,?)',(n,fold('New source text only')))
        assert not query(db,'A','Ne') and 'Ne' in query(db,'A','source')[0][1]
        print('PASS missing-index detection and short-query fallback requirement; candidate-only lookup is insufficient')
        # Native read-only connection has an independent progress handler.
        ro=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        ro.set_progress_handler(lambda: 1,1)
        try:
            ro.execute('SELECT * FROM active_page').fetchall(); raise AssertionError('not interrupted')
        except sqlite3.OperationalError as e: assert e.sqlite_errorcode==sqlite3.SQLITE_INTERRUPT
        ro.close(); assert db.execute('SELECT count(*) FROM active_page').fetchone()==(2,)
        print('PASS native read-only interruption does not affect writer')
        # Benchmark isolated FTS candidate lookup only, not end-to-end literal search.
        with db:
            raw='Synthetic railway source and historical evidence. '*4
            for i in range(10000):
                r=raw+str(i)+(' raremarker' if i==9999 else '')
                row=db.execute('INSERT INTO page(workspace,edition,revision,raw,digest) VALUES(?,?,?,?,?)',('A',str(i),'r',r,hashlib.sha256(r.encode()).hexdigest())).lastrowid
                db.execute('INSERT INTO active VALUES(?,?,?)',('A',str(i),'r'))
                db.execute('INSERT INTO norm_fts(rowid,t) VALUES(?,?)',(row,fold(r)))
        times=[]; transient=[]
        for _ in range(7):
            t=time.perf_counter(); assert len(query(db,'A','raremarker'))==1; times.append(time.perf_counter()-t)
            t=time.perf_counter(); tmp=sqlite3.connect(':memory:'); tmp.execute("CREATE VIRTUAL TABLE f USING fts5(t, tokenize='trigram case_sensitive 1')")
            tmp.executemany('INSERT INTO f(rowid,t) VALUES(?,?)',((i,fold(r)) for i,r in db.execute("SELECT id,raw FROM active_page WHERE workspace='A'")))
            assert len(tmp.execute('SELECT rowid FROM f WHERE f MATCH ?',('"raremarker"',)).fetchall())==1; tmp.close(); transient.append(time.perf_counter()-t)
        print('PASS benchmark 10000 synthetic pages; persistent median %.6fs, rebuild median %.6fs (7 runs)'%(statistics.median(times),statistics.median(transient)))
        print('sqlite',sqlite3.sqlite_version,db.execute('select sqlite_source_id()').fetchone()[0]); db.close()
if __name__=='__main__': test_persistent_candidate()
