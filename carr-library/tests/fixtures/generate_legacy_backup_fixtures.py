"""Regenerate synthetic compatibility fixtures with the actual frozen writer.

Usage: python -B generate_legacy_backup_fixtures.py /path/to/5580d4d/carr-library
The frozen source is an explicit input; no legacy engine code is vendored into
the current application to manufacture an allegedly authentic fixture.
"""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED


def offline(*args, **kwargs):
    raise OSError('Fixture generation is strictly offline.')


def main():
    legacy = Path(sys.argv[1]).resolve(strict=True)
    output = Path(__file__).resolve().parent / 'legacy-backups-5580d4d'
    output.mkdir(exist_ok=True)
    socket.socket.connect = socket.create_connection = socket.getaddrinfo = offline
    sys.path[:0] = [str(legacy / 'src'), str(legacy)]
    os.chdir(legacy)
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup
    from carr_library.reader.reading_state import ReadingLocation, ReadingState, _encode
    from carr_library.reader.narration import NarrationStore, new_project, revise
    from carr_library.reader.live_state import LiveState
    from carr_library.storage.revisions import Storage
    from carr_library.studio import ProjectStore, narration_project
    from carr_library.studio.models import Clip
    from carr_library.providers import ProviderManager, ProviderProfile
    from carr_library.agent import LocalAgent, Plan, Step
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request, record_progress

    class NoVault:
        def __getattr__(self, name):
            raise AssertionError('Fixture generation may not access credentials.')

    checkpoint = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=legacy, check=True, capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=legacy, check=True, capture_output=True, text=True).stdout.strip()
    index = dict(source_checkpoint=checkpoint, source_tree=tree, synthetic=True, archives={})
    with TemporaryDirectory(prefix='legacy-backup-fixtures-') as temporary:
        root = Path(temporary)
        for label in ('v3', 'v4', 'v3-empty'):
            app = LibraryApp(root / (label + '-source'))
            workspace = app.create_workspace('Synthetic legacy library')
            metadata = dict(workspace=workspace)
            if label != 'v3-empty':
                app.add_text(workspace, 'Original text', 'Exact source.\r\nУкраїнський текст.')
                with closing(Storage(app.database, schema_version=4)) as storage:
                    row = storage.db.execute('SELECT r.edition,r.id,r.asset,p.ordinal,p.digest,p.raw '
                        'FROM revision r JOIN page p ON p.revision=r.id WHERE r.workspace=?', (workspace,)).fetchone()
                location = ReadingLocation(workspace, row[0], row[1], 'logical_segment', row[3], row[4], row[2])
                ReadingState(app.root).remember(location)
                LiveState(app.root).remember(location, 2, len(row[5]), len(row[5]), 'fixture:voice')
                narration = revise(new_project(row[5], source_location=location), 'Edited narration only.')
                NarrationStore(app.root).save(narration)
                metadata.update(edition=location.edition, original_text=row[5], narration=narration.id)
            else:
                (app.root / 'reading-state.json').write_text(_encode({}, {}), encoding='utf-8')
            if label == 'v4':
                project_id = uuid4().hex
                project = ProjectStore(app.root / 'audio-projects' / project_id)
                plan = narration_project('Exact authored text.\r\nУкраїнська e\u0301.\r\n', 'fixture:voice')
                scene = replace(plan.scenes[0], clips=(Clip('external-cue', str(root / 'unavailable-effect.wav')),))
                plan = replace(plan, scenes=(scene,))
                revision = project.save(plan)
                with project.connect() as db:
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', ('d' * 32, revision, 'running', '{}', None, None, 1.0))
                providers = ProviderManager(app.root / 'provider-settings', vault=NoVault())
                profile = ProviderProfile('example', 'Explicit synthetic profile', 'llm', 'https://example.com/v1',
                    'selected-model', enabled=True, requires_key=False, request_budget_cents=3, total_budget_cents=20)
                providers.save(profile)
                providers.set_mode('HYBRID')
                with closing(sqlite3.connect(providers.database)) as db, db:
                    db.execute('UPDATE profiles SET spent_cents=3,credential_ref=?', ('a' * 64 + '/example/' + 'b' * 32,))
                    db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)', ('c' * 64, profile.id, 'reserved', 3, None, 1.0))
                agent = LocalAgent(app, app.root / 'agent-jobs', workspaces=(workspace,))
                action = Plan('Create a topic', (Step('topic', 'research.create_topic', {'workspace': workspace, 'name': 'Saved topic'}),))
                agent.run(action, approval_sha256=action.digest)
                external = root / 'pending.txt'
                external.write_text('Pending preserved source.\fSecond segment.', encoding='utf-8')
                request = prepare_request(app.root, workspace, external,
                    dict(title='Pending text', authors=[], language=None, edition=None, volume=None,
                         collections=[], work_id=None, edition_id=None), Config())
                result = app._import_file(workspace, external, title='Pending text',
                    work_id=request['metadata']['work_id'], edition_id=request['metadata']['edition_id'],
                    _request_id=request['request_id'], cancelled=lambda: True)
                record_progress(app.root, request['request_id'], state=result['state'], result=result)
                metadata.update(project_id=project_id, project_revision=revision,
                                request_id=request['request_id'], required_audio=str(root / 'unavailable-effect.wav'))
            else:
                (app.root / 'imports.sqlite').unlink(missing_ok=True)
            folder = root / (label + '-backup')
            digest = create_backup(app, folder)
            manifest = json.loads((folder / 'manifest.json').read_text())
            assert manifest['version'] == (4 if label == 'v4' else 3)
            target = output / (label + '.zip')
            with ZipFile(target, 'w', compression=ZIP_DEFLATED) as archive:
                for path in sorted(folder.rglob('*')):
                    name = path.relative_to(folder).as_posix()
                    info = ZipInfo(name + '/' if path.is_dir() else name, date_time=(2026, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = (0o40700 if path.is_dir() else 0o100600) << 16
                    archive.writestr(info, b'' if path.is_dir() else path.read_bytes())
            index['archives'][label] = dict(manifest_sha256=digest, archive_sha256=sha256(target.read_bytes()).hexdigest(), **metadata)
    (output / 'fixtures.json').write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
