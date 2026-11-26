"""Hierarchical research exports use real historical source verification."""
from dataclasses import replace
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.research.evidence_export import _block
from carr_library.research.topic_outline_export import export_outline


@pytest.fixture
def outline(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    with app.topic_transaction(workspace) as tree:
        tree.create('root', 'Research question')
        tree.create('second', 'Second', parent='root', position=2)
        tree.create('first', 'First', parent='root', position=1)
        tree.create('deep', 'Depth', parent='first')
    raw = 'Railway evidence\r\n```\n# Not a heading\n=SUM(1,2) — цитата.'
    imported = app.add_text(workspace, 'Original study', raw, authors=('Author',), edition='First', volume='II')
    hit, = app.search(workspace, 'Railway')
    with app.read(workspace) as db:
        anchor, _, _ = app._anchor(db, workspace, hit.ref, hit.revision)
    anchor = replace(anchor, start=0, end=len(raw), quote=raw)
    with app.evidence_transaction(workspace) as store:
        store.save('first', 'e1', anchor, 'My interpretation, not source text.')
        store.save('deep', 'e1', anchor, 'A separate topic note.')
    app.set_evidence_tags(workspace, 'first', 'e1', ('Method', 'History'))
    return app, workspace, imported, anchor


@pytest.mark.parametrize('format', ['text', 'markdown'])
def test_hierarchy_exact_quotes_and_separate_notes(outline, format):
    app, workspace, imported, anchor = outline
    app.update_metadata(workspace, imported['edition'], title='Changed study', authors=('New author',))
    result = export_outline(app, workspace, 'root', format=format)
    headings = [line for line in result.splitlines() if 'Section ' in line and not line.startswith('Topic')]
    assert len(headings) == 4
    assert [part in line for part, line in zip(('Section 1:', 'Section 1.1:', 'Section 1.1.1:', 'Section 1.2:'), headings)] == [True] * 4
    assert 'Original study' in result and 'Changed study' not in result
    assert 'Research note' in result and 'Researcher tags' in result
    assert 'No matching saved evidence in this section.' in result
    assert anchor.identity.revision in result and 'Segment 1 (unpaginated)' in result
    if format == 'text':
        quotes = [json.loads(line.removeprefix('Exact quotation: ')) for line in result.splitlines()
                  if line.startswith('Exact quotation: ')]
        assert quotes == [anchor.quote, anchor.quote]
        notes = [json.loads(line.removeprefix('Research note: ')) for line in result.splitlines()
                 if line.startswith('Research note: ')]
        assert notes == ['My interpretation, not source text.', 'A separate topic note.']
    else:
        assert result.count(_block(anchor.quote)) == 2
        assert '\n# Not a heading\n' not in result


def test_filtered_outline_keeps_structure_and_current_scope(outline):
    app, workspace, _, _ = outline
    result = export_outline(app, workspace, 'root', format='text', tag_filter='Method')
    assert 'Section 1.1.1:' in result and 'A separate topic note.' not in result
    assert 'My interpretation, not source text.' in result
    assert 'Exact tag filter: "Method"' in result
    own = export_outline(app, workspace, 'root', include_descendants=False)
    assert 'Original study' not in own and 'No matching saved evidence' in own
    with app.topic_transaction(workspace) as tree:
        tree.archive('first')
    result = export_outline(app, workspace, 'root', format='text')
    assert 'Original study' not in result and 'Depth' not in result


def test_unsafe_section_names_are_encoded_not_markup(outline):
    app, workspace, _, _ = outline
    name = 'Injected\n# heading <script> | ```'
    with app.topic_transaction(workspace) as tree:
        tree.rename('first', name)
    result = export_outline(app, workspace, 'root')
    assert '\n# heading' not in result
    assert '"Injected\\n# heading' in result
    assert result.count('## Section 1:') == 1


def test_heading_pipe_is_exact_json_outside_a_markdown_table(outline):
    app, workspace, _, _ = outline
    with app.topic_transaction(workspace) as tree:
        tree.rename('first', 'a|b')
    result = export_outline(app, workspace, 'root')
    heading = next(line for line in result.splitlines() if line.startswith('### Section 1.1:'))
    assert json.loads(heading.split(': ', 1)[1].strip('` ')) == 'a|b'


def test_deep_outline_keeps_explicit_section_numbers(outline):
    app, workspace, _, _ = outline
    parent = 'deep'
    with app.topic_transaction(workspace) as tree:
        for index in range(7):
            child = f'deeper-{index}'
            tree.create(child, f'Deeper {index}', parent=parent)
            parent = child
    result = export_outline(app, workspace, 'root')
    assert 'Section 1.1.1.1.1.1.1.1.1.1:' in result
    assert '\n#######' not in result


@pytest.mark.parametrize('format', ['csv', 'ris', 'bibtex', '', None])
def test_outline_rejects_unsupported_format_before_read(outline, monkeypatch, format):
    app, workspace, _, _ = outline
    monkeypatch.setattr(app, 'read', lambda *_: pytest.fail('Unsupported format read the store'))
    with pytest.raises(ValueError):
        export_outline(app, workspace, 'root', format=format)


def test_outline_cancel_does_not_return_partial_export(outline):
    app, workspace, _, _ = outline
    with pytest.raises(TimeoutError):
        export_outline(app, workspace, 'root', cancelled=lambda: True)
