"""One-off read-only reconciliation of two explicit public commits."""
import json
from pathlib import Path
import subprocess

OURS = '9f0ea29e5bcd2fc1f34c1ef1607d08c6bce4fd6a'
THEIRS = 'e69ba2403515f21509686d66f4bc8b3673756d81'

def git(*arguments, **kwargs):
    return subprocess.run(['git', *arguments], capture_output=True, text=True,
                          encoding='utf-8', errors='strict', check=True, **kwargs).stdout

result = subprocess.run(['git', 'merge-tree', '--write-tree', '--messages', '--name-only',
                         OURS, THEIRS], capture_output=True, text=True, encoding='utf-8')
if result.returncode not in (0, 1):
    raise RuntimeError(result.stderr)
tree = result.stdout.splitlines()[0]
if len(tree) != 40 or any(char not in '0123456789abcdef' for char in tree):
    raise ValueError('No reviewed merge tree was produced')
paths = git('diff', '--name-only', '-z', OURS, tree).split('\0')
report = {'ours': OURS, 'theirs': THEIRS, 'tree': tree, 'merge_status': result.returncode,
          'details': result.stdout, 'files': []}
for path in filter(None, paths):
    if not path.startswith(('.github/workflows/', 'carr-library/')):
        raise ValueError('Unexpected merge path: ' + path)
    content = git('show', tree + ':' + path)
    row = {'path': path, 'content': content,
           'conflicted': any(line.startswith(('<<<<<<< ', '=======', '>>>>>>> '))
                             for line in content.splitlines())}
    report['files'].append(row)
    print('CARR_MERGE_FILE ' + json.dumps(row, ensure_ascii=True, separators=(',', ':')), flush=True)
Path('ci-reader-results').mkdir(exist_ok=True)
Path('ci-reader-results/merge-review.json').write_text(
    json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
print('CARR_MERGE_SUMMARY ' + json.dumps({k: v for k, v in report.items() if k != 'files'}), flush=True)
