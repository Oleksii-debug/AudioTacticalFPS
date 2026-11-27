"""Read fixed observed CI artifacts without importing or executing their contents."""
from collections import Counter
import hashlib
import io
import json
import os
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

REPOSITORY = 'Oleksii-debug/AudioTacticalFPS'
ARTIFACTS = [
  {
    "run": 46053395802,
    "commit": "480f8ecee5e7eee108245391fb617cf8eaa7bfa5",
    "id": 15226302196,
    "bytes": 1176055,
    "sha256": "3a3661a91f7bb49d3869e6f6f6f4018af947558266e61f6cc6a37cb200a54ee7",
    "kind": "standard-update"
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
    "tested-commit.txt",
    "tested-tree.txt",
    "standard-native-diagnostic.json",
    "standard-update/inputs.json",
    "standard-update/candidate-application-wheel.json",
    "standard-update/previous-build.json",
    "standard-update/previous-build-verification.log",
    "standard-update/preflight.log",
    "standard-update/native-probe.log",
    "standard-update/native.json",
    "standard-update/proof.json",
    "standard-update/library.json",
    "standard-update/verdict.log",
    "standard-update/native-restart-crash-after-activation.json",
    "standard-update/native-restart-recover.json",
    "standard-installer/inputs.json",
    "standard-installer/disk-budget.json",
    "standard-installer/good-build.json",
    "standard-installer/negative-build.json",
    "standard-installer/native.json",
    "standard-installer/proof.json",
    "standard-installer/native-probe.log"
}
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
        if any(len(name) > 4096 for name in names):
            raise ValueError('Artifact member name exceeds the diagnostic bound.')
        inventory = json.dumps({'total_members': len(names), 'names': names,
            'sizes': {member.filename: member.file_size for member in members}}, ensure_ascii=True)
        if len(inventory.encode('utf-8')) > 1024 * 1024:
            raise ValueError('Artifact inventory exceeds the diagnostic output bound.')
        selected = sorted((name for name in names if name in wanted),
            key=lambda name: (not name.startswith('standard-update/'), name))
        print('INVENTORY', inventory, flush=True)
        print('SELECTION', json.dumps({'selected': selected,
              'absent_fixed_paths': sorted(wanted.difference(names))}), flush=True)
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
                print(name, json.dumps(brief(json.loads(content)), sort_keys=True, ensure_ascii=False)[-80000:], flush=True)
            else:
                print(name, content.decode('utf-8', errors='replace')[-48000:], flush=True)

print('DIAGNOSTIC_COMPLETE', json.dumps({'artifact_id': ARTIFACTS[0]['id'],
    'scope': 'fixed existing text and JSON only; archived code was not executed',
    'product_qualification': 'NOT_CLAIMED'}), flush=True)
