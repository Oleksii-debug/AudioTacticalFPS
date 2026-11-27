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
    "run": 46066498865,
    "commit": "910b1498d801cabcf9ba2751b93ecbe01d825ea8",
    "id": 15232821513,
    "bytes": 1777762,
    "sha256": "e1871dbd86a345f902c56e1a2ca4f476cdd5f3f6799b6d81c3e786c5b10e3d64",
    "kind": "standard-managed-health-repaired-installed-library-failure"
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

wanted = {'inputs.json',
    'disk-budget.json',
    'original-build.json',
    'previous-build.json',
    'original-manifest.json',
    'previous-manifest.json',
    'candidate-application-wheel.json',
    'original-candidate-application-wheel.json',
    'original-maintenance.json',
    'native.json',
    'proof.json',
    'library.json',
    'native-restart-crash-after-activation.json',
    'native-restart-recover.json',
    'original-install.log',
    'maintenance-install.log',
    'native-probe.log',
    'verdict.log'}
root_wanted = {
    'tested-commit.txt', 'tested-tree.txt', 'source-wheel-proof.json',
    'tested-application-wheel.json', 'standard-native-diagnostic.json',
    'standard-system-helper-regressions.json', 'standard-system-helper-regressions.txt',
    'standard-system-helper-source.xml', 'standard-system-helper-installed.xml',
    'native-runtime.json', 'full-lock-source-inventory.json',
    'native-launcher-installed-proof.json', 'native-launcher-process.json',
    'full-bundle-verified.json', 'sealed-sbom.json'}
installer_wanted = {'inputs.json', 'native.json', 'proof.json', 'disk-budget.json',
                    'payload-manifest.json', 'good-build.json', 'negative-build.json'}
library_wanted = {'before-test-dependencies.json', 'installed-library.json', 'qualification.log',
                  'snapshot.log', 'test-dependencies.log'}
for number in range(1, 7):
    library_wanted.update({'library-' + str(number) + '.xml',
                          'library-' + str(number) + '-origins.json',
                          'library-' + str(number) + '.log'})

def selected_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe artifact member name.')
    # GitHub removes the declared common ci-results root from these artifacts.
    # The prior reviewed reader also accepts its literal optional directory prefix.
    parts = path.parts[1:] if path.parts[:1] == ('ci-results',) else path.parts
    if len(parts) == 1:
        return parts[0] in root_wanted
    if len(parts) == 2 and parts[0] == 'standard-installer':
        return parts[1] in installer_wanted
    if len(parts) == 2 and parts[0] == 'standard-update':
        return (parts[1] in wanted
                or (parts[1].startswith('native-managed-') and parts[1].endswith('.log')))
    if len(parts) == 3 and parts[0] == 'standard-update':
        if parts[1] in ('original-application-wheel', 'previous-application-wheel'):
            return parts[2] == 'manifest.json'
        directory = parts[1]
        return (directory.startswith('standard-library-')
                and len(directory) == len('standard-library-') + 32
                and all(character in '0123456789abcdef' for character in directory[len('standard-library-'):])
                and parts[2] in library_wanted)
    return False

for pin in ARTIFACTS:
    base = 'https://api.github.com/repos/' + REPOSITORY + '/actions/artifacts/' + str(pin['id'])
    metadata = json.loads(read(base, 65536))
    if (metadata['id'] != pin['id'] or metadata['workflow_run']['id'] != pin['run']
            or metadata['workflow_run']['head_sha'] != pin['commit']
            or metadata['digest'] != 'sha256:' + pin['sha256']
            or metadata['size_in_bytes'] != pin['bytes'] or metadata['expired']):
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
        if len(names) != 90 or sum(len(name) for name in names) > 65536:
            raise ValueError('The observed standard artifact inventory changed.')
        print('MEMBER_NAMES', json.dumps(names), flush=True)
        selected = [name for name in names if selected_member(name)]
        found = {PurePosixPath(name).name for name in selected}
        print('INVENTORY', json.dumps({'total_members': len(names), 'selected': selected,
            'absent_requested_records': sorted(wanted - found)}), flush=True)
        for name in selected:
            entry = archive.getinfo(name)
            if entry.file_size > 4 * 1024 * 1024:
                raise ValueError('Diagnostic member exceeds its read bound.')
            content = archive.read(name)
            print('MEMBER', json.dumps({'name': name, 'bytes': len(content),
                  'sha256': hashlib.sha256(content).hexdigest()}), flush=True)
            if name.endswith('.xml'):
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
                encoding = 'utf-16' if content.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
                print(name, content.decode(encoding, errors='replace')[-48000:], flush=True)
