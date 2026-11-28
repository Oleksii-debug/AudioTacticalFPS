"""Book selections reuse ReadingDesk and its existing non-authorizing anchor.

Adapted from the reviewed source-selection helper in PR152. This is passive
selection/validation shared by Book plans, not a translation or voice service.
Only exact library identities and source metadata enter saved review records.
"""
from dataclasses import asdict

from ..assistant.models import text
from ..reader.reading_desk import ReadingDesk
from ..reader.ebook_contents import open_ebook, read_chapter
from ..speech.reading import ReadingDocument
from ..speech.source import read_source_range


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
        raise ValueError('The reviewed book source changed.')
    for expected, found in zip(positions, actual['positions']):
        # Independent approval may advance later. This flag describes capture;
        # it never authorizes quotation or changes the retained source identity.
        if any(found[key] != expected[key] for key in expected if key != 'text_verified_at_capture'):
            raise ValueError('The reviewed source position changed.')
    guard()


def validate_project_provenance(value, project):
    """Bind a trusted source callback to every literal Project character."""
    required = {'schema', 'plan', 'plan_sha256', 'source_anchor', 'source_metadata',
                'source_topic', 'warnings', 'source_sha256', 'part', 'notice'}
    from hashlib import sha256
    import re
    if (type(value) is not dict or not required <= set(value) <= required | {'landmarks'}
            or value['schema'] != 'carr-book-audio-source/1'
            or value['notice'] != 'A literal reading derivative; this anchor never authorizes a quotation.'
            or type(value['plan']) is not str or re.fullmatch('[0-9a-f]{32}', value['plan']) is None
            or type(value['plan_sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}', value['plan_sha256']) is None
            or type(value['source_sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}', value['source_sha256']) is None):
        raise ValueError('The guarded book source provenance is invalid.')
    part = value['part']
    if (type(part) is not dict or set(part) != {'index', 'start', 'end', 'text_sha256'}
            or any(type(part[key]) is not int for key in ('index', 'start', 'end'))
            or not 1 <= part['index'] <= 256 or not 0 <= part['start'] < part['end'] <= 2_000_000
            or part['end'] - part['start'] != len(project.original_script)
            or part['text_sha256'] != sha256(project.original_script.encode('utf-8')).hexdigest()):
        raise ValueError('The guarded book part differs from its exact literal Project.')
    cursor = part['start']
    text_parts = []
    prefix = 'carr-book-audio:%s:%s:%d:' % (value['plan'], value['plan_sha256'], part['index'])
    for scene in project.scenes:
        if scene.music is not None or scene.cues:
            raise ValueError('A source-bound Book part cannot introduce authored cues or music.')
        for line in scene.lines:
            end = cursor + len(line.text)
            if line.source_ref != prefix + str(cursor) + ':' + str(end):
                raise ValueError('The book source guard belongs to another production plan.')
            text_parts.append(line.text)
            cursor = end
    if cursor != part['end'] or ''.join(text_parts) != project.original_script:
        raise ValueError('The guarded Book lines do not cover their literal source exactly once.')
    return value
