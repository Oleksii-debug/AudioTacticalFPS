from dataclasses import replace
import json
import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research.anchored_export import SavedEvidence, export_saved

RAW = 'Before: Українська цитата\nNext.'


def sample():
    identity = Identity('A', 'ed1', 'a'*64, 'old')
    locator = Locator('physical_page', 2, 'ix', True)
    anchor = make_anchor(identity, locator, RAW, 8, 25,
                         Metadata('Old author', 'Old title', 'II', '1'), text_verified=True)
    row = SavedEvidence('T', 'E', anchor, 'Separate note')
    def load(i, loc):
        assert i == identity and loc == locator
        return identity, locator, RAW, True
    return row, load


def test_historical_exact_export():
    row, load = sample()
    result = export_saved('A', 'T', [row], load)
    quote = next(line.split(': ', 1)[1] for line in result.splitlines() if line.startswith('Exact quotation: '))
    assert json.loads(quote) == row.anchor.quote
    assert 'Old author' in result and 'Old title' in result and 'volume II' in result
    assert 'printed page ix' in result and 'Separate note' in result and '"old"' in result


@pytest.mark.parametrize('case', ['workspace', 'topic', 'duplicate', 'blank'])
def test_scope_checked_before_any_load(case):
    row, _ = sample()
    other = replace(row, evidence_id='E2')
    if case == 'workspace':
        other = replace(other, anchor=replace(other.anchor, identity=replace(other.anchor.identity, workspace='B')))
    elif case == 'topic':
        other = replace(other, topic_id='Other')
    elif case == 'duplicate':
        other = row
    else:
        other = replace(other, evidence_id=' ')
    def forbidden(*args):
        pytest.fail('loader called before scope check')
    with pytest.raises(ValueError):
        export_saved('A', 'T', [row, other], forbidden)


@pytest.mark.parametrize('case', ['raw', 'revision', 'edition', 'asset', 'locator', 'unverified', 'missing'])
def test_source_drift(case):
    row, _ = sample()
    def load(i, loc):
        if case == 'missing':
            raise LookupError('sealed revision absent')
        if case == 'raw':
            return i, loc, RAW + ' changed', True
        if case == 'revision':
            return replace(i, revision='new'), loc, RAW, True
        if case == 'edition':
            return replace(i, edition_id='ed2'), loc, RAW, True
        if case == 'asset':
            return replace(i, asset_sha256='b'*64), loc, RAW, True
        if case == 'locator':
            return i, replace(loc, index=3), RAW, True
        return i, loc, RAW, False
    with pytest.raises((ValueError, LookupError)):
        export_saved('A', 'T', [row], load)


@pytest.mark.parametrize('locator, expected', [
    (Locator('logical_segment', 4), 'Segment 4 (unpaginated)'),
    (Locator('physical_page', 7, 'candidate', False), 'Physical page 7; printed page unknown')])
def test_locator_preserved(locator, expected):
    row, _ = sample()
    row = replace(row, anchor=replace(row.anchor, locator=locator))
    result = export_saved('A', 'T', [row], lambda i, loc: (i, loc, RAW, True), format='markdown')
    assert expected in result
    assert 'candidate' not in result


def test_all_or_nothing_second_page_missing():
    row, load = sample()
    second = replace(row, evidence_id='E2', anchor=replace(row.anchor, locator=Locator('physical_page', 3)))
    with pytest.raises(LookupError):
        export_saved('A', 'T', [row, second], lambda i, loc: load(i, loc) if loc.index == 2 else (_ for _ in ()).throw(LookupError('missing')))
