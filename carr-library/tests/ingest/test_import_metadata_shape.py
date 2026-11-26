"""Public import rejects accidental character-wise author/collection identities."""
import pytest

from carr_library.application import LibraryApp


@pytest.mark.parametrize('suffix', ['.txt', '.pdf', '.docx', '.epub'])
@pytest.mark.parametrize('field,value', [
    ('authors', 'One real author'), ('authors', b'author'), ('authors', {'author': 1}),
    ('authors', ['A'] * 101), ('collections', 'abc'), ('collections', {'scope'}),
    ('collections', b'abc'), ('collections', ['id'] * 101),
])
def test_invalid_public_metadata_is_rejected_before_retaining_or_dispatching(tmp_path, suffix, field, value):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Metadata boundary')
    source = tmp_path / ('source' + suffix)
    source.write_bytes(b'Private synthetic source stays unchanged.')
    with pytest.raises(ValueError):
        app.import_file(workspace, source, **{field: value})
    assert app.browse(workspace) == ()
    assert list(app.assets_dir.iterdir()) == []
    assert not (app.root / 'import-requests').exists()
    assert source.read_bytes() == b'Private synthetic source stays unchanged.'


def test_explicit_metadata_lists_keep_whole_names_and_collection_ids(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Metadata boundary')
    collection = app.create_collection(workspace, 'Selected sources')
    source = tmp_path / 'source.txt'
    source.write_text('The genuine source remains exact.', encoding='utf-8')
    app.import_file(workspace, source, authors=['One complete author'], collections=[collection])
    hit, = app.search(workspace, 'genuine source', collection=collection)
    assert hit.author == 'One complete author'
