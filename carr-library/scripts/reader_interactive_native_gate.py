"""Reuse the existing strict native reader/assistant checkers in the full bundle."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import traceback


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    output = args.results.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'status': 'FAIL', 'nvda': 'NOT_RUN',
              'physical_audio': 'NOT_RUN', 'model_quality': 'NOT_CLAIMED'}
    try:
        if sys.platform != 'win32' or not sys.flags.isolated:
            raise ValueError('Actual isolated installed Windows execution is required.')
        names = (
            'scripts/qualify_assistant_tools.py',
            'scripts/qualify_reader_appearance.py',
            'scripts/windows_reader_appearance_smoke.py',
            'scripts/windows_assistant_interactive_smoke.py',
            'scripts/reader_appearance_acceptance_oracle.json',
            'scripts/assistant_interactive_acceptance_oracle.json',
            'scripts/reader_interactive_native_gate.py',
        )
        def identities():
            return {name: sha256((source / name).read_bytes()).hexdigest() for name in names}
        before = identities()
        common = runpy.run_path(str(source / names[0]), run_name='native_process_helpers')
        readers = runpy.run_path(str(source / names[1]), run_name='native_receipt_helpers')
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source, text=True).strip()
        if commit != os.environ['GITHUB_SHA']:
            raise ValueError('The installed native gate is bound to another event source.')
        report.update(source_commit=commit, source_tree=tree, source_files=before, native={})
        with TemporaryDirectory(prefix='carr-full-reader-native-') as temporary:
            work = Path(temporary)
            for label, probe, oracle, appearance in (
                ('appearance', names[2], names[4], True),
                ('assistant', names[3], names[5], False),
            ):
                config = json.loads((source / oracle).read_text(encoding='utf-8'))['native']
                command = [sys.executable, '-I', '-B', '-X', 'utf8', str(source / probe), '--native']
                if not appearance:
                    command.append(str(work / 'assistant'))
                common['run'](command, work, dict(os.environ), output, label, timeout=180)
                receipt = readers['terminal'](output / (label + '.stdout.txt'))
                count = readers['check_native'](receipt, config, source, appearance=appearance)
                report['native'][label] = {'checks': count, 'receipt': receipt,
                    'stdout_sha256': sha256((output / (label + '.stdout.txt')).read_bytes()).hexdigest()}
            if identities() != before:
                raise ValueError('A source-bound native probe, checker or oracle changed.')
        report['status'] = 'PASS'
    except Exception as error:
        traceback.print_exc()
        report['reason'] = type(error).__name__ + ': ' + str(error)
    (output / 'native-proof.json').write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
