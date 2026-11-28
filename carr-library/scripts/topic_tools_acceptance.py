"""Use the existing exact Windows assistant qualification for scoped topic tools."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / 'scripts' / 'topic_tools_oracle.json'
declared = json.loads(ORACLE_PATH.read_text(encoding='utf-8'))
if declared['source'] != declared['installed']:
    raise ValueError('Topic source and installed populations must be identical.')
tests = {name.replace('.', '/') + '.py': functions
         for name, functions in declared['source'].items()}
if sum(sum(functions.values()) for functions in tests.values()) != declared['total_cases']:
    raise ValueError('Topic function Counters do not equal their declared population.')

spec = importlib.util.spec_from_file_location(
    '_topic_assistant_qualification', ROOT / 'scripts' / 'workflow_template_acceptance.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
# Reuse the same strict pytest policy, complete checkout/wheel inventory,
# real runtime proof, native origins and actual firewall cleanup verifier.
# Only the committed topic population and exact receipt names differ.
adapter.ORACLE_PATH = adapter.base.ORACLE_PATH = ORACLE_PATH
adapter.ORACLE = adapter.base.ORACLE = {
    **declared, 'tests': tests, 'helpers': ['tests/__init__.py'],
    'retained_cases': 0, 'new_cases': declared['total_cases'],
}
adapter.base.SUPPORT = tuple(adapter.ORACLE['helpers'])
adapter.DISPATCH_PATH = Path(__file__)
adapter.GATE_NAME = 'topic-tools-gate.json'
adapter.NATIVE_WORK = 'native-topic-fixtures'
adapter.NATIVE_SECONDS = declared['native']['timeout_seconds']
adapter.CASE_FIELD = 'new_topic_cases'

if __name__ == '__main__':
    raise SystemExit(adapter.main())
