"""Qualify exact reader appearance/pacing without dropping current reader contracts."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys


def terminal(path):
    values = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
              if line.lstrip().startswith('{')]
    values = [value for value in values if value.get('status') in ('PASS', 'FAIL', 'NOT_RUN')]
    if len(values) != 1:
        raise ValueError('A native probe must produce exactly one terminal receipt.')
    return values[0]


def check_native(value, config, source, *, appearance=False):
    for key, expected in config['required_status'].items():
        if type(value.get(key)) is not type(expected) or value[key] != expected:
            raise ValueError('Unexpected native receipt field: ' + key)
    expected = Counter(config['checks'])
    if appearance:
        contrast = value.get('high_contrast')
        if contrast is not None and type(contrast) is not bool:
            raise ValueError('The actual Windows contrast state is missing.')
        if contrast is False:
            expected.update(config['when_high_contrast_false'])
    checks = value.get('checks')
    if type(checks) is not list:
        raise ValueError('The native probe omitted its exact check inventory.')
    if appearance:
        if any(type(row) is not str for row in checks):
            raise ValueError('Invalid native appearance check.')
        actual = Counter(checks)
        if type(value.get('check_count')) is not int or value['check_count'] != len(checks):
            raise ValueError('Native appearance check count differs.')
    else:
        if any(type(row) is not dict or set(row) != {'name', 'passed'}
               or row['passed'] is not True for row in checks):
            raise ValueError('A native assistant command did not pass.')
        actual = Counter(row['name'] for row in checks)
    if actual != expected:
        raise ValueError('Native check Counter differs: ' + repr((actual, expected)))
    origins = value.get('module_origins')
    if type(origins) is not dict or set(origins) != set(config['modules']):
        raise ValueError('Native receipt omitted an installed module identity.')
    prefix = Path(sys.prefix).resolve()
    for name, relative in config['modules'].items():
        record = origins[name]
        path = Path(record['path']).resolve(strict=True)
        digest = sha256((source / relative).read_bytes()).hexdigest()
        if (not path.is_relative_to(prefix) or 'site-packages' not in {p.casefold() for p in path.parts}
                or record['sha256'] != digest or sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError('Native module is not the exact installed candidate: ' + name)
    if appearance:
        original = 'Exact original 😀.\r\nУкраїнський текст.\r\nThird line.'
        if value.get('source_sha256') != sha256(original.encode('utf-8')).hexdigest():
            raise ValueError('Appearance changed its exact original Unicode fixture.')
        if not re.fullmatch('[0-9a-f]{64}', value.get('project_sha256', '')):
            raise ValueError('Appearance probe omitted the retained project identity.')
    else:
        metrics = value['metrics']
        if any(type(metrics.get(name)) is not int or metrics[name] <= 0
               for name in ('frames', 'sample_rate')):
            raise ValueError('Native assistant preparation has no actual PCM metrics.')
        original = 'Prefix 😀.\r\nSelected source sentence for actual speech preparation. Another sentence.'
        if metrics.get('source_sha256') != sha256(original.encode('utf-8')).hexdigest():
            raise ValueError('Native assistant source differs from its Unicode fixture.')
        if not re.fullmatch('[0-9a-f]{64}', metrics.get('pcm_sha256', '')):
            raise ValueError('Native assistant has no exact prepared PCM digest.')
    return len(checks)


def qualify(source, output, work):
    source, output, work = Path(source).resolve(), Path(output).resolve(), Path(work).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(mode=0o700)
    if sys.platform != 'win32' or not sys.flags.isolated:
        raise ValueError('Reader qualification requires isolated actual Windows execution.')
    # Reuse the current actual collection/JUnit checker, with this component's
    # own declared population. Loading definitions performs no assistant work.
    common_path = source / 'scripts/qualify_assistant_tools.py'
    common_digest = sha256(common_path.read_bytes()).hexdigest()
    common = runpy.run_path(str(common_path), run_name='reader_exact_collection_helpers')
    run, collection, verify_junit = common['run'], common['collection'], common['verify_junit']
    oracle_path = source / 'scripts/reader_appearance_gate_oracle.json'
    oracle = json.loads(oracle_path.read_text(encoding='utf-8'))
    native_config = json.loads((source / oracle['native_oracle']).read_text(encoding='utf-8'))['native']
    interactive = json.loads((source / 'scripts/assistant_interactive_acceptance_oracle.json').read_text(encoding='utf-8'))['native']
    files = [*oracle['suites'], *oracle['helpers']]
    file_hashes = {name: sha256((source / name).read_bytes()).hexdigest() for name in files}
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source, text=True).strip()
    environment = dict(os.environ)
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        environment.pop(key, None)
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    binary = Path(environment['CARR_FFMPEG']).resolve(strict=True)
    discovered = shutil.which('ffmpeg')
    if discovered is None or Path(discovered).resolve(strict=True) != binary:
        raise ValueError('Reader qualification must use the exact verified FFmpeg.')
    report = dict(schema=1, status='FAIL', source_commit=commit, source_tree=tree,
        actual_windows=True, isolated=True, installed_package=True, source_cases=None,
        installed_cases=None, native_checks=None, test_files=file_hashes,
        collection_helper_sha256=common_digest, ffmpeg_sha256=sha256(binary.read_bytes()).hexdigest(),
        nvda='NOT_RUN', physical_audio='NOT_RUN', model_quality='NOT_RUN', full_platform='NOT_CLAIMED')
    baseline = None
    try:
        for phase in ('source', 'installed'):
            if phase == 'source':
                cwd, config = source, []
                environment['PYTHONPATH'] = os.pathsep.join((str(source / 'src'), str(source)))
                python = [sys.executable, '-B', '-X', 'utf8']
            else:
                cwd = work / 'installed-tests'
                cwd.mkdir()
                for name in files:
                    destination = cwd / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / name, destination)
                    if sha256(destination.read_bytes()).hexdigest() != file_hashes[name]:
                        raise ValueError('Copied installed regression differs from the candidate.')
                (cwd / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
                environment.pop('PYTHONPATH', None)
                python = [sys.executable, '-I', '-B', '-X', 'utf8']
                config = ['-c', str(cwd / 'pytest.ini')]
            arguments = ['-q', '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                         '--rootdir', str(cwd), '--import-mode=importlib', *config, *oracle['suites']]
            for mode in ('collect', 'execute'):
                label = 'reader-appearance-' + phase + '-' + mode
                inventory = output / (label + '-collection.json')
                junit = output / ('reader-appearance-' + phase + '.xml')
                selected = [*arguments, '--collect-only'] if mode == 'collect' else [
                    *arguments, '--junitxml=' + str(junit)]
                run([*python, '-c', common['PYTEST_DRIVER'], str(source), str(cwd), phase, mode,
                     str(oracle_path), str(output / (label + '-origins.json')), str(inventory), *selected],
                    cwd, environment, output, label, timeout=900)
                rows = collection(inventory, oracle)
                if len(rows) != oracle['expected_total']:
                    raise ValueError('The complete reader regression count differs.')
                identity = Counter((row['path'], row['nodeid'], row['classname'], row['name']) for row in rows)
                if baseline is None:
                    baseline = identity
                    (output / 'reader-appearance-executed-oracle.json').write_text(json.dumps(dict(
                        source_commit=commit, source_tree=tree, actual_collection=True,
                        tests=rows, test_files=file_hashes), sort_keys=True, indent=2) + '\n', encoding='utf-8')
                elif identity != baseline:
                    raise ValueError('Source/installed reader collection and execution differ.')
                if mode == 'execute':
                    report[phase + '_cases'] = verify_junit(junit, rows)
        probe = source / 'scripts/windows_reader_appearance_smoke.py'
        probe_hash = sha256(probe.read_bytes()).hexdigest()
        run([sys.executable, '-I', '-B', '-X', 'utf8', str(probe), '--native'],
            work, environment, output, 'reader-appearance-native', timeout=180)
        native = terminal(output / 'reader-appearance-native.stdout.txt')
        report['native_checks'] = check_native(native, native_config, source, appearance=True)
        report['native'] = native
        if sha256(probe.read_bytes()).hexdigest() != probe_hash:
            raise ValueError('Native appearance probe changed while executing.')
        report['native_probe_sha256'] = probe_hash
        (output / 'reader-appearance-native.json').write_text(json.dumps(native, ensure_ascii=False,
            sort_keys=True, indent=2) + '\n', encoding='utf-8')
        # The newly composed live reader must retain actual SAPI/FFmpeg controls.
        playback_report = output / 'reader-appearance-retained-playback.json'
        run([sys.executable, '-I', '-B', '-X', 'utf8', str(source / 'scripts/r7_native_gate.py'),
             'live-playback', '--report', str(playback_report)],
            work, environment, output, 'reader-appearance-retained-playback', timeout=300)
        playback = json.loads(playback_report.read_text(encoding='utf-8'))
        if (playback.get('status') != 'PASS' or playback.get('checks') != 32
                or playback.get('source_commit') != commit or playback.get('source_tree') != tree):
            raise ValueError('The actual native playback regression did not qualify this candidate.')
        report['retained_playback_checks'] = 32
        assistant_probe = source / 'scripts/windows_assistant_interactive_smoke.py'
        run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', common['NATIVE_DRIVER'],
             str(assistant_probe), str(work / 'native-assistant')], work, environment, output,
            'reader-appearance-retained-assistant', timeout=240)
        assistant = terminal(output / 'reader-appearance-retained-assistant.stdout.txt')
        report['retained_assistant_checks'] = check_native(assistant, interactive, source)
        if report['retained_assistant_checks'] != 26:
            raise ValueError('The actual native assistant regression omitted a required check.')
        (output / 'reader-appearance-retained-assistant.json').write_text(json.dumps(
            assistant, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        if sha256(common_path.read_bytes()).hexdigest() != common_digest:
            raise ValueError('The exact collection checker changed during qualification.')
        for name, digest in file_hashes.items():
            if sha256((source / name).read_bytes()).hexdigest() != digest:
                raise ValueError('A reader regression changed during qualification.')
        report['status'] = 'PASS'
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        (output / 'reader-appearance-summary.json').write_text(json.dumps(report, ensure_ascii=False,
            sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.source, args.output, args.work), ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
