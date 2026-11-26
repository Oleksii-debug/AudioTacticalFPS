"""Keep the isolated wx handler probes inside the normal CI test collection."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('name',['probe_catalog_panel.py','probe_evidence_panel.py',
                                'probe_topic_panel.py','probe_workspace_shell.py'])
def test_headless_panel_protocol(name):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ,PYTHONPATH=os.pathsep.join((str(root / 'src'),str(root))))
    result = subprocess.run([sys.executable,str(Path(__file__).with_name(name)),'-v'],
                            cwd=root,env=env,text=True,capture_output=True,timeout=30)
    assert result.returncode == 0,result.stdout + result.stderr
    assert 'Ran ' in result.stderr and 'OK' in result.stderr
