import pytest
from carr_library.storage.revisions import Storage, Page
from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor, resolve_span
from carr_library.citations.historical import historical_loader

ASSET = 'a'*64
META = Metadata('Synthetic author', 'Synthetic title')
OLD = Identity('A','edition','a'*64,'old')
LOC = Locator('physical_page',7,'iv',True)
RAW = 'old — Київ \U0001f642'

@pytest.fixture
def store(tmp_path):
    path = tmp_path/'library.db'
    s = Storage(path)
    s.publish_revision('A','edition','old',ASSET,'native',[Page(RAW,7,'iv',True),Page('other',8)])
    s.publish_revision('A','edition','new',ASSET,'native',[Page('new',7,'iv',True)])
    s.publish_revision('B','edition','foreign',ASSET,'native',[Page(RAW,7,'iv',True)])
    s.publish_revision('A','notes','logical',ASSET,'txt',[Page('unpaginated')])
    s.close()
    s = Storage(path)
    try:
        yield s
    finally:
        s.close()


def attestor(identity, locator, sha):
    return (identity,locator,sha) == (OLD,LOC,digest(RAW))


def test_historical_after_reopen(store):
    anchor = make_anchor(OLD,LOC,RAW,6,len(RAW),META,text_verified=True)
    with store.transaction():
        load = historical_loader(store.db,'A',attestor)
        assert resolve_span((anchor,),load) == ((anchor,RAW),)
        assert anchor.quote == 'Київ \U0001f642'
        assert store.get_page('A','old',7) is None  # ordinal != physical page


@pytest.mark.parametrize('identity,locator', [
    (Identity('B','edition',ASSET,'foreign'),LOC),
    (Identity('A','wrong',ASSET,'old'),LOC),
    (Identity('A','edition','b'*64,'old'),LOC),
    (OLD,Locator('physical_page',1)),
    (OLD,Locator('logical_segment',1)),
    (OLD,Locator('physical_page',7,'iv',False)),
    (Identity('A','edition',ASSET,'new'),LOC),
])
def test_wrong_source_or_no_attestation(store,identity,locator):
    with store.transaction(), pytest.raises(ValueError):
        historical_loader(store.db,'A',attestor)(identity,locator)


def test_logical_and_unknown_label(store):
    ident = Identity('A','notes',ASSET,'logical')
    loc = Locator('logical_segment',1)
    with store.transaction():
        load = historical_loader(store.db,'A',lambda i,l,h: (i,l,h)==(ident,loc,digest('unpaginated')))
        assert load(ident,loc)[2:] == ('unpaginated',True)
        with pytest.raises(ValueError):
            load(ident,Locator('physical_page',1))
        unknown = Locator('physical_page',8)
        load = historical_loader(store.db,'A',lambda i,l,h: (i,l,h)==(OLD,unknown,digest('other')))
        assert load(OLD,unknown)[1].display() == 'PDF page 8 (printed page unverified)'


def test_unsealed(store):
    with store.transaction():
        store.db.execute('INSERT INTO revision VALUES(?,?,?,?,?)',('draft','A','edition',ASSET,'native'))
        store.db.execute('INSERT INTO page(revision,ordinal,physical,printed,verified,raw,digest) VALUES(?,?,?,?,?,?,?)',('draft',1,7,'iv',1,RAW,digest(RAW)))
        # The public storage reader also excludes unpublished draft revisions.
        assert store.get_page('A','draft',1) is None
        with pytest.raises(ValueError):
            historical_loader(store.db,'A',lambda *_: True)(Identity('A','edition',ASSET,'draft'),LOC)


def test_label_does_not_verify_text(store):
    with store.transaction():
        for denied in (False,1,None):
            with pytest.raises(ValueError):
                historical_loader(store.db,'A',lambda *_: denied)(OLD,LOC)


def test_corrupt_digest(store):
    # Deliberate synthetic fault injection, not a production write path.
    with store.transaction():
        store.db.execute('DROP TRIGGER page_bu')
        store.db.execute("UPDATE page SET raw='tampered' WHERE revision='old' AND physical=7")
        with pytest.raises(ValueError):
            historical_loader(store.db,'A',lambda *_: True)(OLD,LOC)


def test_transaction_required(store):
    load = historical_loader(store.db,'A',attestor)
    with pytest.raises(ValueError):
        load(OLD,LOC)
    store.db.execute('PRAGMA read_uncommitted=ON')
    with store.transaction(), pytest.raises(ValueError):
        load(OLD,LOC)
