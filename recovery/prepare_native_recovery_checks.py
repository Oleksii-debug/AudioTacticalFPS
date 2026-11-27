"""Prepare exact archived syntax inputs or the finite CI runner regression.

Archived workflow bodies are data for the PowerShell parser, never commands to
execute. The CI-runner case copies only its two reviewed source/test files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


PREFIX = Path('recovery/canonical-composition-20261127')
WORKFLOWS = (
    '.github/workflows/carr-windows.yml',
    '.github/workflows/carr-v3.yml',
    '.github/workflows/carr-native-launcher.yml',
)
CI_FILES = {
    'carr-library/scripts/ci_check.py': '67e42f01e73ef7bfcd3686ff34f646aab26c74f3',
    'carr-library/tests/packaging/test_ci_check.py': 'bc05942a32e086cc86238802bffc9023cc4123cd',
}
EXPRESSION = re.compile(r'\$\{\{[\s\S]*?\}\}')


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments], text=True).strip()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()


def inside(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('An archived source path is not relative.')
    candidate = root / relative
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('An archived source path leaves its checkout.')
    return candidate


def candidates(root):
    path = root / PREFIX / 'manifest.json'
    raw = path.read_bytes()
    manifest = json.loads(raw)
    entries = manifest['files']
    selected = {entry['path']: entry for entry in entries}
    if len(entries) != len(selected):
        raise ValueError('The recovery manifest contains duplicate candidate paths.')
    return selected, digest(raw)


def checked_source(root, selected, relative):
    entry = selected[relative]
    raw = inside(root, entry['saved_path']).read_bytes()
    if blob(raw) != entry['new_blob']:
        raise ValueError('Archived candidate bytes changed: ' + relative)
    return raw, {'path': relative, 'saved_path': entry['saved_path'],
                 'git_blob': entry['new_blob'], 'sha256': digest(raw), 'bytes': len(raw)}


def identity(root, manifest_sha256):
    return {'validation_commit': git(root, 'rev-parse', 'HEAD'),
            'validation_tree': git(root, 'rev-parse', 'HEAD^{tree}'),
            'recovery_manifest_sha256': manifest_sha256}


def neutralize_expressions(body):
    replacements = []

    def substitute(match):
        replacements.append({'offset': match.start(),
            'line': body.count('\n', 0, match.start()) + 1,
            'expression_sha256': digest(match.group().encode('utf-8'))})
        return 'RECOVERY_EXPRESSION'

    parsed = EXPRESSION.sub(substitute, body)
    if '${{' in parsed:
        raise ValueError('An unterminated GitHub expression cannot be neutralized.')
    return parsed, replacements


def prepare_powershell(root, evidence):
    import yaml

    selected, manifest_sha256 = candidates(root)
    scripts, skipped, workflows = [], [], []
    destination = evidence / 'powershell-inputs'
    destination.mkdir(parents=True, exist_ok=False)
    for relative in WORKFLOWS:
        raw, workflow_identity = checked_source(root, selected, relative)
        document = yaml.safe_load(raw)
        if not isinstance(document, dict) or not isinstance(document.get('jobs'), dict):
            raise ValueError('A required workflow has no job mapping: ' + relative)
        workflow_shell = document.get('defaults', {}).get('run', {}).get('shell')
        count = 0
        for job_id, job in document['jobs'].items():
            runner = job.get('runs-on', '')
            windows = 'windows' in str(runner).lower()
            default_shell = job.get('defaults', {}).get('run', {}).get('shell', workflow_shell)
            for step_number, step in enumerate(job.get('steps', ()), 1):
                if 'run' not in step:
                    continue
                body = step['run']
                if not isinstance(body, str):
                    raise ValueError('A workflow run body is not text.')
                shell = step.get('shell', default_shell) or ('pwsh' if windows else 'bash')
                if '${{' in shell:
                    raise ValueError('Resolve a dynamic shell before assigning a syntax parser.')
                record = {'workflow': relative, 'workflow_blob': workflow_identity['git_blob'],
                          'workflow_sha256': workflow_identity['sha256'], 'job': job_id,
                          'step': step_number, 'name': step.get('name', ''), 'shell': shell,
                          'run_sha256': digest(body.encode('utf-8'))}
                if shell.split()[0].lower() not in ('pwsh', 'powershell'):
                    skipped.append({**record, 'status': 'NOT_POWERSHELL',
                                    'reason': 'The declared runner/shell uses a different language.'})
                    continue
                parsed, replacements = neutralize_expressions(body)
                content = parsed.encode('utf-8')
                name = f'{len(scripts) + 1:04d}.ps1'
                (destination / name).write_bytes(content)
                scripts.append({**record, 'file': 'powershell-inputs/' + name,
                                'parse_sha256': digest(content), 'expression_replacements': replacements})
                count += 1
        if not count:
            raise ValueError('No PowerShell run blocks were selected from ' + relative)
        workflows.append({**workflow_identity, 'powershell_blocks': count})
    result = {**identity(root, manifest_sha256), 'status': 'PREPARED_NOT_PARSED',
              'scope': 'Syntax only; archived run blocks are never executed.',
              'workflows': workflows, 'scripts': scripts, 'other_shell_blocks': skipped,
              'installed': 'NOT_RUN', 'application_native': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    (evidence / 'powershell-inputs.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'powershell_blocks': len(scripts),
                      'other_shell_blocks': len(skipped)}))


def prepare_ci_check(root, evidence, stage):
    selected, manifest_sha256 = candidates(root)
    rows = []
    if stage.exists():
        raise ValueError('The isolated CI runner source directory must be new.')
    prepared = []
    for relative, expected in CI_FILES.items():
        raw, record = checked_source(root, selected, relative)
        if record['git_blob'] != expected:
            raise ValueError('The selected CI runner candidate changed; re-review its exact bytes.')
        compile(raw, relative, 'exec', dont_inherit=True)
        prepared.append((relative, raw, record))
    for relative, raw, record in prepared:
        target = inside(stage, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if target.read_bytes() != raw:
            raise ValueError('The isolated CI runner copy differs from its candidate.')
        rows.append(record)
    result = {**identity(root, manifest_sha256), 'status': 'PREPARED_NOT_TESTED',
              'scope': 'Only selected CI runner and its complete finite regression file.',
              'files': rows, 'product_sources': 'NOT_IMPORTED_OR_COPIED',
              'installed': 'NOT_RUN', 'application_native': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    (evidence / 'ci-check-source.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'files': len(rows)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('powershell', 'ci-check'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--stage', type=Path)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    if args.mode == 'powershell':
        if args.stage is not None:
            parser.error('--stage is only used for ci-check.')
        prepare_powershell(args.root, args.evidence)
    else:
        if args.stage is None:
            parser.error('--stage is required for ci-check.')
        prepare_ci_check(args.root, args.evidence, args.stage)


if __name__ == '__main__':
    main()
