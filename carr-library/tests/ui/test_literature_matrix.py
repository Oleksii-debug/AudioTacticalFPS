"""Comparison presentation retains source, topic and slice identities."""
from types import SimpleNamespace

from carr_library.ui.literature_matrix import comparison_tags, evidence_text, theme_summary, visible_sources


def item(topic='root', identity='same', *, tags=('Theme',), note='Personal note'):
    return SimpleNamespace(topic_id=topic, topic_name='Repeated topic', evidence_id=identity,
        tags=tags, note=note, quotes=('One exact page.\r\n', 'Another exact page.'),
        citations=('Page 1; printed unknown', 'Page 2; printed unknown'))


def source(revision, evidence):
    return SimpleNamespace(edition_id='same-edition', revision_id=revision, title='Repeated title',
        author='Author', edition='1', volume='2', evidence=evidence)


def test_same_title_and_edition_with_distinct_revisions_remain_separate():
    first = source('old', (item(),))
    second = source('new', (item('child', tags=('theme',)),))
    matrix = SimpleNamespace(topic_id='root', sources=(first, second))
    assert len(visible_sources(matrix)) == 2
    assert visible_sources(matrix, include_descendants=False) == ((first, first.evidence),)
    assert visible_sources(matrix, tag_filter='Theme') == ((first, first.evidence),)
    assert visible_sources(matrix, tag_filter='theme') == ((second, second.evidence),)
    assert visible_sources(matrix, tag_filter='The') == ()


def test_theme_counts_use_scoped_evidence_ids_and_source_versions():
    first = source('old', (item(), item('child')))
    second = source('new', (item(identity='different'),))
    summary = theme_summary(((first, first.evidence), (second, second.evidence)))
    assert summary == 'Theme: 2 source versions; 3 evidence items.'


def test_detail_keeps_each_exact_slice_and_research_note_separate():
    evidence = item(note='Commentary is not a quotation')
    text = evidence_text(source('old', (evidence,)), evidence, topic_path='Chapter / Repeated topic')
    assert 'One exact page.\r\n' in text
    assert 'Another exact page.' in text
    assert text.index('Page 1') < text.index('One exact page.') < text.index('Page 2')
    assert 'Research note, separate from the source:\n\nCommentary is not a quotation' in text
    assert 'Chapter / Repeated topic' in text
    assert 'Source revision: old' in text


def test_empty_filter_has_a_readable_theme_explanation():
    assert 'No tags' in theme_summary(())


def test_missing_tag_after_refresh_does_not_silently_expand_to_all_themes():
    matrix = SimpleNamespace(themes=('Current', 'All themes'))
    names, labels = comparison_tags(matrix, 'Removed theme')
    assert names[names.index('Removed theme')] == 'Removed theme'
    assert labels[names.index('Removed theme')] == 'Tag: Removed theme (no current matches)'
    assert labels[0] != labels[names.index('All themes')]

