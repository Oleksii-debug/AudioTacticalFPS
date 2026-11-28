"""Real current DOCX workers, retained sources, approvals and new-file exports.

Only the existing provider transport is synthetic. ZIP/package parsing, isolated
owned children, resource admission and atomic binary publication are real.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE

from carr_library.assistant import translation_layout as layout_module
from carr_library.assistant import translation_layout_worker as worker
from carr_library.assistant.models import canonical
from carr_library.assistant.translation_layout import (
    capture_layout, review_layout, prepare_layout, export_layout)
from carr_library.audio import process
from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners
from carr_library.audio.models import AudioCancelled
from carr_library.durable_state import encode, read_json
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceLimits, ResourceUnavailable
from carr_library.research.export_file import capture_export_destination

_spec = importlib.util.spec_from_file_location('layout_current_gateway',
    Path(__file__).with_name('test_library_translation.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
setup = _base.setup


def hyperlink(paragraph, value):
    relation = paragraph.part.relate_to('https://example.invalid/retained-reference',
                                        RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), relation)
    run, node = OxmlElement('w:r'), OxmlElement('w:t')
    node.text = value
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


def docx_bytes(*, paragraphs=1, rich=True):
    document = Document()
    for number in range(paragraphs):
        paragraph = document.add_paragraph(style='Heading 1' if number == 0 else 'Normal')
        paragraph.add_run('Original Ukrainian Ω ' + str(number) + ' ').bold = True
        paragraph.add_run('second styled span.').italic = True
    if rich:
        table = document.add_table(rows=2, cols=2)
        table.style = 'Table Grid'
        for row_index, row in enumerate(table.rows):
            for column, cell in enumerate(row.cells):
                cell.text = 'Cell ' + str(row_index) + ':' + str(column)
        link = document.add_paragraph()
        hyperlink(link, 'Complete linked paragraph.')
        document.add_paragraph('')
        document.sections[0].header.paragraphs[0].text = 'Untranslated header stays exact.'
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def replace_members(data, changes):
    result = BytesIO()
    with ZipFile(BytesIO(data)) as original, ZipFile(result, 'w') as output:
        for info in original.infolist():
            output.writestr(info, changes.pop(info.filename, original.read(info)))
        for name, raw in changes.items():
            output.writestr(name, raw)
    return result.getvalue()


@pytest.fixture
def bound(tmp_path, setup):
    source = tmp_path / 'Selected Ω.docx'
    raw = docx_bytes()
    source.write_bytes(raw)
    result = setup.app._import_file(setup.workspace, source, title='Retained DOCX Ω',
        authors=('Synthetic original author',), work_id='docx-layout-work', edition_id='docx-layout-edition')
    assert result['state'] == 'complete' and result['verified'] is False
    yield SimpleNamespace(**vars(setup), edition='docx-layout-edition', source=source, raw=raw)
    setup.app.close_search()


def captured(case):
    return capture_layout(case.app, case.workspace, case.edition, kind='docx')


def prepared(case, layout):
    review = review_layout(case.service, case.profile, layout, 'en', 'uk', '1')
    return prepare_layout(case.service, case.profile, layout, 'en', 'uk', '1',
                          approved_review=review['review_sha256'])


def completed(case):
    layout = captured(case)
    identity = prepared(case, layout)
    assert case.service.resume(identity)['status'] == 'complete'
    return layout, identity


def test_actual_worker_maps_exact_retained_body_and_cells_under_the_real_lease(bound, monkeypatch):
    original, invocations = layout_module.run_process, []
    def observed(args, **kwargs):
        invocations.append((tuple(args), kwargs))
        assert args[1:5] == ['-I', '-B', '-X', 'utf8']
        assert Path(args[-2]).resolve() == Path(worker.__file__).resolve()
        assert kwargs['owned_tree'] is True and kwargs['allow_failure'] is True
        assert kwargs['env']['PYTHONDONTWRITEBYTECODE'] == '1'
        lease, = ResourceBroker.for_app(bound.app.root).snapshot()['leases']
        assert lease['request']['memory_bytes'] == 3 * 1024**3
        assert kwargs['cwd'].is_dir()
        return original(args, **kwargs)
    monkeypatch.setattr(layout_module, 'run_process', observed)
    layout = captured(bound)
    assert len(invocations) == 1
    assert layout.mapping['selected'] == 6 and len(layout.mapping['records']) == 7
    assert [row['locator'] for row in layout.mapping['records']][:5] == [
        [1], [2, 1, 1, 1], [2, 1, 2, 1], [2, 2, 1, 1], [2, 2, 2, 1]]
    source, = layout.binding['sources']
    assert source['selection'] == 'document' and len(source['anchor']['positions']) == 7
    assert layout.source_text == '\n\n'.join(row['text'] for row in layout.mapping['records'])
    assert source['anchor']['quotation_authority'] is False
    assert all(not row['text_verified_at_capture'] for row in source['anchor']['positions'])
    assert bound.source.read_bytes() == bound.raw
    assert not ResourceBroker.for_app(bound.app.root).snapshot()['leases']
    assert not list((bound.app.root / 'runtime').glob('.translation-layout-*'))


def test_review_binds_one_job_to_exact_structure_without_provider_or_credentials(bound):
    layout = captured(bound)
    calls, vault = list(bound.calls), list(bound.vault_calls)
    review = review_layout(bound.service, bound.profile, layout, 'en', 'uk', '1')
    assert review['provider_calls_started'] is False and review['layout']['mapping_sha256'] == layout.mapping_sha256
    assert 'first textual run' in review['layout']['notice']
    identity = prepare_layout(bound.service, bound.profile, layout, 'en', 'uk', '1',
                              approved_review=review['review_sha256'])
    job = bound.service.get(identity)
    assert job['kind'] == 'translation/1' and job['source_partition'] == layout.source_partition
    assert bound.service.jobs.identities() == (identity,)
    assert len(job['source_binding']['sources']) == 1
    assert bound.calls == calls and bound.vault_calls == vault
    with pytest.raises(PermissionError, match='exact layout'):
        prepare_layout(bound.service, bound.profile, layout, 'auto', 'uk', '1',
                       approved_review=review['review_sha256'])
    assert bound.service.jobs.identities() == (identity,)


def test_real_completed_docx_preserves_tables_styles_links_and_every_other_zip_member(bound, tmp_path):
    layout, identity = completed(bound)
    calls, vault = list(bound.calls), list(bound.vault_calls)
    job_before = bound.service.jobs.path(identity).read_bytes()
    target = tmp_path / 'Translated Ω.docx'
    approval = capture_export_destination(target, private_root=bound.app.root)
    result = export_layout(bound.service, identity, target, approval=approval)
    assert result['status'] == 'complete' and result['quotation_authority'] is False
    assert result['source_sha256'] == sha256(bound.raw).hexdigest()
    assert result['output_sha256'] == sha256(target.read_bytes()).hexdigest()
    assert result['output_bytes'] == target.stat().st_size
    assert result['changed_members'] == ['word/document.xml'] and result['job_id'] == identity
    original, exported = Document(BytesIO(bound.raw)), Document(target)
    assert exported.paragraphs[0].style.style_id == original.paragraphs[0].style.style_id
    assert exported.paragraphs[0].runs[0].bold is True
    assert exported.paragraphs[0].runs[1].italic is True and exported.paragraphs[0].runs[1].text == ''
    assert exported.paragraphs[0].text.startswith('Переклад: Original Ukrainian Ω')
    assert exported.tables[0].style.style_id == original.tables[0].style.style_id
    assert len(exported.tables[0].rows) == 2 and len(exported.tables[0].columns) == 2
    assert all(cell.text.startswith('Переклад: Cell') for row in exported.tables[0].rows for cell in row.cells)
    assert exported.paragraphs[-1].text == ''
    assert exported.paragraphs[1].hyperlinks[0].address == original.paragraphs[1].hyperlinks[0].address
    with ZipFile(BytesIO(bound.raw)) as before, ZipFile(target) as after:
        assert before.namelist() == after.namelist()
        preserved = [name for name in before.namelist() if name != 'word/document.xml']
        assert result['preserved_member_count'] == len(preserved)
        for name in preserved:
            assert before.read(name) == after.read(name), name
    assert bound.source.read_bytes() == bound.raw
    assert bound.app.assets.resolve(layout.mapping['source_sha256']).read_bytes() == bound.raw
    assert bound.calls == calls and bound.vault_calls == vault
    assert bound.service.jobs.path(identity).read_bytes() == job_before
    assert not ResourceBroker.for_app(bound.app.root).snapshot()['leases']


@pytest.mark.parametrize('construct', ['field', 'footnote', 'endnote', 'drawing', 'tracked-change',
                                       'mixed-hyperlink', 'nested-table', 'signature', 'macro'])
def test_unsupported_source_shapes_are_refused_by_unchanged_canonical_admission(construct):
    document = Document()
    paragraph = document.add_paragraph('Explicit original.')
    if construct in ('field', 'footnote', 'endnote', 'drawing'):
        tag = {'field': 'w:fldChar', 'footnote': 'w:footnoteReference',
               'endnote': 'w:endnoteReference', 'drawing': 'w:drawing'}[construct]
        paragraph.runs[0]._r.append(OxmlElement(tag))
    elif construct == 'tracked-change':
        paragraph._p.append(OxmlElement('w:ins'))
    elif construct == 'mixed-hyperlink':
        hyperlink(paragraph, 'Separate linked span.')
    elif construct == 'nested-table':
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = 'Nested original.'
    raw = BytesIO()
    document.save(raw)
    data = raw.getvalue()
    if construct == 'signature':
        data = replace_members(data, {'_xmlsignatures/sig1.xml': b'<signature/>'})
    if construct == 'macro':
        data = replace_members(data, {'word/vbaProject.bin': b'NOT EXECUTABLE SYNTHETIC'})
    before = sha256(data).hexdigest()
    with pytest.raises(ValueError):
        worker.describe(data)
    assert sha256(data).hexdigest() == before


def test_mixed_hyperlink_is_refused_by_actual_worker_before_jobs_or_requests(bound):
    document = Document(BytesIO(bound.raw))
    hyperlink(document.paragraphs[0], 'Mixed reference.')
    raw = BytesIO()
    document.save(raw)
    path = bound.source.with_name('mixed.docx')
    path.write_bytes(raw.getvalue())
    bound.app._import_file(bound.workspace, path, title='Mixed source',
        work_id='mixed-work', edition_id='mixed-edition')
    calls = list(bound.calls)
    with pytest.raises(ValueError, match='Mixed hyperlink'):
        capture_layout(bound.app, bound.workspace, 'mixed-edition', kind='docx')
    assert not bound.service.jobs.identities() and bound.calls == calls


def test_same_size_modified_worker_request_is_refused_by_actual_snapshot(bound, monkeypatch):
    actual, reached = layout_module.run_process, []
    def changed(args, **kwargs):
        path = Path(args[-4])
        raw = path.read_bytes()
        value = read_json(path)
        value['source_sha256'] = ('0' if value['source_sha256'][0] != '0' else '1') + value['source_sha256'][1:]
        replacement = encode(value)
        assert len(replacement) == len(raw)
        path.write_bytes(replacement)
        reached.append(True)
        return actual(args, **kwargs)
    monkeypatch.setattr(layout_module, 'run_process', changed)
    with pytest.raises(ValueError, match='request changed'):
        captured(bound)
    assert reached == [True] and not bound.service.jobs.identities()


def test_wrong_actual_worker_origin_is_refused_before_parsing(bound, monkeypatch):
    actual = layout_module._command
    def changed(*args, **kwargs):
        command, environment = actual(*args, **kwargs)
        command[-2] = str(Path(worker.__file__).with_name('not-the-imported-worker.py'))
        return command, environment
    monkeypatch.setattr(layout_module, '_command', changed)
    with pytest.raises(ValueError, match='origin differs'):
        captured(bound)
    assert not bound.service.jobs.identities()


def test_forged_mapping_cannot_reuse_original_review(bound):
    layout = captured(bound)
    mapping = layout.mapping
    mapping['records'][0]['locator'] = [2]
    changed = replace(layout, mapping_json=canonical(mapping))
    calls = list(bound.calls)
    with pytest.raises(ValueError, match='layout changed|mapping'):
        review_layout(bound.service, bound.profile, changed, 'en', 'uk', '1')
    assert not bound.service.jobs.identities() and bound.calls == calls


def test_partial_job_cannot_publish_and_resumes_only_unaccepted_requests(bound, tmp_path):
    layout = captured(bound)
    identity = prepared(bound, layout)
    stop = []
    result = bound.service.resume(identity, cancelled=lambda: bool(stop),
        progress=lambda *args: stop.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1
    accepted = deepcopy(bound.service.get(identity)['receipts'])
    target = tmp_path / 'incomplete.docx'
    with pytest.raises(ValueError, match='Finish every'):
        export_layout(bound.service, identity, target)
    assert not target.exists()
    calls = len(bound.calls)
    assert bound.service.resume(identity)['status'] == 'complete'
    assert len(bound.calls) - calls == layout.mapping['selected'] - 1
    assert bound.service.get(identity)['receipts'][:1] == accepted
    assert export_layout(bound.service, identity, target)['status'] == 'complete'


@pytest.mark.parametrize('fault', ['source', 'same-byte-competitor', 'parent', 'private-target'])
def test_final_publication_keeps_exact_source_destination_and_new_file_authority(
        bound, tmp_path, monkeypatch, fault):
    layout, identity = completed(bound)
    parent = tmp_path / 'external'
    parent.mkdir()
    target = parent / 'translated.docx'
    if fault == 'private-target':
        target = bound.app.root / 'private.docx'
        with pytest.raises(ValueError):
            export_layout(bound.service, identity, target)
        assert not target.exists()
        return
    approval = capture_export_destination(target, private_root=bound.app.root)
    actual, competitor = layout_module.write_binary_export, []
    retained = bound.app.assets.resolve(layout.mapping['source_sha256'])
    def boundary(path, data, **kwargs):
        if fault == 'source':
            retained.write_bytes(bound.raw + b'changed')
        elif fault == 'same-byte-competitor':
            Path(path).write_bytes(data)
            competitor.append(data)
        else:
            parent.rename(tmp_path / 'replaced-external')
            parent.mkdir()
        return actual(path, data, **kwargs)
    monkeypatch.setattr(layout_module, 'write_binary_export', boundary)
    try:
        with pytest.raises((ValueError, FileExistsError)):
            export_layout(bound.service, identity, target, approval=approval)
        if competitor:
            assert target.read_bytes() == competitor[0]
        else:
            assert not target.exists()
        assert bound.source.read_bytes() == bound.raw
    finally:
        if fault == 'source':
            retained.write_bytes(bound.raw)


def test_stop_after_actual_publication_preserves_completed_new_file_receipt(bound, tmp_path, monkeypatch):
    _, identity = completed(bound)
    target, stopped = tmp_path / 'committed.docx', []
    actual = layout_module.write_binary_export
    def published(*args, **kwargs):
        receipt = actual(*args, **kwargs)
        stopped.append(True)
        return receipt
    monkeypatch.setattr(layout_module, 'write_binary_export', published)
    result = export_layout(bound.service, identity, target, cancelled=lambda: bool(stopped))
    assert stopped == [True] and result['status'] == 'complete'
    assert result['output_sha256'] == sha256(target.read_bytes()).hexdigest()


def test_declared_resource_refusal_precedes_parser_and_provider(bound, monkeypatch):
    broker = ResourceBroker.for_app(bound.app.root)
    broker.configure(ResourceLimits(memory_bytes=1024**3))
    calls = list(bound.calls)
    monkeypatch.setattr(layout_module, 'run_process',
                        lambda *args, **kwargs: pytest.fail('Refused admission launched a parser.'))
    with pytest.raises(ResourceUnavailable, match='memory|RAM|capacity'):
        capture_layout(bound.app, bound.workspace, bound.edition, kind='docx', broker=broker)
    assert bound.calls == calls and not bound.service.jobs.identities()
    assert not broker.snapshot()['leases']


def test_actual_worker_cancel_reaps_child_before_lease_and_stage_release(bound, tmp_path, monkeypatch):
    marker = tmp_path / 'worker-ready.json'
    actual_command, actual_start = layout_module._command, process._start_process
    commands, children = [], []
    def blocked(*args, **kwargs):
        command, environment = actual_command(*args, **kwargs)
        index = command.index('-c') + 1
        command[index] = (
            'import os,time; from pathlib import Path; p=Path(' + repr(str(marker)) + '); '
            't=p.with_suffix(".pending"); t.write_bytes(b"owned"); os.replace(t,p); '
            'time.sleep(60); ' + command[index])
        commands.append(tuple(command))
        return command, environment
    def start(args, **kwargs):
        child = actual_start(args, **kwargs)
        if tuple(args) in commands:
            children.append(child)
        return child
    monkeypatch.setattr(layout_module, '_command', blocked)
    monkeypatch.setattr(process, '_start_process', start)
    with pytest.raises((AudioCancelled, InterruptedError)):
        capture_layout(bound.app, bound.workspace, bound.edition, kind='docx', cancelled=marker.exists)
    assert marker.read_bytes() == b'owned' and len(children) == 1
    assert children[0].returncode is not None
    assert not ResourceBroker.for_app(bound.app.root).snapshot()['leases']
    assert not list((bound.app.root / 'runtime').glob('.translation-layout-*'))


@pytest.mark.parametrize('masked', [False, True], ids=['direct', 'causal'])
@pytest.mark.parametrize('first', [0, 1], ids=['first-child', 'second-child'])
def test_two_actual_pending_families_retain_one_stage_and_lease_until_both_quiet(
        bound, monkeypatch, masked, first):
    actual_stop, children, allowed, stages = process._stop_owned, [], set(), []
    owners = ()
    def stop(child):
        if child in children and child not in allowed:
            return False
        return actual_stop(child)
    monkeypatch.setattr(process, '_stop_owned', stop)
    def running(args, **kwargs):
        stage = Path(kwargs['cwd'])
        stages.append(stage)
        try:
            with ExitStack() as native:
                markers = []
                for index in range(2):
                    marker = stage / ('pending-child-' + str(index))
                    child = native.enter_context(process.owned_process(
                        [sys.executable, '-I', '-S', '-c',
                         'from pathlib import Path; import sys,time; '
                         'Path(sys.argv[1]).write_bytes(b"owned"); time.sleep(60)', str(marker)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    children.append(child)
                    markers.append(marker)
                deadline = time.monotonic() + 10
                while not all(path.exists() for path in markers) and time.monotonic() < deadline:
                    time.sleep(.01)
                assert all(path.exists() for path in markers)
                assert all(child.poll() is None for child in children)
                raise RuntimeError('Stop both actual fixture families before publishing.')
        except AudioCleanupPending as error:
            if masked:
                raise InterruptedError('Cancellation retains its actual pending owners.') from error
            raise
    monkeypatch.setattr(layout_module, 'run_process', running)
    try:
        with pytest.raises((AudioCleanupPending, InterruptedError)) as failure:
            captured(bound)
        owners = _pending_owners(failure.value)
        assert len(owners) == 2 and all(not owner.quiescent for owner in owners)
        stage, = stages
        assert stage.exists() and len(ResourceBroker.for_app(bound.app.root).snapshot()['leases']) == 1
        assert not bound.service.jobs.identities()
        finished, acknowledgments, acknowledgment_lock = threading.Event(), set(), threading.Lock()
        def acknowledged(identity):
            with acknowledgment_lock:
                acknowledgments.add(identity)
                if len(acknowledgments) == len(owners):
                    finished.set()
        for owner in owners:
            owner.add_quiescent_callback(lambda identity=id(owner): acknowledged(identity))
        allowed.add(children[first])
        deadline = time.monotonic() + 10
        while sum(owner.quiescent for owner in owners) != 1 and time.monotonic() < deadline:
            for owner in owners:
                owner.close()
            time.sleep(.01)
        assert sum(owner.quiescent for owner in owners) == 1
        assert stage.exists() and len(ResourceBroker.for_app(bound.app.root).snapshot()['leases']) == 1
        allowed.add(children[1-first])
        for owner in owners:
            owner.close()
        assert finished.wait(10)
        assert all(owner.quiescent for owner in owners) and not stage.exists()
        assert not ResourceBroker.for_app(bound.app.root).snapshot()['leases']
        assert all(child.returncode is not None for child in children)
    finally:
        allowed.update(children)
        for owner in owners:
            owner.close()
        monkeypatch.setattr(process, '_stop_owned', actual_stop)
        for child in children:
            actual_stop(child)


def test_explicit_cli_review_submit_and_new_file_export_share_the_same_saved_job(bound, tmp_path):
    from carr_library import translation_cli
    common = ['--data-dir', str(bound.app.root), '--workspace', bound.workspace]
    selected = ['--profile', bound.profile.id, '--edition', bound.edition,
                '--source', 'en', '--target', 'uk', '--budget', '1', '--preserve-format', 'docx']
    calls = list(bound.calls)
    review = translation_cli.execute(translation_cli.parser().parse_args(common + ['preview'] + selected))
    assert review['approval_sha256'] == review['review_sha256'] and bound.calls == calls
    assert review['layout']['format'] == 'docx' and review['provider_calls_started'] is False
    with pytest.raises(PermissionError, match='exact current layout'):
        translation_cli.execute(translation_cli.parser().parse_args(
            common + ['submit'] + selected + ['--approve-preview', review['approval_sha256']]))
    assert not bound.service.jobs.identities() and bound.calls == calls
    result = translation_cli.execute(translation_cli.parser().parse_args(
        common + ['submit'] + selected + ['--approve-preview', review['approval_sha256'], '--allow-remote']))
    assert result['status'] == 'complete'
    job = bound.service.get(result['id'])
    summary = translation_cli.job_summary(job)
    assert summary['source_partition'] == job['source_partition']
    modified = deepcopy(job)
    modified['source_partition']['mapping_sha256'] = '0' * 64
    assert translation_cli.job_summary(modified)['resume_approval_sha256'] != summary['resume_approval_sha256']
    target = tmp_path / 'CLI translated.docx'
    calls = list(bound.calls)
    receipt = translation_cli.execute(translation_cli.parser().parse_args(
        common + ['export-format', '--job', result['id'], '--output', str(target)]))
    assert receipt['status'] == 'complete' and receipt['job_id'] == result['id']
    assert receipt['output_sha256'] == sha256(target.read_bytes()).hexdigest()
    assert bound.calls == calls


@pytest.mark.parametrize('arguments', [
    ['--first', '1', '--last', '1'],
    ['--edition', 'another-edition'],
])
def test_cli_format_mode_refuses_partial_or_multiple_documents_before_parsing(bound, monkeypatch, arguments):
    from carr_library import translation_cli
    monkeypatch.setattr(translation_cli, 'capture_layout',
                        lambda *args, **kwargs: pytest.fail('Ambiguous scope reached a format worker.'))
    args = translation_cli.parser().parse_args([
        '--data-dir', str(bound.app.root), '--workspace', bound.workspace, 'preview',
        '--profile', bound.profile.id, '--edition', bound.edition,
        '--target', 'uk', '--budget', '1', '--preserve-format', 'docx'] + arguments)
    calls = list(bound.calls)
    with pytest.raises(ValueError, match='one whole'):
        translation_cli.execute(args)
    assert bound.calls == calls and not bound.service.jobs.identities()


@pytest.mark.parametrize('phase', ['operation', 'runtime-close'])
@pytest.mark.parametrize('masked', [False, True], ids=['direct', 'causal'])
@pytest.mark.parametrize('first', [0, 1], ids=['first-family', 'second-family'])
def test_portable_cli_process_keeps_actual_pending_families_and_stage_until_both_quiet(
        tmp_path, phase, masked, first):
    import carr_library
    package_root = str(Path(carr_library.__file__).resolve().parent.parent)
    marker = tmp_path / 'pending.json'
    code = r"""
import json, os, subprocess, sys, tempfile, time
from contextlib import ExitStack
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from carr_library import translation_cli, native_runtime
from carr_library.audio import process
from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners, retained_context
root, phase, masked = Path(sys.argv[2]), sys.argv[3], sys.argv[4] == '1'
children, failures = [], []
actual_stop = process._stop_owned

def publish(path, raw):
    temporary = path.with_suffix('.pending')
    temporary.write_bytes(raw)
    os.replace(temporary, path)

def stop(child):
    index = next((index for index, member in enumerate(children) if member is child), None)
    if index is not None and not (root / ('release-' + str(index))).exists():
        return False
    confirmed = actual_stop(child)
    if confirmed is True and index is not None:
        publish(root / ('quiet-' + str(index)), b'confirmed')
    return confirmed
process._stop_owned = stop

def pending_families():
    try:
        with retained_context(tempfile.TemporaryDirectory(prefix='actual-cli-stage-', dir=root)) as temporary:
            stage = Path(temporary)
            (stage / 'retained-original').write_bytes(b'exact retained source')
            with ExitStack() as owned:
                ready = []
                for index in range(2):
                    marker = stage / ('child-' + str(index))
                    child = owned.enter_context(process.owned_process(
                        [sys.executable, '-I', '-S', '-c',
                         'import os,sys,time; from pathlib import Path; p=Path(sys.argv[1]); '
                         't=p.with_suffix(".pending"); t.write_bytes(b"running"); os.replace(t,p); time.sleep(60)',
                         str(marker)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL))
                    children.append(child)
                    ready.append(marker)
                deadline = time.monotonic() + 10
                while not all(path.exists() for path in ready) and time.monotonic() < deadline:
                    time.sleep(.01)
                assert all(path.read_bytes() == b'running' for path in ready)
                raise RuntimeError('The exact fixture operation requires native cleanup.')
    except AudioCleanupPending as error:
        assert len(_pending_owners(error)) == 2
        publish(root / 'pending.json', json.dumps({'stage': str(stage), 'owners': 2}).encode('utf-8'))
        if masked:
            try:
                raise InterruptedError('Cancellation retains the same native families.') from error
            except InterruptedError as wrapped:
                failures.append(wrapped)
                raise
        failures.append(error)
        raise

class Handle:
    def close(self):
        if phase == 'runtime-close':
            pending_families()
        assert len(children) == 2 and all(child.returncode is not None for child in children)
        publish(root / 'runtime-closed', b'closed after native quiescence')

native_runtime.activate_bundled_runtime = lambda: Handle()
translation_cli.execute = lambda args: pending_families() if phase == 'operation' else {'status': 'complete'}
try:
    result = translation_cli.main(['profiles'])
except (AudioCleanupPending, InterruptedError) as error:
    assert phase == 'runtime-close' and error is failures[-1]
    owners = _pending_owners(error)
    assert len(owners) == 2
    assert all(owner.quiescent and owner.notifications_complete_event.is_set() for owner in owners)
    result = 72 if masked else 71
publish(root / 'returned.json', json.dumps({'result': result}).encode('utf-8'))
raise SystemExit(result)
"""
    releases = tuple(tmp_path / ('release-' + str(index)) for index in range(2))
    with process.owned_process([sys.executable, '-I', '-B', '-c', code,
            package_root, str(tmp_path), phase, '1' if masked else '0'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as cli_child:
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and cli_child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists(), 'The portable CLI never reached its actual pending native families.'
            pending = json.loads(marker.read_bytes())
            stage = Path(pending['stage'])
            assert pending['owners'] == 2 and stage.is_dir()
            assert cli_child.poll() is None and not (tmp_path / 'returned.json').exists()
            releases[first].write_bytes(b'release')
            deadline = time.monotonic() + 10
            while not (tmp_path / ('quiet-' + str(first))).exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert (tmp_path / ('quiet-' + str(first))).read_bytes() == b'confirmed'
            assert cli_child.poll() is None and stage.is_dir()
            assert not (tmp_path / 'runtime-closed').exists()
            assert not (tmp_path / 'returned.json').exists()
            releases[1-first].write_bytes(b'release')
            expected = (130 if masked else 1) if phase == 'operation' else (72 if masked else 71)
            assert cli_child.wait(timeout=10) == expected
            assert json.loads((tmp_path / 'returned.json').read_bytes()) == {'result': expected}
            assert not stage.exists()
            assert (tmp_path / 'runtime-closed').exists() == (phase == 'operation')
        finally:
            for release in releases:
                release.write_bytes(b'release')


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
def test_cli_defers_shutdown_interrupt_until_same_owner_is_quiet_and_runtime_closed(
        tmp_path, monkeypatch, signal_type):
    from carr_library import translation_cli, native_runtime
    from carr_library.audio.cleanup import CleanupOwner
    signal, attempts, closed = signal_type('interrupt during native cleanup'), [], []
    def attempt():
        attempts.append(True)
        if len(attempts) == 1:
            raise signal
        return len(attempts) >= 3
    owner = CleanupOwner(attempt)
    pending = AudioCleanupPending('Owned family still pending.', cleanup_owner=owner)
    def execute(args):
        raise pending
    class Handle:
        def close(self):
            assert owner.quiescent
            closed.append(True)
    monkeypatch.setattr(translation_cli, 'execute', execute)
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', lambda: Handle())
    with pytest.raises(signal_type) as observed:
        translation_cli.main(['--data-dir', str(tmp_path / 'library'), 'profiles'])
    assert observed.value is signal
    assert owner.quiescent and len(attempts) == 3 and closed == [True]
