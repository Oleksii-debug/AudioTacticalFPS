"""Actual process exits across current topic transaction/publication checkpoints."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import carr_library
from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step, topic_tools


@pytest.fixture
def environment(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Process crash fixture')
    topic = app.create_topic(workspace, 'Existing evidence topic')
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    return app, workspace, topic, outputs, AssistantService(app)


CHILD = r'''
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, topic_tools
from carr_library.research import export_file
from carr_library.research.topic_tree import TopicTree
app = LibraryApp(sys.argv[2])
service = AssistantService(app)
identity, phase, target = sys.argv[3], sys.argv[4], Path(sys.argv[5])
print(json.dumps({'isolated': bool(sys.flags.isolated),
    'topic_sha256': hashlib.sha256(Path(topic_tools.__file__).read_bytes()).hexdigest()}), flush=True)
save = service.store.save
def checkpoint(value):
    row = value['steps'][0]
    if phase in ('topic-committed', 'export-returned') and row['status'] == 'complete':
        os._exit(73)
    save(value)
    prepared = row['intent'].get('topic_publication')
    if prepared and prepared['staging']:
        if phase == 'export-prepared':
            os._exit(73)
        if phase == 'export-linked-twin':
            os.link(target.parent / prepared['staging']['name'], target)
            os._exit(73)
service.store.save = checkpoint
if phase == 'topic-uncommitted':
    create = TopicTree.create
    def interrupted(self, *args, **kwargs):
        create(self, *args, **kwargs)
        os._exit(73)
    TopicTree.create = interrupted
if phase == 'export-published':
    method = 'rename' if os.name == 'nt' else 'link'
    publish = getattr(export_file.os, method)
    def committed(source, destination, *args, **kwargs):
        result = publish(source, destination, *args, **kwargs)
        if Path(destination) == target:
            os._exit(73)
        return result
    setattr(export_file.os, method, committed)
result = service.run(identity)
print(result, flush=True)
raise SystemExit(99)
'''


def crash(environment, phase):
    app, workspace, topic, outputs, service = environment
    if phase.startswith('topic-'):
        step = Step('topic.create', {'name': 'Created exactly once'})
    else:
        step = Step('topic.export', {'topic': topic, 'name': 'once.md'})
    identity = service.submit(Plan(Scope(workspace, (), outputs), (step,)))
    source = Path(carr_library.__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', CHILD,
        str(source), str(app.root), identity, phase, str(outputs / 'once.md')],
        cwd=outputs.parent, capture_output=True, text=True, encoding='utf-8', timeout=60,
        check=False)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)
    origin = json.loads(result.stdout.splitlines()[0])
    assert origin == {'isolated': True,
        'topic_sha256': sha256(Path(topic_tools.__file__).read_bytes()).hexdigest()}
    assert service.store.load(identity)['steps'][0]['status'] == 'running'
    return identity


@pytest.mark.parametrize('phase', ['topic-uncommitted', 'topic-committed'])
def test_real_process_exit_around_topic_commit_recovers_one_deterministic_topic(environment, phase):
    app, workspace, topic, _, service = environment
    identity = crash(environment, phase)
    assert len(app.topics(workspace)) == (1 if phase == 'topic-uncommitted' else 2)
    reopened = AssistantService(LibraryApp(app.root))
    report = reopened.resume(identity)
    assert report.status == 'complete', report
    assert report.results[0]['topic_id'] == topic_tools.topic_identity(identity, 0)
    assert len(app.topics(workspace)) == 2
    assert reopened.resume(identity).status == 'complete'
    assert len(app.topics(workspace)) == 2


@pytest.mark.parametrize('phase', [
    'export-prepared', 'export-published', 'export-returned', 'export-linked-twin',
])
def test_real_process_exit_before_and_after_publication_recovers_only_owned_bytes(environment, phase):
    app, workspace, topic, outputs, service = environment
    identity = crash(environment, phase)
    target = outputs / 'once.md'
    expected = app.export_topic(workspace, topic).encode('utf-8')
    if phase == 'export-prepared':
        assert not target.exists()
    else:
        assert target.read_bytes() == expected
    if phase == 'export-linked-twin' or (phase == 'export-published' and os.name != 'nt'):
        assert target.stat().st_nlink == 2
    record_before = service.store.path(identity).read_bytes()
    service.get(identity)
    assert service.store.path(identity).read_bytes() == record_before, 'Inspection cannot reconcile a publication.'
    report = AssistantService(LibraryApp(app.root)).resume(identity)
    assert report.status == 'complete', report
    assert report.results[0]['recovered'] is (phase != 'export-prepared')
    assert target.read_bytes() == expected and target.stat().st_nlink == 1
    assert not list(outputs.glob('.research-export-*'))
    before = target.stat()
    assert service.resume(identity).status == 'complete'
    assert target.stat().st_ino == before.st_ino and target.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize('damage', ['foreign-same-bytes', 'changed-content', 'extra-link', 'parent-replaced'])
def test_postcrash_output_cannot_be_replaced_or_adopted_by_hash_only(environment, damage):
    app, _, _, outputs, service = environment
    identity = crash(environment, 'export-returned')
    target = outputs / 'once.md'
    raw = target.read_bytes()
    if damage == 'foreign-same-bytes':
        replacement = outputs / 'independent'
        replacement.write_bytes(raw)
        os.replace(replacement, target)
    elif damage == 'changed-content':
        target.write_bytes(raw + b'\nIndependent edit')
    elif damage == 'extra-link':
        os.link(target, outputs / 'unaccounted-link.md')
    else:
        outputs.rename(outputs.with_name('previous-output-directory'))
        outputs.mkdir()
        target.write_bytes(raw)
    before = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink, p.stat().st_mtime_ns)
              for p in outputs.iterdir()}
    report = AssistantService(LibraryApp(app.root)).resume(identity)
    assert report.status in ('failed', 'attention') and report.completed == 0
    assert {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink, p.stat().st_mtime_ns)
            for p in outputs.iterdir()} == before


@pytest.mark.parametrize('damage', ['changed-twin', 'replaced-stage', 'extra-link'])
def test_real_link_publication_crash_rejects_unknown_or_changed_twins_without_cleanup(environment, damage):
    app, _, _, outputs, service = environment
    identity = crash(environment, 'export-linked-twin')
    target = outputs / 'once.md'
    staged, = outputs.glob('.research-export-*')
    if damage == 'changed-twin':
        staged.write_bytes(b'Changed bytes through the surviving stage link')
    elif damage == 'replaced-stage':
        raw = staged.read_bytes()
        staged.unlink()
        staged.write_bytes(raw)
    else:
        os.link(target, outputs / 'third-link.md')
    before = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink, p.stat().st_mtime_ns)
              for p in outputs.iterdir()}
    result = AssistantService(LibraryApp(app.root)).resume(identity)
    assert result.status in ('failed', 'attention') and result.completed == 0
    assert {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink, p.stat().st_mtime_ns)
            for p in outputs.iterdir()} == before


@pytest.mark.parametrize('damage', ['new-evidence', 'foreign-same-bytes'])
def test_prepared_crash_refuses_changed_evidence_or_an_unowned_destination(environment, damage):
    app, workspace, topic, outputs, service = environment
    identity = crash(environment, 'export-prepared')
    staged, = outputs.glob('.research-export-*')
    target = outputs / 'once.md'
    if damage == 'new-evidence':
        app.add_text(workspace, 'Later evidence', 'Additional railway source')
        hit, = app.search(workspace, 'railway')
        app.save_hit(workspace, topic, hit.ref, hit.revision, note='New note after preparation')
    else:
        target.write_bytes(staged.read_bytes())
    before = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink, p.stat().st_mtime_ns)
              for p in outputs.iterdir()}
    result = AssistantService(LibraryApp(app.root)).resume(identity)
    assert result.status in ('failed', 'attention') and result.completed == 0
    assert {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink, p.stat().st_mtime_ns)
            for p in outputs.iterdir()} == before


def test_committed_link_cleanup_failure_remains_complete_with_an_owned_notice(environment, monkeypatch):
    app, _, _, outputs, service = environment
    identity = crash(environment, 'export-linked-twin')
    target = outputs / 'once.md'
    stage, = outputs.glob('.research-export-*')
    raw, inode = target.read_bytes(), target.stat().st_ino
    unlink = Path.unlink
    def refuse_own_stage(path, *args, **kwargs):
        if path == stage:
            raise OSError('Synthetic owned staging cleanup failure')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patched:
        patched.setattr(Path, 'unlink', refuse_own_stage)
        report = service.resume(identity)
        assert report.status == 'complete' and report.completed == 1
        assert report.results[0]['recovered'] is True
        assert 'saved' in report.results[0]['cleanup_warning'] and 'cleanup' in report.results[0]['cleanup_warning']
        assert stage.exists() and target.stat().st_nlink == 2
        assert target.read_bytes() == raw and target.stat().st_ino == inode
    assert AssistantService(LibraryApp(app.root)).resume(identity).status == 'complete'
    assert not stage.exists() and target.stat().st_nlink == 1
    assert target.read_bytes() == raw and target.stat().st_ino == inode
