"""Actual local children must not inherit implicit diagnostic file outputs."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.audio.engines import _run
from carr_library.audio.model_manifest import offline_environment


CHILD = '''
import json, os
print(json.dumps({"logs": {key: value for key, value in os.environ.items()
    if key.upper() in {"FFREPORT", "SSLKEYLOGFILE"}},
    "sentinel": os.environ.get("CARR_ENV_TEST_SENTINEL")}))
'''


@pytest.mark.parametrize('boundary', ['worker', 'run_inherited', 'run_explicit'])
def test_local_child_cannot_create_logs_from_inherited_environment(boundary, monkeypatch):
    monkeypatch.setenv('FFREPORT', 'file=unexpected-report.log:level=32')
    monkeypatch.setenv('SSLKEYLOGFILE', 'unexpected-tls-keys.log')
    monkeypatch.setenv('CARR_ENV_TEST_SENTINEL', 'preserved')
    command = [str(Path(sys.executable).resolve()), '-I', '-c', CHILD]
    if boundary == 'worker':
        output = subprocess.run(command, env=offline_environment(), shell=False,
            capture_output=True, check=True, timeout=5).stdout
    else:
        environment = dict(os.environ) if boundary == 'run_explicit' else None
        if environment is not None:
            # Windows environment names are case insensitive; explicit mappings
            # should be sanitized independently of the parent platform.
            environment['ffreport'] = 'file=another-report.log:level=32'
            environment['SslKeyLogFile'] = 'another-tls-keys.log'
        output = _run(command, env=environment, timeout=5)
        if environment is not None:
            assert 'FFREPORT' in environment and 'SslKeyLogFile' in environment
    assert json.loads(output) == {'logs': {}, 'sentinel': 'preserved'}
    assert os.environ['FFREPORT'] == 'file=unexpected-report.log:level=32'
    assert os.environ['SSLKEYLOGFILE'] == 'unexpected-tls-keys.log'
