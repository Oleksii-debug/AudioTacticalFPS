"""Source selections reuse ReadingDesk and the existing non-authorizing anchor.

Only identifiers and exact snapshot metadata travel in jobs. No source pathname
is retained, so canonical library/state backup restores the same binding.
"""
from dataclasses import asdict

from .models import canonical, text
from ..reader.reading_desk import ReadingDesk
from ..reader.ebook_contents import open_ebook, read_chapter
from ..speech.reading import ReadingDocument
from ..speech.source import read_source_range
from ..speech._io import check_cancelled


def source_context(value, anchor=None, metadata=None, topic=None):
    if anchor is None:
        if metadata is not None or topic is not None:
            raise ValueError('Source metadata requires its exact reading-source anchor.')
        return None
    checked = ReadingDocument(value, source_anchor=anchor).source_anchor
    if (type(metadata) is not dict or set(metadata) != {'author', 'title', 'volume', 'edition'}
            or any(item is not None and (type(item) is not str or len(item) > 16384 or '\x00' in item)
                   for item in metadata.values())):
        raise ValueError('Invalid original source metadata.')
    if topic is not None:
        text(topic, 'source topic', maximum=1024)
    return {'source_anchor': checked, 'source_metadata': dict(metadata), 'source_topic': topic}


def capture_source(library, workspace, edition, *, topic=None, first=None, last=None,
                   chapter=None, revision=None, cancelled=lambda: False, guard=lambda: None):
    guard()
    desk = ReadingDesk(library, workspace, edition, topic=topic, revision=revision, cancelled=cancelled)
    if chapter is not None:
        if first is not None or last is not None or type(chapter) is not int:
            raise ValueError('Choose an EPUB chapter or a source-position range, not both.')
        read_chapter(library, workspace, edition, desk.revision, chapter,
                     authorized_topic=topic, cancelled=cancelled)
        first = last = chapter
    elif first is None and last is None:
        first, last = 1, len(desk.pages)
    elif first is None or last is None:
        raise ValueError('Choose both the first and last source reading positions.')
    selected = read_source_range(desk, first, last, guard=guard, cancelled=cancelled)
    context = source_context(selected.text, selected.reading_document().source_anchor,
                             asdict(desk.metadata), topic)
    return {'text': selected.text, 'title': desk.title, 'warnings': list(selected.warnings), **context}


def source_options(library, workspace, edition, *, topic=None, cancelled=lambda: False):
    desk = ReadingDesk(library, workspace, edition, topic=topic, cancelled=cancelled)
    labels = [page.description for page in desk.pages]
    try:
        contents = open_ebook(library, workspace, edition, authorized_topic=topic, cancelled=cancelled)
    except ValueError:
        # Ordinary documents have reading positions without EPUB navigation.
        pass
    else:
        if contents['revision'] != desk.revision or len(contents['chapters']) != len(desk.pages):
            raise ValueError('The electronic-book contents changed. Reopen the source.')
        labels = ['EPUB chapter %d: %s' % (row['ordinal'], row['title']) for row in contents['chapters']]
    return {'title': desk.title, 'revision': desk.revision, 'positions': labels}


def validate_source(library, value, anchor, metadata, topic=None, *,
                    cancelled=lambda: False, guard=lambda: None):
    context = source_context(value, anchor, metadata, topic)
    if context is None:
        return
    if library is None:
        raise ValueError('Open this source-bound job from its existing library.')
    guard()
    positions = context['source_anchor']['positions']
    current = capture_source(library, anchor['workspace'], anchor['edition'], topic=topic,
        first=positions[0]['ordinal'], last=positions[-1]['ordinal'], revision=anchor['revision'],
        cancelled=cancelled, guard=guard)
    actual = current['source_anchor']
    if (current['text'] != value or current['source_metadata'] != metadata
            or any(actual[key] != anchor[key] for key in anchor if key != 'positions')
            or len(actual['positions']) != len(positions)):
        raise ValueError('The reviewed translation source changed.')
    for expected, found in zip(positions, actual['positions']):
        # Independent approval may advance later. This flag describes capture;
        # it never authorizes quotation or changes the retained source identity.
        if any(found[key] != expected[key] for key in expected if key != 'text_verified_at_capture'):
            raise ValueError('The reviewed source position changed.')
    guard()


def read_text_file(path, *, cancelled=lambda: False):
    """Capture one explicit bounded UTF-8 file through the shared regular reader."""
    from ..backup_sidecars import read_regular
    from .models import local_path
    check_cancelled(cancelled)
    selected = local_path(path)
    raw = read_regular(selected, 8_000_000)
    check_cancelled(cancelled)
    value = raw.decode('utf-8', errors='strict')
    text(value, 'selected translation text', maximum=2_000_000)
    return value
