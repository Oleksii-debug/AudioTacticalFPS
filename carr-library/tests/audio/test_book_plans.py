"""Current plans preserve source bytes; synthetic plans are not voice qualification."""
from dataclasses import replace
from hashlib import sha256

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.book_plan import BookPlan, SplitOptions, capture_plan, split_parts, encode
from carr_library.audio.book_batch import select_books, validate_plans, BookBatch
from carr_library.audio.models import Role, Line, Effect
from carr_library.reader.book_source import capture_source
from carr_library.reader.narration_text import NarrationDraft


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Books')
    first = app.add_text(workspace, 'Книжка ü', '[music ../secret]\nFirst. Second!\n\nОстанній абзац.',
                         authors=('Автор',), language='uk')
    second = app.add_text(workspace, 'Second', 'Second book.', authors=('Other',), language='en')
    return app, workspace, first, second


def make_plan(library, *, item=None, **options):
    app, workspace, first, second = library
    return capture_plan(app, workspace, (item or first)['edition'],
        options=SplitOptions(**options),
        role=Role('Narrator', voice='fixture:uk', language='uk', pan=-.2, rate=1,
                  effects=(Effect('highpass', {'frequency': 100}),)),
        sample_rate=8000)


@pytest.mark.parametrize('mode', ['document', 'paragraphs', 'sentences', 'blank-lines', 'characters'])
def test_lossless_split_literal_project_and_roundtrip(library, mode):
    plan = make_plan(library, mode=mode, characters=12)
    restored = BookPlan.from_dict(plan.to_dict())
    assert restored.fingerprint == plan.fingerprint
    assert ''.join(plan.source['text'][part.start:part.end] for part in plan.parts) == plan.source['text']
    for index in range(1, len(plan.parts) + 1):
        production = plan.project(index)
        assert production.input_kind == 'narration' and production.export_stems is False
        assert production.roles == (plan.role,)
        assert all(isinstance(event, Line) and event.literal
                   for scene in production.scenes for event in scene.events)
        document = plan.document(index)
        assert document['output_dir'] is None
        assert NarrationDraft.from_dict(document['draft']).text == production.original_script
        assert plan.reference(index)['project_id'] == plan.job_id(index)
    assert plan.provenance(1)['notice'].endswith('never authorizes a quotation.')


def test_batch_exact_order_and_budget_are_retained_without_output_permission(library):
    a = make_plan(library)
    b = make_plan(library, item=library[3])
    batch = select_books((b, a), title='Reviewed order')
    assert [plan.id for plan in validate_plans(batch, (b, a))] == [b.id, a.id]
    assert BookBatch.from_dict(batch.to_dict()) == batch
    with pytest.raises(ValueError, match='order|changed'):
        validate_plans(batch, (a, b))
    with pytest.raises(ValueError):
        select_books((a, a))
    assert 'output' not in encode(batch.to_dict()).decode()
    assert 'runtime' not in encode(batch.to_dict()).decode()


@pytest.mark.parametrize('template', ['{book}', '../{index}', '{index.__class__}', '{book[0]}-{index}', '{index!r}'])
def test_names_never_evaluate_expressions_or_paths(template):
    with pytest.raises(ValueError):
        SplitOptions(name_template=template)


def test_logical_segments_cannot_be_claimed_as_physical_pages_or_epub(library):
    with pytest.raises(ValueError, match='physical'):
        make_plan(library, mode='pages')
    with pytest.raises(ValueError):
        make_plan(library, mode='chapters')


def test_actual_owned_regex_worker_preserves_exact_ranges(library):
    plan = make_plan(library, mode='regex', pattern=r'[.!]\s*')
    assert len(plan.parts) >= 2
    assert ''.join(plan.project(i).original_script for i in range(1, len(plan.parts) + 1)) == plan.source['text']


def test_split_boundary_budget_is_independent_of_character_budget(library):
    app, workspace, *_ = library
    item = app.add_text(workspace, 'Bounded', 'a' * 300)
    source = capture_source(app, workspace, item['edition'])
    with pytest.raises(ValueError, match='256'):
        split_parts(source, SplitOptions(mode='characters', characters=1))
    with pytest.raises(InterruptedError):
        split_parts(source, SplitOptions(), cancelled=lambda: True)


def test_forged_plan_source_and_noncurrent_schema_refused(library):
    plan = make_plan(library)
    value = plan.to_dict()
    value['source']['text'] += 'forged'
    with pytest.raises(ValueError):
        BookPlan.from_dict(value)
    value = plan.to_dict()
    value['schema'] = 1
    with pytest.raises(ValueError, match='Unsupported'):
        BookPlan.from_dict(value)


def test_pure_decoders_bound_repeated_lists_before_encoding(library, monkeypatch):
    plan = make_plan(library)
    batch = select_books((plan,))
    import carr_library.audio.book_plan as module
    value = plan.to_dict()
    value['parts'] = value['parts'] * 257
    monkeypatch.setattr(module, 'encode', lambda value: pytest.fail('Bound before serializing'))
    with pytest.raises(ValueError, match='Unsupported'):
        BookPlan.from_dict(value)
    value = batch.to_dict()
    value['items'] = value['items'] * 33
    with pytest.raises(ValueError, match='Invalid'):
        BookBatch.from_dict(value)
