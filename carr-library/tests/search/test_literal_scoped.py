"""Synthetic contract fixtures; no real books or invented bibliographic claims."""
import hashlib
import sqlite3
import pytest
from carr_library.search.literal import search


@pytest.fixture
def db():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE active_page(id INTEGER PRIMARY KEY,workspace,edition,revision,asset,ordinal,physical,printed,verified,raw,digest)')
    def add(ws, ed, raw, ordinal=1, physical=None, printed=None, verified=0, revision='r1'):
        c.execute('INSERT INTO active_page VALUES(NULL,?,?,?,?,?,?,?,?,?,?)',
                  (ws,ed,revision,'a'*64,ordinal,physical,printed,verified,raw,hashlib.sha256(raw.encode()).hexdigest()))
    add('A','a','Straße café Cafe\u0301; French\nRevolution; революція; Marxism Marx. AND OR "x" O\'Brien *', physical=3, printed='iv',verified=1)
    add('A','b','Marx and railway. French', physical=1, printed='17',verified=0)
    add('A','b','Revolution. Marx.',ordinal=2)
    add('B','a','Marx Straße French Revolution',physical=9)
    yield c,add
    c.close()


@pytest.mark.parametrize('query,expected', [('STRASSE','Straße'),('CAFÉ','café'),('French Revolution','French\nRevolution'),('РЕВОЛЮЦІЯ','революція'),('AND','AND'),('"x"','"x"'),("O'Brien","O'Brien"),('*','*'),('OR','OR')])
def test_exact(db,query,expected):
    c,_=db
    hit=search(c,'A',query)[0]
    raw=c.execute('SELECT raw FROM active_page WHERE id=?',(hit['page_id'],)).fetchone()[0]
    assert hit['raw_quote']==expected==raw[hit['start']:hit['end']]
    assert hit['raw_text_sha256']==hashlib.sha256(raw.encode()).hexdigest()
    assert hit['workspace_id']=='A' and hit['revision_id']=='r1'


def test_unicode_and_partial(db):
    c,_=db
    assert [h['raw_quote'] for h in search(c,'A','café')]==['café','Cafe\u0301']
    assert not search(c,'A','cafe')
    assert not search(c,'A','ss',mode='word')
    assert [h['raw_quote'] for h in search(c,'A','Marx',mode='word')]==['Marx']*3
    assert len(search(c,'A','French Revolution'))==1


def test_scope_filters_paging(db):
    c,add=db
    initial=search(c,'A','Marx')
    for i in range(20): add('B',str(i),'Marx '*50)
    assert search(c,'A','Marx')==initial
    assert search(c,'A','Marx',limit=1,offset=1)==initial[1:2]
    assert search(c,'A','Marx',edition_ids=['b'])==initial[1:]
    assert not search(c,'A','Marx',edition_ids=[])
    assert not search(c,'A','Marx',edition_ids=["' OR 1=1 --"])
    assert search(c,'B','Marx')[0]['workspace_id']=='B'


def test_locators_terms_revision(db):
    c,_=db
    a,b,d=search(c,'A','Marx')
    assert (a['physical_index'],a['printed_label'],a['printed_label_verified'])==(3,'iv',True)
    assert b['physical_index']==1 and b['printed_label'] is None
    assert d['locator_kind']=='logical_segment' and d['physical_index'] is None and d['segment_index']==2
    h=search(c,'A','railway Marx',mode='terms')[0]
    assert h['edition_id']=='b' and len(h['match_spans'])==2
    assert not search(c,'A','railway Revolution',mode='terms')
    c.execute("UPDATE active_page SET revision='r2' WHERE workspace='A' AND edition='b'")
    assert search(c,'A','railway')[0]['revision_id']=='r2'


def test_invalid_budget_and_drift(db):
    c,_=db
    assert search(c,'A','  ')==[]
    assert search(c,'A','not here')==[]
    for q in ('\0','x'*501):
        with pytest.raises(ValueError): search(c,'A',q)
    with pytest.raises(ValueError): search(c,'','Marx')
    with pytest.raises(ValueError): search(c,'A','Marx',limit=101)
    with pytest.raises(TimeoutError): search(c,'A','Marx',cancelled=lambda: True)
    c.execute("UPDATE active_page SET digest='wrong' WHERE workspace='A' AND edition='a'")
    with pytest.raises(ValueError,match='digest'): search(c,'A','Marx')


def test_cancel_when_storage_returns_no_rows(db):
    c,_=db
    cancelled = False
    def reject():
        nonlocal cancelled
        cancelled = True
        return 0
    c.create_function('reject', 0, reject)
    c.execute('ALTER TABLE active_page RENAME TO pages')
    c.execute('CREATE VIEW active_page AS SELECT * FROM pages WHERE reject()')
    with pytest.raises(TimeoutError):
        search(c,'A','Marx',cancelled=lambda: cancelled)


def test_cancel_during_mapping_rejected_substring(db, monkeypatch):
    from carr_library.search import literal
    c,add=db
    add('C','c','xneedle '*4000)
    real_fold = literal._fold
    calls = 0
    def fold(value):
        nonlocal calls
        if len(value)==1:
            calls += 1
        return real_fold(value)
    monkeypatch.setattr(literal,'_fold',fold)
    with pytest.raises(TimeoutError):
        search(c,'C','needle',cancelled=lambda: calls>=1)
    assert 1 <= calls <= 1024


@pytest.mark.parametrize('mode', ['phrase','terms'])
def test_cancel_during_rejected_matches(db, monkeypatch, mode):
    from carr_library.search import literal
    c,add=db
    add('C','c','xneedle '*3000)
    real_word = literal._word
    calls = 0
    def word(value):
        nonlocal calls
        calls += 1
        return real_word(value)
    monkeypatch.setattr(literal,'_word',word)
    with pytest.raises(TimeoutError):
        search(c,'C','needle',mode=mode,cancelled=lambda: calls>=1)
    assert 1 <= calls <= 4
