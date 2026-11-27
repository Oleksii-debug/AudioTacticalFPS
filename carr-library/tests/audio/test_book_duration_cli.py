"""Headless completed-PCM publication using real fixture renders and fresh processes."""
import json
from pathlib import Path
import subprocess

from carr_library.audio.measured_audio import inspect
from tests.audio.test_book_plans import library
from tests.audio.test_measured_book_audio import completed
from tests.audio.test_book_cli import child_arguments


def invoke(library, tmp_path, *arguments):
    result = subprocess.run(child_arguments(library, *arguments),
        cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout)


def test_isolated_review_publish_verify_and_offline_batch_reexport(completed, library, tmp_path):
    books, plan, _, _ = completed
    output = tmp_path / 'cli-measured.zip'
    options = ('--part', 1, '--maximum-ms', 70, '--output', output)
    preview = invoke(library, tmp_path, 'duration-review', plan.id, *options)
    assert not output.exists() and len(preview['content']['parts']) > 1
    receipt = invoke(library, tmp_path, 'duration-publish', plan.id, *options,
                     '--reviewed-sha256', preview['approval_sha256'])
    assert receipt['status'] == 'complete' and output.is_file()
    assert inspect(output)['source']['book']['plan_sha256'] == plan.fingerprint
    records = invoke(library, tmp_path, 'publications', plan.id, '--part', 1)
    assert records['verified_outputs'] is False and records['receipts'][0]['id'] == receipt['id']
    verified = invoke(library, tmp_path, 'duration-verify', plan.id, '--part', 1,
                      '--receipt', receipt['id'], '--output', output)
    assert verified == receipt
    # The current saved Book remains usable from another interpreter with no
    # fixture engine object. Its exact accepted render, not an engine cache,
    # is the sole authority for the independent completed-book copy.
    batch = invoke(library, tmp_path, 'batch', '--plan', plan.id)
    external = tmp_path / 'cli-completed-export'
    external.mkdir()
    review = invoke(library, tmp_path, 'review', batch['batch'], '--output-parent', external)
    assert review['prepared']
    result = invoke(library, tmp_path, 'run', batch['batch'], '--output-parent', external,
                    '--reviewed-sha256', review['approval_sha256'])
    assert result['status'] == 'complete'
    assert result['books'][0]['items'][0]['retained_completion'] is True
    assert Path(result['books'][0]['items'][0]['destination']).parent == external
