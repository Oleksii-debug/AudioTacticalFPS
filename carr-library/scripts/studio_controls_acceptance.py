"""Reuse the strict installed/source runner for current native Studio role and output controls."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / 'scripts' / 'studio_controls_oracle.json'
declared = json.loads(ORACLE_PATH.read_text(encoding='utf-8'))
if sum(sum(functions.values()) for functions in declared['tests'].values()) != declared['total_cases']:
    raise ValueError('Studio-control function Counters differ from their declared population.')
spec = importlib.util.spec_from_file_location(
    '_studio_controls_qualification', ROOT / 'scripts' / 'workflow_template_acceptance.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
adapter.ORACLE_PATH = adapter.base.ORACLE_PATH = ORACLE_PATH
adapter.ORACLE = adapter.base.ORACLE = declared
adapter.base.SUPPORT = tuple(declared['helpers'])
adapter.DISPATCH_PATH = Path(__file__)
adapter.GATE_NAME = 'studio-controls-gate.json'
adapter.NATIVE_WORK = 'native-studio-controls-fixtures'
adapter.NATIVE_SECONDS = declared['native']['timeout_seconds']
adapter.CASE_FIELD = 'new_studio_control_cases'

if __name__ == '__main__':
    raise SystemExit(adapter.main())
