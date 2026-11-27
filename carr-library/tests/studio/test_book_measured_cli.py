"""Actual Book CLI processes for measured publication; source and installed origins agree."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.studio import book_cli, publishing
from carr_library.studio.book_cli import capture_request
from carr_library.audio import Effect

_spec = importlib.util.spec_from_file_location('book_measured_cli_fixtures', Path(__file__).with_name('test_measured_audio.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
book = _fixture.book
rendered = _fixture.rendered


def invoke(r, *arguments, crash=False):
    import bootstrap
    import carr_library
    roots = dict.fromkeys(str(Path(module.__file__).resolve().parent.parent)
                          for module in (carr_library, bootstrap))
    script = (
        "import os,sys;from pathlib import Path;"
        "from carr_library.studio import book_cli,publishing;"
        "assert Path(book_cli.__file__).resolve()==Path(sys.argv[1]).resolve();"
    )
    if crash:
        script += (
            "\nreal=publishing.publish_file\n"
            "def publish(*a,**k):\n real(*a,**k)\n os._exit(73)\n"
            "publishing.publish_file=publish\n"
        )
    script += "\nraise SystemExit(book_cli.main(sys.argv[2:]))\n"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(roots), PYTHONDONTWRITEBYTECODE='1')
    args = [sys.executable, '-B', '-c', script, str(Path(book_cli.__file__).resolve()),
            '--data-dir', str(r.e.app.root), '--workspace', r.e.workspace]
    if r.e.topic is not None:
        args.extend(('--topic', r.e.topic))
    args.extend(map(str, arguments))
    return subprocess.run(args, cwd=r.e.app.root.parent, env=env,
        capture_output=True, text=True, encoding='utf-8', timeout=90)


def options(r):
    return [r.plan.id, '--part', 1, '--output', r.output, '--maximum-ms', 70]


def test_actual_request_captures_typed_ordered_effects_without_mutable_aliases(book):
    e = book
    effect = Effect('gain', gain_db=-6).to_dict()
    declaration = {'schema': 'carr-book-batch-request/1', 'title': 'Effects explicitly selected',
        'books': [{'edition': e.imported['edition'], 'output_parent': str(e.output),
            'voice': {'language': 'en', 'voice_id': 'synthetic-tone', 'effects': [effect]}}]}
    selected, plans, outputs = capture_request(e.service, declaration, topic=e.topic)
    captured = plans[0].fingerprint
    assert plans[0].schema == 2 and plans[0].project(1).schema == 3
    effect['parameters']['gain_db'] = 12
    assert plans[0].fingerprint == captured
    assert plans[0].role.effects[0].parameters['gain_db'] == -6
    assert not e.voices.received and not e.production.jobs() and len(selected.items) == 1


@pytest.mark.parametrize('effects', [
    'arbitrary-filter-string',
    [{'kind': 'not-an-effect', 'parameters': {}, 'enabled': True}],
    [Effect('tempo', factor=1.5).to_dict()],
    [Effect('width_envelope', point_count=2, time_1=0, time_2=1, value_1=1, value_2=1).to_dict()],
])
def test_invalid_request_effects_keep_jobs_and_sources_untouched(book, effects):
    e = book
    declaration = {'schema': 'carr-book-batch-request/1', 'title': 'Unreviewed effects',
        'books': [{'edition': e.imported['edition'], 'output_parent': str(e.output),
            'voice': {'language': 'en', 'voice_id': 'synthetic-tone', 'effects': effects}}]}
    with pytest.raises(ValueError):
        capture_request(e.service, declaration, topic=e.topic)
    assert not e.production.jobs() and not e.voices.received
    assert e.app.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode('utf-8')


def test_real_cli_review_publish_and_no_clobber_need_no_additional_speech(rendered):
    r = rendered
    calls = tuple(r.e.voices.received)
    result = invoke(r, 'duration-review', *options(r))
    assert result.returncode == 0, result.stderr
    reviewed = json.loads(result.stdout)
    assert not r.output.exists() and reviewed['content']['maximum_ms'] == 70
    result = invoke(r, 'duration-publish', *options(r), '--approval', reviewed['approval_sha256'])
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    before = (sha256(r.output.read_bytes()).hexdigest(), r.output.stat().st_mtime_ns)
    assert receipt['status'] == 'complete' and receipt['format'] == 'pcm-parts'
    again = invoke(r, 'duration-publish', *options(r), '--approval', reviewed['approval_sha256'])
    assert again.returncode == 1 and not again.stdout
    assert before == (sha256(r.output.read_bytes()).hexdigest(), r.output.stat().st_mtime_ns)
    assert r.publisher.verify(receipt['id']) == receipt
    assert tuple(r.e.voices.received) == calls


@pytest.mark.parametrize('change', ['approval', 'source'])
def test_real_cli_rejects_unreviewed_approval_or_changed_source(rendered, change):
    r = rendered
    result = invoke(r, 'duration-review', *options(r))
    assert result.returncode == 0, result.stderr
    approved = json.loads(result.stdout)['approval_sha256']
    if change == 'approval':
        approved = '0' * 64
    else:
        r.e.app.update_metadata(r.e.workspace, r.e.imported['edition'], title='Changed source title')
    result = invoke(r, 'duration-publish', *options(r), '--approval', approved)
    assert result.returncode == 1 and not result.stdout and not r.output.exists()
    assert not r.publisher.publications()


def test_actual_child_death_after_publish_keeps_prepared_receipt_and_never_repeats(rendered):
    r = rendered
    reviewed = invoke(r, 'duration-review', *options(r))
    assert reviewed.returncode == 0, reviewed.stderr
    approved = json.loads(reviewed.stdout)['approval_sha256']
    crashed = invoke(r, 'duration-publish', *options(r), '--approval', approved, crash=True)
    assert crashed.returncode == 73 and r.output.exists()
    receipt, = r.publisher.publications()
    assert receipt['status'] == 'prepared'
    before = (r.output.read_bytes(), r.output.stat().st_mtime_ns)
    refused = invoke(r, 'duration-publish', *options(r), '--approval', approved)
    assert refused.returncode == 1 and not refused.stdout
    assert (r.output.read_bytes(), r.output.stat().st_mtime_ns) == before
    complete = r.publisher.verify(receipt['id'])
    assert complete['status'] == 'complete'
    assert (r.output.read_bytes(), r.output.stat().st_mtime_ns) == before
