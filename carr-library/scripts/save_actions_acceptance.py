"""Reuse the strict installed/source runner for current Save and Edit menu routes."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / 'scripts' / 'save_actions_oracle.json'
declared = json.loads(ORACLE_PATH.read_text(encoding='utf-8'))
if sum(sum(functions.values()) for functions in declared['tests'].values()) != declared['total_cases']:
    raise ValueError('Save-menu function Counters differ from their declared population.')
spec = importlib.util.spec_from_file_location(
    '_save_menu_qualification', ROOT / 'scripts' / 'workflow_template_acceptance.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
adapter.ORACLE_PATH = adapter.base.ORACLE_PATH = ORACLE_PATH
adapter.ORACLE = adapter.base.ORACLE = declared
adapter.base.SUPPORT = tuple(declared['helpers'])
adapter.DISPATCH_PATH = Path(__file__)
adapter.GATE_NAME = 'save-actions-gate.json'
adapter.NATIVE_WORK = 'native-save-actions-fixtures'
adapter.NATIVE_SECONDS = declared['native']['timeout_seconds']
adapter.CASE_FIELD = 'new_save_route_cases'

if __name__ == '__main__':
    raise SystemExit(adapter.main())
