"""Reject unbounded metadata before either inline or isolated import dispatch."""
import pytest

from carr_library.application import LibraryApp


class UnreadableIterable:
    def __iter__(self):
        raise AssertionError('metadata iterable must not be consumed')


def forbidden_generator():
    raise AssertionError('metadata generator must not be consumed')
    yield 'unreachable'


def invalid_values(kind):
    return {
        'string': lambda: 'Олена Коваль',
        'bytes': lambda: b'Author',
        'mapping': lambda: {'Author': 'value'},
        'set': lambda: {'Author'},
        'range': lambda: range(3),
        'generator': forbidden_generator,
        'iterable': UnreadableIterable,
        'none': lambda: None,
        'number': lambda: 7,
        'long-list': lambda: ['Author'] * 101,
        'long-tuple': lambda: ('Author',) * 101,
    }[kind]()


@pytest.mark.parametrize('suffix', ['txt', 'pdf'])
@pytest.mark.parametrize('field', ['authors', 'collections'])
@pytest.mark.parametrize('kind', [
    'string', 'bytes', 'mapping', 'set', 'range', 'generator', 'iterable',
    'none', 'number', 'long-list', 'long-tuple',
])
def test_invalid_metadata_never_reaches_an_import_backend(tmp_path, monkeypatch, suffix, field, kind):
    from carr_library.ingest import worker

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Metadata boundary')
    calls = []

    def unexpected_dispatch(*args, **kwargs):
        calls.append((args, kwargs))
        return {'state': 'unexpected dispatch'}

    monkeypatch.setattr(app, '_import_file', unexpected_dispatch)
    monkeypatch.setattr(worker, 'run_request', unexpected_dispatch)
    with pytest.raises(ValueError, match='invalid ' + field):
        app.import_file(workspace, tmp_path / ('selected.' + suffix),
                        **{field: invalid_values(kind)})
    assert calls == []
    assert app.browse(workspace) == ()
    assert tuple(app.assets_dir.iterdir()) == ()
    assert not (app.root / 'import-requests').exists()


@pytest.mark.parametrize('suffix', ['txt', 'pdf'])
@pytest.mark.parametrize('sequence', [list, tuple], ids=['list', 'tuple'])
@pytest.mark.parametrize('count', [0, 2, 100])
def test_bounded_unicode_sequences_preserve_order_on_both_routes(
        tmp_path, monkeypatch, suffix, sequence, count):
    from carr_library.ingest import worker

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Unicode metadata')
    authors = sequence(('Олена Коваль', 'Ján Novák') * (count // 2))
    collections = sequence(('Залізниці', 'Města') * (count // 2))
    captured = []
    expected = {'state': 'accepted'}

    def inline(selected_workspace, path, **metadata):
        captured.append(('inline', selected_workspace, metadata))
        return expected

    def isolated(root, selected_workspace, path, metadata, config, **options):
        captured.append(('isolated', selected_workspace, metadata))
        return expected

    monkeypatch.setattr(app, '_import_file', inline)
    monkeypatch.setattr(worker, 'run_request', isolated)
    source = tmp_path / ('selected.' + suffix)
    source.write_bytes(b'Synthetic selected input for metadata routing.\n')
    assert app.import_file(workspace, source,
                           authors=authors, collections=collections) is expected
    route, selected_workspace, metadata = captured.pop()
    assert captured == []
    assert route == ('inline' if suffix == 'txt' else 'isolated')
    assert selected_workspace == workspace
    assert metadata['authors'] == tuple(authors)
    assert metadata['collections'] == tuple(collections)
    assert isinstance(metadata['authors'], tuple) and isinstance(metadata['collections'], tuple)


@pytest.mark.parametrize('sequence', [list, tuple], ids=['list', 'tuple'])
def test_valid_unicode_metadata_is_stored_by_real_import(tmp_path, sequence):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Українська бібліотека')
    collection = app.create_collection(workspace, 'Історія залізниць')
    source = tmp_path / 'source.txt'
    source.write_text('Міст railway history.', encoding='utf-8')
    authors = sequence(['Олена Коваль', 'Ján Novák'])
    result = app.import_file(workspace, source, title='Історія міста',
                             authors=authors, collections=sequence([collection]))
    assert result['state'] == 'complete' and result['verified']
    book, = app.browse(workspace, collection=collection)
    assert book.title == 'Історія міста'
    assert book.authors == tuple(authors)
