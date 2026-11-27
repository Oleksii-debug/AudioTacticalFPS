"""CSL name display roundtrips must never promote a missing name component."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.bibliography_workbench import records_for_style
from carr_library.ui.academic_library_dialog import AcademicController


@pytest.fixture
def libraries(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    first = app.create_workspace('Original CSL import')
    second = app.create_workspace('CSL roundtrip')
    return AcademicController(app, first), AcademicController(app, second)


def roundtrip(libraries, author):
    first, second = libraries
    source = json.dumps([{'type': 'book', 'title': 'Synthetic name reference',
                         'author': [author]}], ensure_ascii=False).encode('utf-8')
    review = first.preview(source, 'csl-json')
    assert first.list() == second.list() == ()
    saved = first.import_selected(review, (0,))
    original = first.get(saved.imported_ids[0])
    exported = first.export(identity=original.id, format='csl-json', include_notes=False)
    assert json.loads(exported)[0]['author'] == [{'literal': name} for name in original.authors]
    repeated = second.preview(exported.encode('utf-8'), 'csl-json')
    assert repeated.entries[0].authors == original.authors
    imported = second.import_selected(repeated, (0,))
    # Read a new application instance to verify the marker is durable metadata,
    # rather than an in-memory property of the first import preview.
    reopened = AcademicController(LibraryApp(first.library.root), second.workspace)
    result = reopened.get(imported.imported_ids[0])
    assert result.authors == original.authors
    assert first.get(original.id) == original
    snapshot = reopened.export(identity=result.id, format='csl-json', include_notes=False)
    return review.entries[0], original.authors, snapshot


@pytest.mark.parametrize(('author', 'display'), [
    ({'given': 'John', 'suffix': 'Jr.'}, 'John, Jr.'),
    ({'family': 'Smith', 'suffix': 'Jr.'}, 'Smith, Jr.'),
    ({'given': 'John', 'non-dropping-particle': 'van', 'suffix': 'Jr.'}, 'van, John, Jr.'),
    ({'family': 'Smith', 'dropping-particle': 'van', 'suffix': 'Jr.'}, 'Smith, van, Jr.'),
    ({'family': '  ', 'given': 'John', 'suffix': 'Jr.'}, '  , John, Jr.'),
    ({'family': 'Smith', 'given': 'John, A.'}, 'Smith, John, A.'),
    ({'family': 'Smith, Jones', 'given': 'John'}, 'Smith, Jones, John'),
    ({'family': 'Smith', 'given': 'John', 'suffix': 'Jr., III'}, 'Smith, John, Jr., III'),
    ({'family': 'Smith', 'given': 'John', 'non-dropping-particle': 'von, den'},
     'von, den Smith, John'),
    ({'given': 'John, A.'}, 'John, A.'),
])
def test_ambiguous_structured_names_remain_literal_after_real_csl_roundtrip(libraries, author, display):
    row, names, snapshot = roundtrip(libraries, author)
    assert names == ('{' + display + '}',)
    assert any('retained literally' in warning and 'not reassigned' in warning
               for warning in row.warnings)
    for mode in ('literal', 'family-given'):
        records, _ = records_for_style(snapshot, name_mode=mode)
        assert records[0]['author'] == [{'literal': display}]


@pytest.mark.parametrize(('author', 'display', 'expected'), [
    ({'family': 'Smith', 'given': 'John', 'suffix': 'Jr.'}, 'Smith, John, Jr.',
     {'family': 'Smith', 'given': 'John', 'suffix': 'Jr.'}),
    ({'family': 'García Márquez', 'given': 'Gabriel José'}, 'García Márquez, Gabriel José',
     {'family': 'García Márquez', 'given': 'Gabriel José'}),
    ({'family': 'Helsing', 'given': 'Abraham', 'non-dropping-particle': 'van'}, 'van Helsing, Abraham',
     {'family': 'van Helsing', 'given': 'Abraham'}),
    ({'family': 'Šimko', 'given': 'Ján', 'suffix': ''}, 'Šimko, Ján',
     {'family': 'Šimko', 'given': 'Ján'}),
    ({'family': 'Plato'}, 'Plato', {'literal': 'Plato'}),
    ({'given': 'Cher'}, 'Cher', {'literal': 'Cher'}),
])
def test_unambiguous_structured_display_and_explicit_roles_remain_compatible(libraries, author, display, expected):
    row, names, snapshot = roundtrip(libraries, author)
    assert names == (display,)
    assert not any('not reassigned' in warning for warning in row.warnings)
    records, _ = records_for_style(snapshot, name_mode='family-given')
    assert records[0]['author'] == [expected]
    literal, _ = records_for_style(snapshot, name_mode='literal')
    assert literal[0]['author'] == [{'literal': display}]


@pytest.mark.parametrize('name', ['Šimko, Ján', 'Research, Inc.', '{Research, Inc.}', '{John, Jr.}'])
def test_existing_literal_author_equality_is_preserved_without_new_wrapping(libraries, name):
    row, names, snapshot = roundtrip(libraries, {'literal': name})
    assert names == (name,)
    assert not any('not reassigned' in warning for warning in row.warnings)
    records, _ = records_for_style(snapshot, name_mode='literal')
    expected = name[1:-1] if name.startswith('{') and name.endswith('}') else name
    assert records[0]['author'] == [{'literal': expected}]
