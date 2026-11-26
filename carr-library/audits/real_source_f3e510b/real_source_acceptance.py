"""Scratch-only, real-source acceptance; never writes source text to a repo.

Inputs and expected spans come from an independently retrieved manifest. The
result contains identities, hashes and assertion counts, never quotations.
"""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup


checks = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)


def rejects(name, callback):
    try:
        callback()
    except (ValueError, OSError):
        checks.append(name)
        return
    raise AssertionError(name)


def run(manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    source_rows = manifest['sources']
    results = []
    with TemporaryDirectory(prefix='carr-real-source-acceptance-') as temporary:
        root = Path(temporary)
        app = LibraryApp(root / 'library')
        cases = []
        for row in source_rows:
            path = Path(row['source_path'])
            original = path.read_bytes()
            raw = original.decode('utf-8')
            prefix = row['id'] + ': '
            check(prefix + 'input digest', sha256(original).hexdigest() == row['source_sha256'])
            check(prefix + 'independent expected span', raw[row['start']:row['end']] == row['expected_match'])
            workspace = app.create_workspace(row['id'], 'Real public-domain source technical acceptance')
            collection = app.create_collection(workspace, 'Primary texts')
            topic = app.create_topic(workspace, 'Evidence')
            section = app.create_topic(workspace, 'Selected passage', parent=topic)
            imported = app.import_file(workspace, path, title=row['title'],
                authors=(row['author'],), language=row['language'], edition=row['edition'],
                volume=row['volume'], collections=(collection,))
            check(prefix + 'published verified source', imported['state'] == 'complete' and imported['verified'] is True)
            check(prefix + 'retained bytes exact', app.assets.resolve(imported['asset']).read_bytes() == original)
            check(prefix + 'original input unchanged', path.read_bytes() == original)
            book, = app.browse(workspace, collection=collection)
            check(prefix + 'bibliographic metadata', book.title == row['title'] and book.authors == (row['author'],))
            hits = app.search(workspace, row['query'], mode='phrase', collection=collection)
            check(prefix + 'unique scoped phrase', len(hits) == row['expected_matches'])
            hit, = hits
            check(prefix + 'source exact quotation', hit.verified and hit.quote == row['expected_match'])
            context = app.context(workspace, hit.ref, hit.revision)
            page = context.pages[0]
            check(prefix + 'source context and offsets', page.raw_text == raw and
                (page.anchor.start, page.anchor.end) == (row['start'], row['end']))
            check(prefix + 'honest unpaginated locator', page.anchor.locator.kind == 'logical_segment' and
                page.anchor.locator.printed is None and not page.anchor.locator.verified)
            check(prefix + 'fresh exact copy', app.copy(workspace, hit.ref, hit.revision, 'quote') == row['expected_match'])
            check(prefix + 'correct author and title', row['author'] in hit.citation and row['title'] in hit.citation)
            note = 'Independent acceptance note; separate from original text.'
            evidence_id = app.save_hit(workspace, section, hit.ref, hit.revision, note)
            app.set_evidence_tags(workspace, section, evidence_id, ('Primary', row['language']))
            saved, = app.evidence(workspace, section)
            check(prefix + 'saved evidence and note', saved.anchor.quote == row['expected_match'] and saved.note == note)
            check(prefix + 'tags persisted', set(app.evidence_tags(workspace, section, evidence_id)) == {'Primary', row['language']})
            query_id = app.save_query(workspace, section, 'Phrase in evidence', row['query'])
            check(prefix + 'saved query resolves evidence',
                app.run_query(workspace, section, query_id)[0].evidence.evidence_id == evidence_id)
            exports = {fmt: app.export_topic(workspace, section, format=fmt) for fmt in ('text', 'markdown', 'ris')}
            check(prefix + 'export attribution', all(row['title'] in value and row['author'] in value for value in exports.values()))
            check(prefix + 'export source-note separation',
                all('Exact quotation' in exports[fmt] and 'Research note' in exports[fmt] for fmt in ('text', 'markdown')) and
                note not in exports['ris'])
            with app.catalog_transaction(workspace) as catalog:
                catalog.remove(collection, imported['edition'])
            check(prefix + 'collection removal updates search', app.search(workspace, row['query'], collection=collection) == ())
            rejects(prefix + 'stale collection copy rejected', lambda: app.copy(workspace, hit.ref, hit.revision, 'quote'))
            check(prefix + 'saved citation survives collection change', app.evidence(workspace, section) == (saved,))
            check(prefix + 'exports survive collection change', all(app.export_topic(workspace, section, format=fmt) == value for fmt, value in exports.items()))
            cases.append(dict(row=row, workspace=workspace, section=section, saved=saved,
                evidence_id=evidence_id, query_id=query_id, exports=exports, imported=imported))
            results.append({key: row[key] for key in ('id', 'title', 'author', 'language', 'source_url', 'source_sha256', 'source_bytes')})

        first, second = cases
        check('cross-library phrase isolation', app.search(second['workspace'], first['row']['query']) == () and
              app.search(first['workspace'], second['row']['query']) == ())
        own_hit, = app.search(first['workspace'], first['row']['query'])
        rejects('foreign selection authorization', lambda: app.copy(second['workspace'], own_hit.ref, own_hit.revision))
        for case in cases:
            reopened = LibraryApp(app.root)
            check(case['row']['id'] + ': reopen evidence', reopened.evidence(case['workspace'], case['section']) == (case['saved'],))
        app.archive_workspace(second['workspace'])
        rejects('archived workspace denies retrieval', lambda: app.search(second['workspace'], second['row']['query']))
        bundle = root / 'backup'
        manifest_digest = app.backup(bundle)
        check('backup manifest bound', sha256((bundle / 'manifest.json').read_bytes()).hexdigest() == manifest_digest)
        moved_bundle = root / 'moved backup'
        shutil.move(str(bundle), moved_bundle)
        restored_path = restore_backup(moved_bundle, root / 'restored library', manifest_digest)
        restored = LibraryApp(restored_path)
        check('backup retains archived workspace', len(restored.workspaces()) == 1 and len(restored.workspaces(include_archived=True)) == 2)
        restored.restore_workspace(second['workspace'])
        for case in cases:
            row, workspace, section = case['row'], case['workspace'], case['section']
            prefix = row['id'] + ': restored '
            check(prefix + 'evidence exact', restored.evidence(workspace, section) == (case['saved'],))
            check(prefix + 'exports byte-identical', all(restored.export_topic(workspace, section, format=fmt) == value for fmt, value in case['exports'].items()))
            check(prefix + 'saved query', restored.run_query(workspace, section, case['query_id'])[0].evidence.evidence_id == case['evidence_id'])
            check(prefix + 'tags', set(restored.evidence_tags(workspace, section, case['evidence_id'])) == {'Primary', row['language']})
            hit, = restored.search(workspace, row['query'])
            check(prefix + 'fresh copy', restored.copy(workspace, hit.ref, hit.revision, 'quote') == row['expected_match'])
            asset = restored.assets.resolve(case['imported']['asset'])
            data = asset.read_bytes()
            asset.write_bytes(b'changed source for negative acceptance')
            rejects(prefix + 'source drift blocks copying', lambda: restored.copy(workspace, hit.ref, hit.revision, 'quote'))
            asset.write_bytes(data)
            check(prefix + 'source restored', restored.copy(workspace, hit.ref, hit.revision, 'quote') == row['expected_match'])
        rejects('wrong manifest digest rejected', lambda: restore_backup(moved_bundle, root / 'wrong digest', '0' * 64))
    return dict(status='PASS', checks=len(checks), check_names=checks, sources=results,
                limits=['Derived UTF-8 texts are the imported authority; no PDF print-page verification.',
                        'Bounded source technical acceptance only; Windows/NVDA not executed.'])


if __name__ == '__main__':
    with (patch('socket.socket.connect', side_effect=AssertionError('Offline acceptance forbids network')),
          patch('socket.create_connection', side_effect=AssertionError('Offline acceptance forbids network'))):
        result = run(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'checks': result['checks'], 'sources': len(result['sources'])}))
