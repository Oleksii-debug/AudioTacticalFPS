"""Candidate vertical slice: persistent research library using SQLite FTS5 and pypdf.
Local trusted inputs only; untrusted PDF sandboxing and OCR are separate release gates.
"""
import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS workspace(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS book(id INTEGER PRIMARY KEY, workspace INTEGER NOT NULL REFERENCES workspace(id), title TEXT NOT NULL, author TEXT NOT NULL, edition TEXT NOT NULL, digest TEXT NOT NULL, UNIQUE(workspace,id), UNIQUE(workspace,title,author,edition,digest));
CREATE TABLE IF NOT EXISTS page(id INTEGER PRIMARY KEY, workspace INTEGER NOT NULL, book INTEGER NOT NULL, number INTEGER NOT NULL, label TEXT, raw TEXT NOT NULL, digest TEXT NOT NULL, FOREIGN KEY(workspace,book) REFERENCES book(workspace,id), UNIQUE(workspace,id), UNIQUE(book,number));
CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(raw, content='page', content_rowid='id', tokenize='unicode61 remove_diacritics 0');
CREATE TRIGGER IF NOT EXISTS page_insert AFTER INSERT ON page BEGIN INSERT INTO page_fts(rowid,raw) VALUES(new.id,new.raw); END;
CREATE TRIGGER IF NOT EXISTS page_immutable_update BEFORE UPDATE ON page BEGIN SELECT RAISE(ABORT,'immutable page'); END;
CREATE TRIGGER IF NOT EXISTS page_immutable_delete BEFORE DELETE ON page BEGIN SELECT RAISE(ABORT,'immutable page'); END;
CREATE TABLE IF NOT EXISTS collection(id INTEGER PRIMARY KEY, workspace INTEGER NOT NULL REFERENCES workspace(id), name TEXT NOT NULL, UNIQUE(workspace,id), UNIQUE(workspace,name));
CREATE TABLE IF NOT EXISTS membership(workspace INTEGER NOT NULL, collection INTEGER NOT NULL, book INTEGER NOT NULL, PRIMARY KEY(collection,book), FOREIGN KEY(workspace,collection) REFERENCES collection(workspace,id), FOREIGN KEY(workspace,book) REFERENCES book(workspace,id));
CREATE TABLE IF NOT EXISTS topic(id INTEGER PRIMARY KEY, workspace INTEGER NOT NULL REFERENCES workspace(id), parent INTEGER, name TEXT NOT NULL, UNIQUE(workspace,id), FOREIGN KEY(workspace,parent) REFERENCES topic(workspace,id));
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, workspace INTEGER NOT NULL, topic INTEGER NOT NULL, page INTEGER NOT NULL, start INTEGER NOT NULL, stop INTEGER NOT NULL, quote TEXT NOT NULL, page_digest TEXT NOT NULL, note TEXT NOT NULL, FOREIGN KEY(workspace,topic) REFERENCES topic(workspace,id), FOREIGN KEY(workspace,page) REFERENCES page(workspace,id));
CREATE TRIGGER IF NOT EXISTS evidence_anchor_immutable BEFORE UPDATE OF workspace,topic,page,start,stop,quote,page_digest ON evidence BEGIN SELECT RAISE(ABORT,'immutable evidence anchor'); END;
'''


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ResearchLibrary:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def _insert(self, sql, args):
        with self.db:
            return self.db.execute(sql, args).lastrowid

    def workspace(self, name):
        return self._insert('INSERT INTO workspace(name) VALUES(?)', (name,))

    def collection(self, workspace, name):
        return self._insert('INSERT INTO collection(workspace,name) VALUES(?,?)', (workspace, name))

    def assign(self, workspace, collection, book):
        with self.db:
            if not self.db.execute('SELECT 1 FROM collection WHERE workspace=? AND id=?', (workspace, collection)).fetchone() or not self.db.execute('SELECT 1 FROM book WHERE workspace=? AND id=?', (workspace, book)).fetchone():
                raise sqlite3.IntegrityError('collection or book outside workspace')
            self.db.execute('INSERT INTO membership VALUES(?,?,?) ON CONFLICT(collection,book) DO NOTHING', (workspace, collection, book))

    def topic(self, workspace, name, parent=None):
        return self._insert('INSERT INTO topic(workspace,name,parent) VALUES(?,?,?)', (workspace, name, parent))

    def move_topic(self, workspace, topic, parent):
        with self.db:
            row = self.db.execute('SELECT id FROM topic WHERE workspace=? AND id=?', (workspace, topic)).fetchone()
            if row is None:
                raise ValueError('unknown topic')
            descendants = self.db.execute('WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT t.id FROM topic t JOIN d ON t.parent=d.id WHERE t.workspace=?) SELECT id FROM d', (topic, workspace))
            if parent in {r[0] for r in descendants}:
                raise ValueError('topic cycle')
            self.db.execute('UPDATE topic SET parent=? WHERE workspace=? AND id=?', (parent, workspace, topic))

    def add_book(self, workspace, title, author, edition, pages, source_digest=None):
        pages = list(pages)
        if not pages or len(pages) > 1000 or sum(len(t) for _, t in pages) > 10_000_000:
            raise ValueError('invalid or oversized page set')
        source_digest = source_digest or digest(json.dumps(pages, ensure_ascii=False))
        with self.db:
            cur = self.db.execute('INSERT INTO book(workspace,title,author,edition,digest) VALUES(?,?,?,?,?)', (workspace,title,author,edition,source_digest))
            book = cur.lastrowid
            self.db.executemany('INSERT INTO page(workspace,book,number,label,raw,digest) VALUES(?,?,?,?,?,?)', [(workspace,book,n,label,raw,digest(raw)) for n,(label,raw) in enumerate(pages,1)])
        return book

    def import_file(self, workspace, path, title, author, edition):
        path = Path(path)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('source too large')
        data = path.read_bytes()
        if path.suffix.lower() == '.txt':
            pages = [(None, s) for s in data.decode('utf-8-sig').split('\f')]
        elif path.suffix.lower() == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted or len(reader.pages) > 1000:
                raise ValueError('encrypted or oversized PDF')
            pages = [(None, p.extract_text() or '') for p in reader.pages]
            if not any(t.strip() for _,t in pages):
                raise ValueError('OCR required; no native text')
        else:
            raise ValueError('unsupported format')
        return self.add_book(workspace,title,author,edition,pages,hashlib.sha256(data).hexdigest())

    def search(self, workspace, query, collection=None, thematic=False, limit=50):
        query = query.strip()
        if not query or len(query)>500 or not 1<=limit<=1000:
            return []
        terms = re.findall(r'\w+', query, re.UNICODE)
        if not terms:
            return []
        # FTS is candidate retrieval; the raw page supplies the exact quotation.
        quoted = lambda s: '"' + s.replace('"','""') + '"'
        match = ' OR '.join(quoted(t) for t in terms) if thematic else quoted(query)
        sql = '''SELECT p.*,b.title,b.author,b.edition,b.digest AS source_digest,bm25(page_fts) AS rank
                 FROM page_fts JOIN page p ON p.id=page_fts.rowid JOIN book b ON b.id=p.book
                 WHERE page_fts MATCH ? AND p.workspace=?
                 AND (? IS NULL OR EXISTS (SELECT 1 FROM membership m WHERE m.workspace=p.workspace AND m.book=p.book AND m.collection=?))
                 ORDER BY rank,p.id'''
        pattern = re.compile(r'(?<!\w)(?:'+'|'.join(re.escape(t) for t in (terms if thematic else [query]))+r')(?!\w)', re.IGNORECASE)
        results = []
        for row in self.db.execute(sql,(match,workspace,collection,collection)):
            raw = row['raw']; hit = pattern.search(raw)
            if not hit:
                continue
            start,stop = (0,len(raw)) if thematic else (max(0,hit.start()-40),min(len(raw),hit.end()+40))
            results.append(dict(page=row['id'],book=row['book'],title=row['title'],author=row['author'],edition=row['edition'],physical=row['number'],printed=row['label'],source_digest=row['source_digest'],page_digest=row['digest'],start=start,stop=stop,quote=raw[start:stop]))
            if len(results)>=limit:
                break
        return results

    def save(self, workspace, topic, hit, note=''):
        row = self.db.execute('SELECT raw,digest FROM page WHERE workspace=? AND id=?',(workspace,hit['page'])).fetchone()
        start,stop = hit['start'],hit['stop']
        if row is None or not 0<=start<stop<=len(row['raw']) or row['raw'][start:stop]!=hit['quote'] or digest(row['raw'])!=row['digest'] or row['digest']!=hit['page_digest']:
            raise ValueError('unverified source span')
        return self._insert('INSERT INTO evidence(workspace,topic,page,start,stop,quote,page_digest,note) VALUES(?,?,?,?,?,?,?,?)',(workspace,topic,hit['page'],start,stop,hit['quote'],hit['page_digest'],note))

    def note(self, workspace, evidence, note):
        with self.db:
            self.db.execute('UPDATE evidence SET note=? WHERE workspace=? AND id=?',(note,workspace,evidence))

    def export_topic(self, workspace, topic):
        sql = '''SELECT e.quote,e.note,e.start,e.stop,e.page_digest,p.raw,p.number,p.label,b.title,b.author,b.edition,b.digest AS source_digest FROM evidence e JOIN page p ON p.id=e.page JOIN book b ON b.id=p.book WHERE e.workspace=? AND e.topic=? ORDER BY e.id'''
        output = []
        for r in self.db.execute(sql,(workspace,topic)):
            if digest(r['raw'])!=r['page_digest'] or r['raw'][r['start']:r['stop']]!=r['quote']:
                raise ValueError('evidence drift')
            entry = dict(r); del entry['raw']; output.append(entry)
        return json.dumps(output,ensure_ascii=False,indent=2)
