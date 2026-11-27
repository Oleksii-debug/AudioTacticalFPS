"""Finite syntax and declared-entrypoint preflight; no runtime qualification."""
import ast
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    def git(*values):
        return subprocess.run(['git', *values], cwd=root, capture_output=True,
                              check=True, timeout=30).stdout
    actual = git('rev-parse', 'HEAD').decode().strip()
    if actual != args.source_commit:
        raise ValueError('Syntax preflight is not on the exact event commit.')
    tree = git('rev-parse', 'HEAD^{tree}').decode().strip()
    inventory = git('ls-files', '-z', '--', 'carr-library').decode('utf-8').split('\0')
    selected = sorted(path for path in inventory if path.endswith('.py'))
    if not 1 <= len(selected) <= 10000:
        raise ValueError('Invalid finite tracked Python inventory.')
    passed, failures = {}, {}
    for relative in selected:
        path = root / relative
        raw = path.read_bytes()
        try:
            if len(raw) > 4 * 1024**2:
                raise ValueError('A Python source exceeds the syntax preflight byte budget.')
            ast.parse(raw, filename=relative, mode='exec', feature_version=(3, 11))
            passed[relative] = sha256(raw).hexdigest()
        except (SyntaxError, ValueError) as error:
            failures[relative] = str(error)
    metadata = tomllib.loads((root / 'carr-library/pyproject.toml').read_text(encoding='utf-8'))
    entrypoints = {}
    for group in ('scripts', 'gui-scripts'):
        for name, target in metadata['project'].get(group, {}).items():
            module, separator, function = target.partition(':')
            if not separator or not function.isidentifier():
                raise ValueError('Entry point target is not an explicit function.')
            relative = 'carr-library/src/' + module.replace('.', '/') + '.py'
            if relative not in passed:
                raise ValueError('Entry point module is missing from the verified source inventory.')
            parsed = ast.parse((root / relative).read_bytes())
            if function not in {node.name for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}:
                raise ValueError('Entry point function is missing: ' + name)
            entrypoints[group + ':' + name] = target
    standard = (root / 'carr-library/scripts/ci_standard_update.ps1').read_text(encoding='utf-8')
    for name in ('preflight', 'sourceProof', 'checkPrevious', 'verify'):
        start = standard.index('$' + name + " = @'\n") + len('$' + name + " = @'\n")
        end = standard.index("\n'@", start)
        ast.parse(standard[start:end], filename='ci_standard_update.ps1:' + name, feature_version=(3, 11))
    git('diff', '--exit-code', 'HEAD', '--')
    report = dict(schema=1, status='FAIL' if failures else 'PASS', source_commit=actual, source_tree=tree,
        python=sys.version.split()[0], syntax_files=len(passed), files=passed, failures=failures,
        entrypoints=entrypoints, runtime_tests='NOT_RUN', installed_windows='NOT_RUN', nvda='NOT_RUN')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
                                                 'syntax_files', 'failures', 'entrypoints')}))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
