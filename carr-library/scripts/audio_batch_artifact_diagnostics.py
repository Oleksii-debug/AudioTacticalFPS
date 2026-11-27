"""Read the existing exact audio-batch artifacts; never execute their contents."""
import hashlib
import io
import json
import os
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

REPOSITORY = 'Oleksii-debug/AudioTacticalFPS'
RUN = 46001491511
COMMIT = '5c98cc855c55a12a6d43a7a33a0a7f83e1aafd24'
ARTIFACTS = {
    15198991074: 'c55a1e52159bb68286878c6cacf9decab54e86f932619f95551d684dde9781d1',
}


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
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.build_opener(Redirect).open(request, timeout=90) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Artifact response exceeds its explicit byte bound.')
    return raw


for identity, expected in ARTIFACTS.items():
    base = 'https://api.github.com/repos/' + REPOSITORY + '/actions/artifacts/' + str(identity)
    metadata = json.loads(read(base, 65536))
    if (metadata['id'] != identity or metadata['workflow_run']['id'] != RUN
            or metadata['workflow_run']['head_sha'] != COMMIT
            or metadata['digest'] != 'sha256:' + expected or metadata['expired']):
        raise ValueError('Existing artifact provenance differs.')
    raw = read(base + '/zip', 8 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Existing artifact archive failed its recorded digest.')
    print('ARTIFACT', identity, metadata['name'], len(raw), expected, flush=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 500:
            raise ValueError('Ambiguous or oversized artifact inventory.')
        print('INVENTORY', json.dumps(names), flush=True)
        for name in ('source-gate.json', 'installed-gate.json', 'source-junit.xml', 'installed-junit.xml',
                     'source-pytest.txt', 'installed-pytest.txt'):
            if name not in names:
                continue
            entry = archive.getinfo(name)
            if entry.file_size > 4 * 1024 * 1024:
                raise ValueError('Diagnostic member exceeds its read bound.')
            content = archive.read(name)
            if name.endswith('-junit.xml'):
                root = ET.fromstring(content)
                cases = root.findall('.//testcase')
                print(name, 'COUNTS', json.dumps({
                    'cases': len(cases), 'failures': len(root.findall('.//failure')),
                    'errors': len(root.findall('.//error')), 'skips': len(root.findall('.//skipped'))}), flush=True)
                for case in cases:
                    failures = list(case.findall('failure')) + list(case.findall('error')) + list(case.findall('skipped'))
                    print('CASE', case.get('classname'), case.get('name'), 'FAIL' if failures else 'PASS', flush=True)
                    for failure in failures:
                        print((failure.text or '')[-12000:], flush=True)
            elif name == 'converter-gate.json':
                value = json.loads(content)
                for key in ('runtime_files',):
                    if key in value:
                        value[key] = {k: {'entries': len(v)} for k, v in value[key].items()}
                installed = value.get('installed_identity', {})
                if 'files' in installed:
                    installed['files'] = {'entries': len(installed['files'])}
                print(name, json.dumps(value, sort_keys=True)[-50000:], flush=True)
            else:
                print(name, content.decode('utf-8', errors='replace')[-24000:], flush=True)
