"""Read fixed observed CI artifacts without importing or executing their contents."""
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

REPOSITORY = 'Oleksii-debug/AudioTacticalFPS'
ARTIFACTS = [
  {
    "run": 46058236666,
    "commit": "e5453076c9593219e2d851d8f96a7fa62680d219",
    "id": 15227926080,
    "sha256": "e172c5edf09305ef948c6e5067457341f65f7b9166c65ee2e8ef03457cccd964",
    "bytes": 3529405,
    "kind": "chm-ordinary-source-installed236-native4-and70-member-census"
  }
]

class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        if not url.startswith('https://'):
            raise ValueError('Only HTTPS artifact redirects are allowed.')
        result = super().redirect_request(request, fp, code, message, headers, url)
        result.remove_header('Authorization')
        return result

def read(url, maximum):
    request = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.build_opener(Redirect).open(request, timeout=90) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Artifact response exceeds its explicit byte bound.')
    return raw

def brief(value):
    if isinstance(value, dict):
        if len(value) > 120:
            return {'entries': len(value), 'canonical_sha256': hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        return {key: brief(item) for key, item in value.items()}
    if isinstance(value, list) and len(value) > 160:
        return {'entries': len(value)}
    if isinstance(value, list):
        return [brief(item) for item in value]
    return value

wanted = {
    'converter-gate.json', 'legacy-converter-gate.json', 'before-execution.json',
    'publisher-identities.json', 'source-provenance.json',
    'source-origins.json', 'installed-origins.json', 'translation-scope-gate.json',
    'native-selected-advanced.txt', 'native-selected-advanced.json',
    'native-document.txt', 'native-document.json', 'native-chapter.txt',
    'native-chapter.json', 'native-batch.txt', 'native-batch.json',
    'local-translation-gate.json', 'native-local-selected.json', 'native-local-selected.txt',
    'native-local-document.json', 'native-local-document.txt',
    'native-local-chapter.json', 'native-local-chapter.txt',
    'native-local-batch.json', 'native-local-batch.txt',
    'source-junit.xml', 'installed-junit.xml', 'source-pytest.txt', 'installed-pytest.txt'}
for pin in ARTIFACTS:
    base = 'https://api.github.com/repos/' + REPOSITORY + '/actions/artifacts/' + str(pin['id'])
    metadata = json.loads(read(base, 65536))
    if (metadata['id'] != pin['id'] or metadata['workflow_run']['id'] != pin['run']
            or metadata['workflow_run']['head_sha'] != pin['commit']
            or metadata['digest'] != 'sha256:' + pin['sha256'] or metadata['expired']
            or metadata['size_in_bytes'] != pin['bytes']):
        raise ValueError('Existing artifact provenance differs.')
    raw = read(base + '/zip', 8 * 1024 * 1024)
    if len(raw) != pin['bytes'] or hashlib.sha256(raw).hexdigest() != pin['sha256']:
        raise ValueError('Existing artifact archive failed its recorded digest.')
    print('ARTIFACT', json.dumps({**pin, 'bytes': len(raw), 'name': metadata['name']}), flush=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if (len(names) != len(set(names)) or len(names) > 10000
                or sum(member.file_size for member in members) > 512 * 1024 * 1024):
            raise ValueError('Ambiguous or oversized artifact inventory.')
        selected = [name for name in names if PurePosixPath(name).name in wanted]
        print('INVENTORY', json.dumps({'total_members': len(names), 'selected': selected}), flush=True)
        for name in selected:
            entry = archive.getinfo(name)
            if entry.file_size > 4 * 1024 * 1024:
                raise ValueError('Diagnostic member exceeds its read bound.')
            content = archive.read(name)
            print('MEMBER', json.dumps({'name': name, 'bytes': len(content),
                  'sha256': hashlib.sha256(content).hexdigest()}), flush=True)
            if name.endswith('-junit.xml'):
                root = ET.fromstring(content)
                cases = root.findall('.//testcase')
                functions = Counter(case.get('classname', '') + '::' +
                    case.get('name', '').split('[', 1)[0] for case in cases)
                print('JUNIT', json.dumps({'artifact': pin['id'], 'file': name, 'cases': len(cases),
                    'failures': len(root.findall('.//failure')), 'errors': len(root.findall('.//error')),
                    'skips': len(root.findall('.//skipped')), 'function_counter': dict(sorted(functions.items()))}),
                    flush=True)
                for case in cases:
                    for failure in list(case.findall('failure')) + list(case.findall('error')) + list(case.findall('skipped')):
                        print('FAILED_CASE', case.get('classname'), case.get('name'), flush=True)
                        print((failure.text or '')[-12000:], flush=True)
            elif name.endswith('.json'):
                value = json.loads(content)
                if isinstance(value, dict) and isinstance(value.get('runtime_files'), dict):
                    files = value['runtime_files']
                    code = {key: row for key, row in files.items()
                            if PurePosixPath(key).suffix.casefold() in ('.exe', '.dll', '.pyd', '.zip', '.so')
                            or key == 'calibre-debug'}
                    print('RUNTIME_CODE_INVENTORY', json.dumps({
                        'artifact': pin['id'], 'member': name, 'files': len(files), 'code_handles': len(code),
                        'code_bytes': sum(row['bytes'] for row in code.values()),
                        'suffix_counts': dict(Counter(PurePosixPath(key).suffix.casefold() for key in code)),
                        'canonical_sha256': hashlib.sha256(json.dumps(code, sort_keys=True,
                            separators=(',', ':')).encode()).hexdigest()}), flush=True)
                rendered = json.dumps(brief(value), sort_keys=True, ensure_ascii=False)
                if len(rendered) > 256 * 1024:
                    raise ValueError('Bounded diagnostic JSON exceeds its printing budget.')
                print(name, rendered, flush=True)
            else:
                print(name, content.decode('utf-8', errors='replace')[-48000:], flush=True)
