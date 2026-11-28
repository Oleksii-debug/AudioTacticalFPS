from dataclasses import replace
import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.reader.context import open_context


@pytest.fixture
def sample():
    identity = Identity('A', 'edition-1', 'a' * 64, 'old-revision')
    meta = Metadata('Original author', 'Same title', 'II', 'First')
    raw = ('Before: Київ\n', 'Čas after.')
    loc = (Locator('physical_page', 3, 'iv', True), Locator('physical_page', 4, '5'))
    anchors = (make_anchor(identity, loc[0], raw[0], 8, len(raw[0]), meta, text_verified=True),
               make_anchor(identity, loc[1], raw[1], 0, 3, meta, text_verified=True))
    def load(i, l):
        assert i == identity
        return i, l, raw[loc.index(l)], True
    return anchors, raw, load


def test_exact_navigation_and_copy(sample):
    anchors, raw, load = sample
    context = open_context('A', anchors, load)
    assert context.page(0).raw_text == raw[0]
    assert context.page(1).raw_text == raw[1]
    assert context.clipboard('A', 0, 'quote', load) == 'Київ\n'
    assert context.clipboard('A', 1, 'quote', load) == 'Čas'
    assert 'p. iv (physical page 3)' in context.clipboard('A', 0, 'citation', load)
    assert 'printed page unverified' in context.clipboard('A', 1, 'citation', load)
    assert 'Original author' in context.clipboard('A', 1, 'both', load)
    for invalid in (-1, 2, True):
        with pytest.raises(IndexError):
            context.page(invalid)
    with pytest.raises(ValueError):
        context.clipboard('A', 0, 'html', load)


def test_other_workspace_rejected_before_loader(sample):
    anchors, _, _ = sample
    def forbidden(*args):
        pytest.fail('loader must not be called')
    with pytest.raises(ValueError):
        open_context('B', anchors, forbidden)
    with pytest.raises(ValueError):
        open_context('A', (), forbidden)


@pytest.mark.parametrize('fault', ['revision', 'edition', 'raw', 'locator', 'unverified', 'missing'])
def test_second_page_failure_prevents_any_context(sample, fault):
    anchors, _, load = sample
    def broken(i, l):
        original = load(i, l)
        if l.index == 3:
            return original
        i, l, raw, verified = original
        if fault == 'missing':
            raise FileNotFoundError('sealed revision unavailable')
        if fault == 'revision':
            i = replace(i, revision='new')
        elif fault == 'edition':
            i = replace(i, edition_id='edition-2')
        elif fault == 'raw':
            raw += ' altered'
        elif fault == 'locator':
            l = replace(l, printed='9')
        elif fault == 'unverified':
            verified = False
        return i, l, raw, verified
    with pytest.raises((ValueError, FileNotFoundError)):
        open_context('A', anchors, broken)


def test_mixed_metadata_rejected(sample):
    anchors, _, load = sample
    other = replace(anchors[1], metadata=replace(anchors[1].metadata, author='Different author'))
    with pytest.raises(ValueError):
        open_context('A', (anchors[0], other), load)


def test_unpaginated_single_page(sample):
    anchors, _, _ = sample
    a = anchors[0]
    locator = Locator('logical_segment', 1)
    raw = 'unpaginated'
    anchor = make_anchor(a.identity, locator, raw, 0, len(raw), a.metadata, text_verified=True)
    load = lambda i, l: (i, l, raw, True)
    context = open_context('A', [anchor], load)
    assert 'Segment 1 (unpaginated)' in context.clipboard('A', 0, 'citation', load)
    assert 'Physical page' not in context.clipboard('A', 0, 'citation', load)


def test_copy_revalidates_scope_and_revision(sample):
    anchors, _, load = sample
    context = open_context('A', anchors, load)
    with pytest.raises(ValueError):
        context.clipboard('B', 0, 'quote', load)
    def drift(i, l):
        i, l, raw, verified = load(i, l)
        return i, l, raw + ' changed', verified
    with pytest.raises(ValueError):
        context.clipboard('A', 0, 'quote', drift)
