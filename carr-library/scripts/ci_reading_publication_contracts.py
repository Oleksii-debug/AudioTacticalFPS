"""Run exact Unicode reading and publication contracts against source and a verified wheel.

Reuses the current strict runner, wheel proof, native origin snapshots and
pinned audio-input verifier. Actual playback/NVDA remain separate gates.
"""
import argparse
from hashlib import file_digest, sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "tests/ui/test_ebook_dialog.py": 38,
    "tests/studio/test_publishing_boundaries.py": 19,
    "tests/studio/test_publishing_handlers.py": 12
}
PINS = {
    "src/carr_library/ui/ebook_model.py": "358ff820e01e07410a3227f895bec17905bc3e68",
    "src/carr_library/ui/ebook_dialog.py": "224cc307e27732b284f1ddfb2b076a6a31762849",
    "src/carr_library/ui/ebook_actions.py": "10c7f842e1dcfcde6cd8dafee994beca2e035cd6",
    "src/carr_library/ui/ebook_audio_dialog.py": "15f27a5eb5ca8491b43dcfbe6d550de495b8dbe5",
    "src/carr_library/ui/publishing_dialog.py": "9f5047e74f38a17189e43157c0f432d118e3cdeb",
    "src/carr_library/ui/import_center.py": "b304c895432fe69f68b7ea7a768ee0c70c9d4f0d",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/studio/publishing.py": "e369f0d40cbc818d9374e1d4f84bc3e28e09ad63",
    "src/carr_library/studio/daisy.py": "bbc6f559aa3aa11a14fd6eefa6c802058541f98c",
    "src/carr_library/studio/render.py": "3fafc206b592fedd3cf5c836c1b1bc7a0387e5ab",
    "src/carr_library/studio/project.py": "63a20c64909db77b9958e9c16627b78f08050d95",
    "src/carr_library/audio/audiobook.py": "64932ca712ce871cb77bebb9ad0c63dab3abd996",
    "src/carr_library/audio/dsp.py": "f7833b5f600e3a822a3fa2f2068661c922576230",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/audio/cleanup.py": "77d69f8406db93dc0ff6110b9597ad805bd92dc5",
    "src/carr_library/research/export_file.py": "335fd4d03ed4e612d628ec2f93b615feba49e97d",
    "src/carr_library/extract/document_formats.py": "557798e324a126ee12bb439c6fea25237eece59d",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/file_publication.py": "2ce3bdaa2c489cfeff301907e29ca6b552a2f90f",
    "src/carr_library/runtime_settings.py": "00df790d393f9d54f87d0dcf891e6506916437a1",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "tests/ui/test_ebook_dialog.py": "63a70758799bfa88bc00f042d49a029a4f028a7b",
    "tests/studio/test_publishing_boundaries.py": "92af546865656226faceae9c1e1cb4ea1b97a64d",
    "tests/studio/test_publishing_handlers.py": "82ce7755f62c716ed70f63fcb672c0e8d39e4806",
    "tests/studio/test_publishing.py": "3f6ad2d411de67c28296c3bb9c8d9bafb6b80ba3",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-audio.lock.json": "2bc706ffc6208f5596a10abee61d3da33f20e6c6",
    "scripts/windows_text_positions_smoke.py": "0aba933834760f198e685d55926b639d799e1c1d",
    "tests/ui/native_document_formats_smoke.py": "986a1afc208e6d6ba057b95e0346c3306ee9bde9",
    "scripts/windows_publishing_smoke.py": "bf37a4b206c6f05fec383902eae577f528a05f3d"
}
COMPANIONS = ["tests/studio/test_publishing.py"]
MODULES = ["carr_library.ui.ebook_model","carr_library.ui.ebook_dialog","carr_library.ui.ebook_actions","carr_library.ui.ebook_audio_dialog","carr_library.ui.publishing_dialog","carr_library.ui.import_center","carr_library.ui.desktop_app","carr_library.studio.publishing","carr_library.studio.daisy","carr_library.studio.render","carr_library.studio.project","carr_library.audio.audiobook","carr_library.audio.dsp","carr_library.audio.process","carr_library.audio.cleanup","carr_library.research.export_file","carr_library.extract.document_formats","carr_library.durable_state","carr_library.file_publication","carr_library.runtime_settings","carr_library.operations.broker","carr_library.application","bootstrap"]


def require(value, message):
    if not value:
        raise ValueError(message)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def sources():
    result = {}
    for relative, expected in PINS.items():
        path = ROOT / relative
        require(path.is_file() and not path.is_symlink(), 'Missing ordinary source: ' + relative)
        raw = path.read_bytes()
        actual = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        require(actual == expected, 'Changed pinned source: ' + relative)
        result[relative] = dict(git_blob=actual, sha256=sha256(raw).hexdigest(), bytes=len(raw))
    return result


def git(*args):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(ROOT.parent), *args],
        check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def child(command, output, *, cwd, ci, timeout=600):
    environment = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTEST_PLUGINS', 'PYTEST_ADDOPTS'):
        environment.pop(key, None)
    environment.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
        ORT_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
    isolation = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
    with output.open('xb') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, **isolation)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            ci._terminate_process_tree(process)
            raise
    require(output.stat().st_size <= 8 * 1024**2, 'Required-suite log exceeds its finite bound.')
    print(output.read_text(encoding='utf-8', errors='replace'), flush=True)
    return code


INSTALLED_CHILD = r"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

wheel, proof_path, checkout, receipt, contract_path = map(Path, sys.argv[1:6])
arguments = sys.argv[6:]
contract = json.loads(contract_path.read_text(encoding='utf-8'))
proof = json.loads(proof_path.read_text(encoding='utf-8'))
assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__
assert sha256(wheel.read_bytes()).hexdigest() == proof['wheel_sha256']
import carr_library, bootstrap
package = Path(carr_library.__file__).resolve(strict=True).parent
helper = Path(bootstrap.__file__).resolve(strict=True).parent
assert package.parent == helper.parent
assert not package.is_relative_to(checkout) and not helper.is_relative_to(checkout)
assert all(not Path(p).resolve().is_relative_to(checkout) for p in sys.path if p)
payload = {}
with ZipFile(wheel) as archive:
    for name in archive.namelist():
        if name.startswith(('carr_library/', 'bootstrap/')) and not name.endswith('/'):
            payload[name] = sha256(archive.read(name)).hexdigest()
assert payload
def verify():
    rows = {}
    for relative, digest in payload.items():
        path = package.parent / relative
        assert path.is_file() and not path.is_symlink()
        assert sha256(path.read_bytes()).hexdigest() == digest, relative
    for name in contract['modules']:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        assert path.is_relative_to(package if name.startswith('carr_library') else helper)
        rows[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return rows
before = verify()
sys.argv = ['pytest', *arguments]
try:
    exec(compile(contract['pytest_child'], '<existing-ci-check-policy>', 'exec'),
         {'__name__': '__main__'})
except SystemExit as result:
    code = result.code
else:
    raise AssertionError('Existing strict pytest child did not exit explicitly.')
after = verify()
assert after == before
receipt.write_text(json.dumps({'status': 'PASS', 'origins': before,
    'wheel_sha256': proof['wheel_sha256'], 'wheel_payload_files_verified': len(payload),
    'isolated': True, 'source_injection': False, 'payload_unchanged': True},
    indent=2, sort_keys=True) + '\n', encoding='utf-8')
raise SystemExit(code)
"""



ORIGIN_CHILD = r"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

wheel, proof_path, checkout, receipt, contract_path = map(Path, sys.argv[1:6])
arguments = sys.argv[6:]
contract = json.loads(contract_path.read_text(encoding='utf-8'))
proof = json.loads(proof_path.read_text(encoding='utf-8'))
assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__
assert sha256(wheel.read_bytes()).hexdigest() == proof['wheel_sha256']
import carr_library, bootstrap
package = Path(carr_library.__file__).resolve(strict=True).parent
helper = Path(bootstrap.__file__).resolve(strict=True).parent
assert package.parent == helper.parent
assert not package.is_relative_to(checkout) and not helper.is_relative_to(checkout)
assert all(not Path(p).resolve().is_relative_to(checkout) for p in sys.path if p)
payload = {}
with ZipFile(wheel) as archive:
    for name in archive.namelist():
        if name.startswith(('carr_library/', 'bootstrap/')) and not name.endswith('/'):
            payload[name] = sha256(archive.read(name)).hexdigest()
assert payload
def verify():
    rows = {}
    for relative, digest in payload.items():
        path = package.parent / relative
        assert path.is_file() and not path.is_symlink()
        assert sha256(path.read_bytes()).hexdigest() == digest, relative
    for name in contract['modules']:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        assert path.is_relative_to(package if name.startswith('carr_library') else helper)
        rows[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return rows
origins = verify()
receipt.write_text(json.dumps({'status': 'PASS', 'qualification': 'identity-only',
    'origins': origins, 'wheel_sha256': proof['wheel_sha256'],
    'wheel_payload_files_verified': len(payload), 'isolated': True,
    'source_injection': False}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
"""

def audio_inputs(selected):
    require(os.name == 'nt', 'This exact native reading/publication contract needs actual Windows.')
    pin = json.loads((ROOT / 'packaging/windows-audio.lock.json').read_text(encoding='utf-8'))
    rows = {}
    for name in ('ffmpeg', 'ffprobe'):
        path = (selected / 'bin' / (name + '.exe')).resolve(strict=True)
        found = shutil.which(name)
        require(found and Path(found).resolve(strict=True) == path,
                'Actual codec discovery differs from the explicitly selected runtime: ' + name)
        before = path.stat()
        require(path.is_file() and not path.is_symlink() and before.st_nlink == 1,
                'Selected codec is not an ordinary unlinked file.')
        with path.open('rb') as stream:
            digest = file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                'Selected codec changed while hashing.')
        expected = pin['output_files']['bin/' + name + '.exe']
        require(before.st_size == expected['size'] and digest == expected['sha256'],
                'Selected codec bytes differ from the existing lock.')
        rows[name] = dict(path=str(path), size=before.st_size, sha256=digest)
    return rows


DOCUMENT_CHECKS = ["native document modules are loaded from the installed distribution","native file selector includes all additional formats","document support help is reachable from the native menu","format help has a named read-only multiline text control","format help describes unverified text and unsupported conversions","format help describes bounded raster frames and independent review","format help can receive native keyboard focus","native document help menu event is handled","native queue completes both actual document imports","queue does not approve additional format text","native selected-file event is handled","selected file details explain unverified logical segments","actual result details control can receive keyboard focus","native queue recognizes HTML and Markdown as two documents","native Start queued files button event launches work","native import controls expose their spoken names","document preflight runs asynchronously with cancellation enabled","native document import workflow completed","native import retains source bytes and digest exactly","script text is not searchable after real HTML parsing","native document reader loads asynchronously","native reader shows actual imported Unicode text","native reader exposes named read-only source text","native reader exposes the unverified logical source location","native imported text receives keyboard focus","native next-document-segment button event is handled","native next-segment reads second source paragraph","reading never converts imported text into approved quotation"]
PUBLISHING_CHECKS = ["publication UI comes from installed package","real native Windows widgets","actual FFmpeg produced a completed Studio fixture","M4B carries actual measured chapter names and bounds","LRC retains measured line starts without inferred word timestamps","real native main loop is active","export controls have native accessible names","completed job is listed without implicit selection","native export completed with a durable receipt","shared progress completes only after publication verification","native completion focuses readable publication status","exact script and measured line overlays are retained","embedded audio is MP3 with an actual byte identity","native explicit receipt verification succeeds","native duplicate export cannot overwrite completed bytes","publication format and publisher are native named controls","DAISY requires explicitly supplied publisher metadata","native DAISY export commits the second durable receipt","DAISY preserves exact script, measured audio and explicit publisher","native DAISY verification follows the selected receipt","native keyboard close completes","installed module origin: ui/publishing_dialog.py","exact installed source bytes: ui/publishing_dialog.py","installed module origin: audio/audiobook.py","exact installed source bytes: audio/audiobook.py","installed module origin: audio/dsp.py","exact installed source bytes: audio/dsp.py","installed module origin: studio/daisy.py","exact installed source bytes: studio/daisy.py","installed module origin: studio/publishing.py","exact installed source bytes: studio/publishing.py","installed module origin: studio/render.py","exact installed source bytes: studio/render.py","explicit selected audio executable: ffmpeg","pinned audio executable bytes: ffmpeg","explicit selected audio executable: ffprobe","pinned audio executable bytes: ffprobe","native Space down: &Load jobs / receipts","native Space up: &Load jobs / receipts","native Space down: &Export publication","native Space up: &Export publication","native Space down: &Verify receipt","native Space up: &Verify receipt","native Space down: &Close","native Space up: &Close"]


def native_contract(*, work, wheel, contract, evidence, audio, ci):
    wrapper = work / 'native-origin-child.py'
    wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
    before, after = evidence / 'native-origins-before.json', evidence / 'native-origins-after.json'
    def snapshot(path, log):
        require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(path), str(contract)],
            log, cwd=work, ci=ci, timeout=60) == 0, 'Installed native origin snapshot failed.')
        return json.loads(path.read_text(encoding='utf-8'))
    first = snapshot(before, evidence / 'native-origins-before.txt')
    results, failures = [], []
    jobs = [
        ('text-source', ROOT / 'scripts/windows_text_positions_smoke.py',
         ['--source', str(ROOT), '--report', str(evidence / 'native-text-source.json')], 90),
        ('text-installed', ROOT / 'scripts/windows_text_positions_smoke.py',
         ['--report', str(evidence / 'native-text-installed.json')], 90),
        ('document-formats', ROOT / 'tests/ui/native_document_formats_smoke.py', ['--native'], 180),
        ('publishing', ROOT / 'scripts/windows_publishing_smoke.py',
         ['--audio-directory', str(audio), '--audio-lock', str(ROOT / 'packaging/windows-audio.lock.json')], 330),
    ]
    try:
        for name, script, arguments, timeout in jobs:
            log = evidence / ('native-' + name + '.txt')
            try:
                code = child([sys.executable, '-I', '-B', '-X', 'utf8', str(script), *arguments],
                             log, cwd=work, ci=ci, timeout=timeout)
                require(code == 0, 'Required actual native phase failed: ' + name)
                raw = log.read_text(encoding='utf-8-sig')
                reports = []
                for match in re.finditer(r'(?m)^\{', raw):
                    try:
                        value, _ = json.JSONDecoder().raw_decode(raw[match.start():])
                    except ValueError:
                        continue
                    if isinstance(value, dict) and ('checks' in value):
                        reports.append(value)
                require(len(reports) == 1, 'Required native report is missing or ambiguous: ' + name)
                value = reports[0]
                if name.startswith('text-'):
                    require(value.get('schema') == 'carr-native-text-positions/1'
                            and value.get('status') == 'PASS'
                            and value.get('mode') == name.removeprefix('text-')
                            and not value.get('errors')
                            and len(value['checks']) == len(set(value['checks'])) == 20
                            and len(value['profiles']) == 3, 'Native position inventory differs.')
                    for row in value['profiles']:
                        require(row['all_boundaries'] == row['source_characters'] + 1
                                and row['selected_native'][1] > row['selected_native'][0],
                                'Every native text boundary and selected Unicode fragment is required.')
                    require(all(value[key] == 'NOT_RUN' for key in ('audio', 'manual_nvda', 'release')),
                            'Native coordinate work cannot qualify playback, NVDA or release.')
                    saved = json.loads((evidence / ('native-' + name + '.json')).read_text(encoding='utf-8'))
                    require(saved == value, 'Native position saved report differs from actual output.')
                elif name == 'document-formats':
                    require(value.get('status') == 'PASS' and value.get('source_override') is None
                            and value.get('native_file_picker_operation') == value.get('nvda') == 'NOT_RUN',
                            'Complete installed native document acceptance is required.')
                    require(all(raw.splitlines().count('PASS: ' + item) >= 1 for item in DOCUMENT_CHECKS),
                            'A required document import/read/native check is absent.')
                    require(value['checks'] == sum(line.startswith('PASS: ') for line in raw.splitlines()),
                            'Actual native document count differs from retained evidence.')
                    require(set(value['origins']) == {'carr_library.extract.document_formats',
                            'carr_library.ui.import_center', 'carr_library.ui.desktop_app'},
                            'Native document origins differ.')
                    for module, origin in value['origins'].items():
                        require(Path(origin).resolve(strict=True) ==
                                Path(first['origins'][module]['path']).resolve(strict=True),
                                'Native document module differs from installed proof.')
                    write(evidence / 'native-document-formats.json', value)
                else:
                    require(value.get('result') == value.get('native_keyboard') ==
                            value.get('daisy_2_02_interchange') == value.get('measured_m4b_chapters') ==
                            value.get('line_lrc') == 'PASS', 'Actual complete publication report is required.')
                    require(set(PUBLISHING_CHECKS) <= set(value['checks']),
                            'Required native publication checks are missing.')
                    require(all(value.get(key) == 'NOT_RUN' for key in ('epubcheck',
                        'independent_daisy_validation', 'reader_interoperability', 'speech_quality',
                        'manual_keyboard', 'nvda')), 'Publication does not establish external quality gates.')
                    require(set(value['audio_runtime']) == {'ffmpeg', 'ffprobe'},
                            'Selected actual publication codec identities are missing.')
                    pin = json.loads((ROOT / 'packaging/windows-audio.lock.json').read_text(encoding='utf-8'))
                    for executable, observed in value['audio_runtime'].items():
                        require(observed['sha256'] == pin['output_files']['bin/' + executable + '.exe']['sha256']
                                and Path(observed['path']).resolve(strict=True) == audio / 'bin' / (executable + '.exe'),
                                'Native publication used an unreviewed executable.')
                    write(evidence / 'native-publishing.json', value)
                results.append({'phase': name, 'status': 'PASS',
                    'log_sha256': sha256(log.read_bytes()).hexdigest()})
            except Exception as error:
                failures.append({'phase': name, 'type': type(error).__name__, 'error': str(error)[:1024]})
    finally:
        last = snapshot(after, evidence / 'native-origins-after.txt')
        require(last == first, 'Installed wheel or native origin changed during actual native work.')
    result = {'status': 'FAIL' if failures else 'PASS', 'phases': results, 'failures': failures,
              'required_phases': 4, 'origin_count': len(first['origins']),
              'installed_payload_unchanged': True, 'audio_device_playback': 'NOT_RUN',
              'manual_nvda': 'NOT_RUN', 'private_model_inference': 'NOT_RUN', 'release': 'NOT_RUN'}
    write(evidence / 'native-summary.json', result)
    require(not failures and len(results) == 4, 'Every required native reading/publication phase must pass.')
    return result


def main():
    # Isolated Python ignores PYTHONUTF8; display the retained UTF-8 child log.
    # This changes only this orchestrator's stream, not child/test encodings.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--audio-directory', required=True, type=Path)
    args = parser.parse_args()
    require(__debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head), 'Exact commit and assertions required.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Checkout differs from exact event head.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before = sources()
    selected_audio = args.audio_directory.resolve(strict=True)
    audio_before = audio_inputs(selected_audio)
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    require(git('show', args.expected_head + ':' + own_relative) == Path(__file__).read_text(encoding='utf-8').strip(),
            'Orchestrator differs from its committed source.')
    identity = dict(source_commit=args.expected_head, source_tree=git('rev-parse', 'HEAD^{tree}'),
        inputs=before, audio_runtime=audio_before, python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('reading_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('reading_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # Three whole suites use two waves with their original 600 s limits;
    # 1500 s includes finite tree cleanup and report handling.
    code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                  '--jobs', '2', *SUITES], source_log, cwd=ROOT, ci=ci, timeout=1500)
    lines = source_log.read_text(encoding='utf-8').splitlines()
    expected = [f'PASS: {suite}: junit_passed={count}, failed=0, skipped=0, pytest_exit=0'
                for suite, count in SUITES.items()]
    require(code == 0 and all(lines.count(line) == 1 for line in expected)
            and lines[-1] == f'Required suites complete: {len(SUITES)}; passed={len(SUITES)}; failed=0',
            'Every source whole suite must pass without skips or expected failures.')
    write(evidence / 'source-summary.json', dict(status='PASS', suites=SUITES,
          passed=sum(SUITES.values()), failed=0, skipped=0, expected_failures=False,
          log_sha256=sha256(source_log.read_bytes()).hexdigest()))
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-reading-publication-', dir=work_parent) as tmp:
        work = Path(tmp)
        wheels = work / 'wheel'
        wheels.mkdir()
        build_code = child([sys.executable, '-B', '-m', 'pip', 'wheel', '--no-index',
            '--no-deps', '--no-build-isolation', '--wheel-dir', str(wheels), str(ROOT)],
            evidence / 'wheel-build.txt', cwd=work, ci=ci, timeout=300)
        require(build_code == 0, 'Current source wheel build failed.')
        found = list(wheels.glob('*.whl'))
        require(len(found) == 1, 'Exactly one current application wheel is required.')
        wheel = found[0]
        proof = proof_module.verify_application_wheel(wheel, ROOT.parent, args.expected_head)
        write(evidence / 'source-wheel-proof.json', proof)
        install_code = child([sys.executable, '-I', '-B', '-m', 'pip', 'install', '--no-index',
            '--no-deps', '--force-reinstall', str(wheel)],
            evidence / 'wheel-install.txt', cwd=work, ci=ci, timeout=180)
        require(install_code == 0, 'Verified current wheel was not installed.')
        require(child([sys.executable, '-I', '-B', '-m', 'pip', 'check'],
            evidence / 'installed-pip-check.txt', cwd=work, ci=ci, timeout=60) == 0,
            'Installed dependency closure is inconsistent.')
        staged = work / 'isolated-tests'
        staged.mkdir()
        for relative in (*SUITES, *COMPANIONS):
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
            require(sha256(target.read_bytes()).hexdigest() == before[relative]['sha256'],
                    'Companion changed during staging.')
        ini = staged / 'pytest.ini'
        ini.write_text('[pytest]\naddopts =\n', encoding='utf-8')
        wrapper = work / 'installed_child.py'
        wrapper.write_text(INSTALLED_CHILD, encoding='utf-8')
        contract = work / 'child-contract.json'
        require(len(MODULES) == len(set(MODULES)) == 23,
                'Every selected reading/publication origin and bootstrap are required.')
        write(contract, dict(pytest_child=ci.PYTEST_CHILD, modules=MODULES))
        installed = []
        for number, (suite, expected_count) in enumerate(SUITES.items(), 1):
            xml = evidence / f'installed-{number}.xml'
            receipt = evidence / f'installed-{number}-origins.json'
            log = evidence / f'installed-{number}.txt'
            result = child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
                str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(receipt), str(contract),
                '-v', '-c', str(ini), '--rootdir', str(staged), '--confcutdir', str(staged),
                '--import-mode=importlib', '-o', 'pythonpath=', '-o', 'addopts=',
                '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
                '-o', 'faulthandler_exit_on_timeout=false', '--junitxml', str(xml),
                '--basetemp', str(work / f'pytest-{number}'), '--', str(staged / suite)],
                log, cwd=staged, ci=ci)
            cases = list(ET.parse(xml).iter('testcase'))
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            require(result == 0 and passed == expected_count and failed == skipped == 0
                    and receipt.is_file(), 'Installed whole contract failed: ' + suite)
            installed.append(dict(suite=suite, passed=passed, failed=failed, skipped=skipped,
                                  exit=result, origins=json.loads(receipt.read_text(encoding='utf-8'))))
        for relative in (*SUITES, *COMPANIONS):
            require(sha256((staged / relative).read_bytes()).hexdigest() == before[relative]['sha256'],
                    'An installed test or companion changed.')
        native = native_contract(work=work, wheel=wheel, contract=contract,
            evidence=evidence, audio=selected_audio, ci=ci)
    require(audio_inputs(selected_audio) == audio_before, 'Selected codec changed during actual contracts.')
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, native=native,
        qualification='Exact reading/publication source and installed contracts plus four native phases only; playback, NVDA, acoustics and release remain separate.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
