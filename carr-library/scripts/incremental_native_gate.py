"""Strict installed-native snapshot evidence; never infer success from zero exit alone."""
from collections import Counter
from contextlib import redirect_stdout
from hashlib import sha256
import importlib
import io
import json
from pathlib import Path
import re
import runpy
import sys
import traceback


def main():
    root = Path.cwd().resolve()
    destination = root / 'incremental-results/native-proof.json'
    oracle = json.loads((root / 'scripts/incremental_acceptance_oracle.json').read_text(encoding='utf-8'))
    report = {'status': 'NOT_RUN', 'source_commit': None, 'source_tree': None,
              'actual_windows': False, 'installed_package': False,
              'manual_keyboard': 'NOT_RUN', 'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
              'full_platform': 'NOT_CLAIMED'}
    origins, raw = {}, io.StringIO()

    class Tee:
        def write(self, value):
            raw.write(value)
            sys.__stdout__.write(value)
            return len(value)
        def flush(self):
            sys.__stdout__.flush()

    try:
        assert sys.platform == 'win32' and sys.flags.isolated
        report['source_commit'] = (root / 'incremental-results/tested-commit.txt').read_text(encoding='utf-8-sig').strip()
        report['source_tree'] = (root / 'incremental-results/tested-tree.txt').read_text(encoding='utf-8-sig').strip()
        assert all(re.fullmatch('[0-9a-f]{40}', report[key]) for key in ('source_commit', 'source_tree'))
        for name in oracle['origins']:
            path = Path(importlib.import_module(name).__file__).resolve()
            expected = root / 'src' / Path(*name.split('.')).with_suffix('.py')
            assert path.is_relative_to(Path(sys.prefix).resolve()) and 'site-packages' in path.parts, name
            digest = sha256(path.read_bytes()).hexdigest()
            assert digest == sha256(expected.read_bytes()).hexdigest(), name
            origins[name] = {'path': str(path), 'sha256': digest}
        probe = root / 'scripts/windows_incremental_backup_smoke.py'
        report['probe_sha256'] = sha256(probe.read_bytes()).hexdigest()
        namespace = runpy.run_path(str(probe), run_name='carr_incremental_native_probe')
        with redirect_stdout(Tee()):
            code = namespace['main']()
        records = []
        for line in raw.getvalue().splitlines():
            if line.startswith('{'):
                record = json.loads(line)
                if record.get('status') in ('PASS', 'FAIL', 'NOT_RUN'):
                    records.append(record)
        assert code == 0 and len(records) == 1, (code, records)
        observed = records[0]
        native = oracle['native']
        for key in ('status', 'native_controls', 'real_keyboard_tab', 'actual_background_roundtrip',
                    'manual_keyboard', 'native_file_pickers', 'nvda'):
            assert observed[key] == native[key], key
        assert Counter(observed['checks']) == Counter(native['checks'])
        assert len(observed['checks']) == native['expected_checks'] == 14
        assert observed['failures'] == []
        assert type(observed['new_bytes']) is int and observed['new_bytes'] >= 0
        assert type(observed['reused_bytes']) is int and observed['reused_bytes'] > 0
        assert all(re.fullmatch('[0-9a-f]{64}', observed[key])
                   for key in ('snapshot_sha256', 'restored_snapshot_sha256'))
        for record in origins.values():
            assert sha256(Path(record['path']).read_bytes()).hexdigest() == record['sha256']
        report.update(observed, actual_windows=True, installed_package=True, isolated=True,
                      module_origins=origins)
    except BaseException:
        report.update(status='FAIL', error=traceback.format_exc(), module_origins=origins)
    finally:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
                               encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
