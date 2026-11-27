"""Actual local CPU inference through the unchanged, review-only product gateway.

This is a qualification probe, not an installer, planner, gateway or engine.
Only fixed synthetic prompts are sent. No model-supplied action is executed.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from uuid import uuid4

PRODUCT = Path(__file__).resolve().parents[1]
PINS = PRODUCT / 'packaging' / 'local-llm-proof-inputs.json'
GRAMMAR = PRODUCT / 'packaging' / 'local-llm-proof-json.gbnf'
ALIAS = 'carr-qwen3-06b-q8-qualification'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def file_hash(path):
    digest = sha256()
    with path.open('rb') as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def acquire(record, root):
    """No retries, executable install scripts, model code or mutable revisions."""
    destination = root / record['filename']
    require(not destination.exists(), 'Qualification inputs must use a fresh owned directory.')
    require(shutil.disk_usage(root).free >= record['size_bytes'] + 512 * 1024**2,
            'The download would consume the reserved disk headroom.')
    temporary = destination.with_name(destination.name + '.part')
    digest, count, started = sha256(), 0, time.monotonic()
    request = urllib.request.Request(record['url'], headers={'User-Agent': 'carr-local-llm-qualification/1'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open('xb') as output:
            declared = response.getheader('Content-Length')
            require(not declared or (declared.isdecimal() and int(declared) == record['size_bytes']),
                    'The publisher response has an unexpected byte length.')
            while block := response.read(4 * 1024 * 1024):
                count += len(block)
                require(count <= record['size_bytes'] and time.monotonic() - started <= 300,
                        'Acquisition exceeded its byte/time allowance.')
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        require((count, digest.hexdigest()) == (record['size_bytes'], record['sha256']),
                'Downloaded bytes do not match the separately inspected publisher metadata.')
        os.link(temporary, destination)
        temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return {'filename': record['filename'], 'size_bytes': count, 'sha256': digest.hexdigest()}


def unpack_runtime(archive, destination, maximum):
    with tarfile.open(archive, 'r:gz') as stream:
        members = stream.getmembers()
        total = sum(item.size for item in members if item.isfile())
        require(len(members) <= 512 and total <= maximum, 'Runtime extraction exceeds its inventory bound.')
        for item in members:
            path = PurePosixPath(item.name)
            require(not path.is_absolute() and '..' not in path.parts
                    and (item.isfile() or item.isdir() or item.issym()),
                    'Unexpected runtime archive member.')
        destination.mkdir(mode=0o700)
        stream.extractall(destination, members=members, filter='data')
    inventory = []
    for path in sorted(destination.rglob('*')):
        if path.is_symlink():
            require(path.resolve().is_relative_to(destination.resolve()), 'Runtime link escapes its directory.')
            inventory.append({'path': path.relative_to(destination).as_posix(), 'link': str(path.readlink())})
        elif path.is_file():
            inventory.append({'path': path.relative_to(destination).as_posix(),
                              'size_bytes': path.stat().st_size, 'sha256': file_hash(path)})
    return {'members': len(members), 'regular_bytes': total, 'files': inventory}


def child_allowance():
    resource.setrlimit(resource.RLIMIT_CPU, (120, 125))
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def stop_owned(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def wait_ready(process, port):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        require(process.poll() is None, 'The owned model server exited before readiness.')
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.25)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            raw = response.read(4097)
            if response.status == 200 and json.loads(raw) == {'status': 'ok'}:
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(.1)
    raise RuntimeError('The owned model server exceeded its startup allowance.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Fresh private qualification directory')
    args = parser.parse_args()
    require(sys.platform == 'linux', 'This isolated probe qualifies only the selected Linux CPU runtime.')
    root = args.root.absolute()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    pins = json.loads(PINS.read_text(encoding='utf-8'))
    report = {
        'schema': 'carr-local-llm-qualification/1', 'status': 'running',
        'source_commit': subprocess.check_output(['git', '-C', str(PRODUCT), 'rev-parse', 'HEAD'], text=True).strip(),
        'model': pins['model'], 'runtime': pins['runtime'], 'licenses': pins['licenses'],
        'cases': [], 'actual_transport': [], 'assistant_jobs_created': None, 'automatic_tools_executed': None,
        'limitations': ['Linux CPU only', 'Synthetic prompts only', 'No language-quality benchmark',
                        'No native Windows or installed-package qualification', 'No OS network-denial test'],
        'runtime_options': {'cpu_threads': 2, 'context_tokens': 2048, 'prediction_tokens': 256,
                            'slots': 1, 'offline_option': True, 'thinking': False,
                            'grammar': 'upstream generic JSON syntax with the pinned Qwen non-thinking prefix; no plan fields or tool choices forced',
                            'wall_watchdog_seconds': 240, 'address_space_limit_bytes': 3 * 1024**3},
    }
    native = None
    complete = threading.Event()
    started = time.monotonic()
    try:
        actual_source = {name: file_hash(PRODUCT / 'src/carr_library' / name)
                         for name in pins['product_source_sha256']}
        require(actual_source == pins['product_source_sha256'],
                'The proposal gateway or validated executor differs from the reviewed product bytes.')
        report['product_source_sha256'] = actual_source
        require(file_hash(GRAMMAR) == pins['generation_grammar']['sha256'],
                'The native JSON syntax grammar differs from the reviewed bytes.')
        report['generation_grammar'] = pins['generation_grammar']
        identities = [acquire(item, root) for item in (*pins['licenses'], pins['runtime'], pins['model'])]
        (root / 'asset-identities.json').write_text(json.dumps(identities, indent=2) + '\n', encoding='utf-8')
        report['download_identities'] = identities
        report['runtime_inventory'] = unpack_runtime(root / pins['runtime']['filename'],
            root / 'runtime', pins['runtime']['maximum_expanded_bytes'])
        binary = root / 'runtime' / pins['runtime']['binary']
        model = root / pins['model']['filename']
        environment = dict(os.environ)
        for name in tuple(environment):
            if name.startswith(('LLAMA_', 'GGML_', 'DYLD_')) or name == 'LD_PRELOAD':
                environment.pop(name)
        environment['LD_LIBRARY_PATH'] = str(binary.parent)
        environment['OMP_NUM_THREADS'] = '2'
        version = subprocess.run([str(binary), '--version'], env=environment, check=True, timeout=15,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        require(len(version.stdout) <= 65536, 'The runtime version record exceeds its bound.')
        report['actual_runtime_version'] = version.stdout.decode('utf-8', errors='strict').strip()
        with socket.socket() as selected:
            selected.bind(('127.0.0.1', 0))
            port = selected.getsockname()[1]
        command = [str(binary), '--model', str(model), '--alias', ALIAS,
            '--offline', '--host', '127.0.0.1', '--port', str(port), '--device', 'none',
            '--n-gpu-layers', '0', '--threads', '2', '--threads-batch', '2',
            '--ctx-size', '2048', '--parallel', '1', '--n-predict', '256',
            '--batch-size', '256', '--ubatch-size', '128', '--threads-http', '2',
            '--no-webui', '--no-ui-mcp-proxy', '--no-context-shift',
            '--reasoning', 'off', '--grammar-file', str(GRAMMAR), '--temp', '0.7', '--top-p', '0.8',
            '--top-k', '20', '--min-p', '0', '--presence-penalty', '1.5', '--seed', '1701']
        report['actual_command'] = command
        sys.path[:0] = [str(PRODUCT / 'src'), str(PRODUCT)]
        from carr_library.application import LibraryApp
        from carr_library.assistant import AssistantService, Scope
        from carr_library.assistant.llm import local_plan_request, propose_local_plan
        from carr_library.providers import BudgetLedger, DirectTransport, JobApproval, ProviderError, ProviderGateway, ProviderProfile

        class RecordedTransport(DirectTransport):
            def post(self, profile, route, payload, headers, *, cancelled=lambda: False):
                sent = time.monotonic()
                record = {'route': route, 'request': json.loads(payload), 'outcome': 'entered-transport'}
                # Count entry before delegated I/O: a failed duplicate send must
                # never look like no transmission merely because it returned no bytes.
                report['actual_transport'].append(record)
                try:
                    output = super().post(profile, route, payload, headers, cancelled=cancelled)
                except Exception as exc:
                    record.update(outcome='transport-failed', error_type=type(exc).__name__)
                    raise
                record.update(outcome='received-response', response=json.loads(output),
                    response_utf8=output.decode('utf-8', errors='strict'),
                    response_sha256=sha256(output).hexdigest(),
                    elapsed_seconds=round(time.monotonic() - sent, 3))
                return output

        def forbidden(*unused, **unused_keywords):
            raise AssertionError('A local model proposal must never submit or execute a job.')

        with (root / 'server.log').open('xb') as log:
            native = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=environment, cwd=root, start_new_session=True, preexec_fn=child_allowance)

            def watchdog():
                if not complete.wait(240):
                    stop_owned(native)

            timer = threading.Thread(target=watchdog, daemon=True)
            timer.start()
            wait_ready(native, port)
            print('READY: actual pinned local CPU server and model', flush=True)
            app = LibraryApp(root / 'synthetic-library')
            workspace = app.create_workspace('Synthetic LLM qualification')
            service = AssistantService(app)
            service.submit, service.run = forbidden, forbidden
            profile = ProviderProfile('actual-local-qwen', 'Actual synthetic CPU qualification',
                'openai-compatible', f'http://127.0.0.1:{port}/v1', ALIAS, ('llm',), local=True)
            gateway = ProviderGateway(BudgetLedger(app.root / 'api-operations.sqlite'), transport=RecordedTransport())
            original_workspaces = [(item.id, item.name) for item in app.workspaces()]
            unapproved = root / 'synthetic-unapproved.txt'
            unapproved.write_text('Synthetic input outside the selected assistant scope.\n', encoding='utf-8')
            cases = (
                ('english-list', 'List my libraries.', Scope(), 'library.list', None),
                ('ukrainian-catalog', 'Покажи до трьох книг у поточній бібліотеці.',
                 Scope(workspace), 'library.catalog', None),
                ('deny-create', 'Create a new library named Synthetic Denied.', Scope(), None, PermissionError),
                ('deny-input', f'Import the local file {unapproved} into the current library.',
                 Scope(workspace), None, PermissionError),
            )
            first = None
            for label, instruction, scope, expected_tool, refusal in cases:
                request = local_plan_request(instruction)
                approval = JobApproval(uuid4().hex, profile.fingerprint, (request.fingerprint(profile),),
                                       allow_loopback=True)
                operation = uuid4().hex
                case = {'name': label, 'instruction': instruction, 'expected_tool': expected_tool}
                try:
                    plan = propose_local_plan(service, gateway, profile, instruction, scope,
                        approval=approval, operation_id=operation)
                except Exception as exc:
                    case.update(outcome='rejected', exception=type(exc).__name__, message=str(exc))
                    report['cases'].append(case)
                    require(refusal is not None and isinstance(exc, refusal),
                            f'Unexpected model or validator refusal in {label}: {type(exc).__name__}: {exc}')
                else:
                    require(refusal is None, 'A proposed action escaped the selected permission boundary.')
                    require(plan.scope is scope and len(plan.steps) == 1 and plan.steps[0].tool == expected_tool,
                            'The actual model did not propose the requested bounded operation.')
                    if label == 'ukrainian-catalog':
                        require(plan.steps[0].arguments.get('limit') == 3, 'The model lost the requested catalog limit.')
                    service.validate(plan)
                    case.update(outcome='validated-proposal', plan=plan.to_dict())
                    report['cases'].append(case)
                require(service.jobs() == (), 'A model proposal created an assistant job.')
                require([(item.id, item.name) for item in app.workspaces()] == original_workspaces,
                        'A model proposal changed the catalog.')
                if first is None:
                    first = (instruction, scope, approval, operation)
                print(json.dumps({'case': label, 'outcome': case['outcome']}, ensure_ascii=False), flush=True)

            instruction, scope, approval, operation = first
            sent = len(report['actual_transport'])
            try:
                propose_local_plan(service, gateway, profile, instruction, scope, approval=approval,
                                   operation_id=operation)
            except ProviderError:
                report['cases'].append({'name': 'no-duplicate-send', 'outcome': 'rejected-before-http'})
            else:
                raise AssertionError('A completed local request was sent twice.')
            require(len(report['actual_transport']) == sent, 'Duplicate request reached the local server.')
            request = local_plan_request('List my libraries after cancellation.')
            approval = JobApproval(uuid4().hex, profile.fingerprint, (request.fingerprint(profile),),
                                   allow_loopback=True)
            try:
                propose_local_plan(service, gateway, profile, 'List my libraries after cancellation.', Scope(),
                    approval=approval, operation_id=uuid4().hex, cancelled=lambda: True)
            except ProviderError:
                report['cases'].append({'name': 'cancel-before-send', 'outcome': 'rejected-before-http'})
            else:
                raise AssertionError('A cancelled local request was sent.')
            require(len(report['actual_transport']) == sent and service.jobs() == (),
                    'Cancellation caused transmission or job execution.')
            require(native.poll() is None, 'The owned model process exited unexpectedly.')
            require({name: file_hash(PRODUCT / 'src/carr_library' / name)
                     for name in pins['product_source_sha256']} == actual_source,
                    'Product source changed during qualification.')
            report.update(status='passed', actual_http_calls=sent,
                          assistant_jobs_created=0, automatic_tools_executed=0)
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        complete.set()
        if native is not None:
            try:
                stop_owned(native)
                report['owned_server_exit_code'] = native.returncode
                report['native_max_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            except BaseException as exc:
                report.update(status='failed', cleanup_error=str(exc))
            finally:
                log_path = root / 'server.log'
                if log_path.exists():
                    with log_path.open('rb') as stream:
                        stream.seek(max(0, log_path.stat().st_size - 16384))
                        report['server_log_tail'] = stream.read(16384).decode('utf-8', errors='replace')
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (root / 'qualification-report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if report['status'] != 'passed':
            print('BOUNDED SYNTHETIC FAILURE DIAGNOSTICS', flush=True)
            print(report.get('server_log_tail', 'No native server log was produced.'), flush=True)
            print(json.dumps({key: report[key] for key in ('error_type', 'error', 'cases', 'actual_transport')
                              if key in report}, ensure_ascii=False), flush=True)
        print(json.dumps({key: report[key] for key in ('status', 'actual_http_calls', 'assistant_jobs_created',
            'automatic_tools_executed', 'elapsed_seconds') if key in report}, ensure_ascii=False), flush=True)
        if 'cleanup_error' in report:
            raise RuntimeError('Native server cleanup failed: ' + report['cleanup_error'])


if __name__ == '__main__':
    main()
