"""Acceptance-tool boundary tests; these do not replace actual checker runs."""
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import sys
from types import SimpleNamespace

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[2] / 'scripts' / name
    spec = importlib.util.spec_from_file_location('boundary_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_epub_tool_refuses_an_unpinned_archive_before_extracting(tmp_path):
    checker = load_script('epubcheck_acceptance.py')
    archive, tools = tmp_path / 'unknown.zip', tmp_path / 'tools'
    archive.write_bytes(b'not the pinned validator')
    tools.mkdir()
    pin = json.loads((Path(__file__).resolve().parents[2] / 'packaging' / 'epubcheck.json').read_text())
    with pytest.raises(ValueError, match='exact reviewed'):
        checker.unpack_validator(archive, tools, pin)
    assert not list(tools.iterdir())


def test_daisy_schema_boundary_refuses_changed_local_bytes_without_loading_lxml(tmp_path):
    checker = load_script('daisy_schema_acceptance.py')
    for name in checker.SCHEMA_NAMES:
        (tmp_path / name).write_bytes(b'not the independently pinned schema')
    with pytest.raises(ValueError, match='failed its pin'):
        checker.schema_paths(tmp_path)


def test_daisy_resolver_only_returns_explicit_local_aliases(tmp_path):
    checker = load_script('daisy_schema_acceptance.py')
    class Resolver:
        def resolve_string(self, data, context, *, base_url):
            return data, base_url
    paths = {'SMIL10.dtd': tmp_path / 'SMIL10.dtd', 'xhtml-lat1.ent': tmp_path / 'xhtml-lat1.ent'}
    for name, path in paths.items():
        path.write_bytes(name.encode())
    resolve = checker.local_resolver(SimpleNamespace(Resolver=Resolver), paths)().resolve
    assert resolve('http://www.w3.org/TR/REC-SMIL/SMIL10.dtd', None, None) == (
        b'SMIL10.dtd', 'http://www.w3.org/TR/REC-SMIL/SMIL10.dtd')
    assert resolve(paths['xhtml-lat1.ent'].as_uri(), None, None) == (
        b'xhtml-lat1.ent', 'http://www.w3.org/TR/xhtml1/DTD/xhtml-lat1.ent')
    for request in ('https://unapproved.example/SMIL10.dtd', '../SMIL10.dtd',
                    'file:///unselected/private.dtd', 'http://127.0.0.1/SMIL10.dtd'):
        with pytest.raises(ValueError, match='unapproved'):
            resolve(request, None, None)


def test_windows_schema_filenames_do_not_become_nested_entity_system_ids(monkeypatch):
    from hashlib import sha256
    from lxml import etree
    checker = load_script('daisy_schema_acceptance.py')
    paths = {name: PureWindowsPath('D:/Selected schemas/Unicode каталог') / name
             for name in ('xhtml1-transitional.dtd', 'xhtml-lat1.ent')}
    data = {
        'xhtml1-transitional.dtd': b'<!ENTITY % child SYSTEM "xhtml-lat1.ent">%child;<!ELEMENT root EMPTY>',
        'xhtml-lat1.ent': b'<!ATTLIST root answer CDATA #IMPLIED>',
    }
    seen = []
    def local_bytes(path):
        seen.append(path)
        return data[path.name]
    monkeypatch.setattr(checker, '_schema_bytes', local_bytes)
    identities = {name: sha256(raw).hexdigest() for name, raw in data.items()}
    resolver = checker.local_resolver(etree, paths, identities)
    document = b'<!DOCTYPE root SYSTEM "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd"><root answer="bounded"/>'
    assert etree.fromstring(document, checker.parser_for_html(resolver, etree)).get('answer') == 'bounded'
    assert set(seen) == set(paths.values())
    with pytest.raises(ValueError, match='unapproved'):
        resolver().resolve('file:///D:/unselected/xhtml-lat1.ent', None, None)


def test_local_schema_change_between_pin_validation_and_resolver_is_refused(tmp_path):
    from hashlib import sha256
    from lxml import etree
    checker = load_script('daisy_schema_acceptance.py')
    path = tmp_path / 'SMIL10.dtd'
    path.write_bytes(b'changed after validation')
    with pytest.raises(ValueError, match='changed before parsing'):
        checker.local_resolver(etree, {'SMIL10.dtd': path}, {'SMIL10.dtd': sha256(b'approved').hexdigest()})


@pytest.mark.parametrize('version,exit_code,errors,warnings', [
    ('5.2.1', 0, 0, 0), ('5.3.0', 1, 0, 0), ('5.3.0', 0, 1, 0), ('5.3.0', 0, 0, 1)],
    ids=['wrong-version', 'failed-process', 'error-in-report', 'warning-in-report'])
def test_zero_or_forged_success_cannot_satisfy_checker_gate(tmp_path, monkeypatch, version, exit_code, errors, warnings):
    checker = load_script('epubcheck_acceptance.py')
    publication, report = tmp_path / 'fixture.epub', tmp_path / 'report.json'
    publication.write_bytes(b'synthetic harness bytes')
    def invoke(arguments, report_path, environment, **kwargs):
        assert report_path == report
        assert arguments[1] == '-Xmx512m'
        report.write_text(json.dumps({'checker': {'checkerVersion': version, 'nFatal': 0,
            'nError': errors, 'nWarning': warnings, 'nUsage': 0}, 'messages': []}), encoding='utf-8')
        return SimpleNamespace(returncode=exit_code)
    monkeypatch.setattr(checker, 'run_checker', invoke)
    with pytest.raises(RuntimeError):
        checker.validate(tmp_path / 'java', tmp_path / 'validator.jar', publication, report)


def test_acceptance_report_does_not_replace_an_existing_report(tmp_path):
    checker = load_script('epubcheck_acceptance.py')
    path = tmp_path / 'report.json'
    path.write_bytes(b'previous evidence')
    with pytest.raises(FileExistsError):
        checker._write_report(path, {'status': 'PASS'})
    assert path.read_bytes() == b'previous evidence'


@pytest.mark.parametrize('program,timeout,message', [
    ("import os; os.write(1, b'x' * (2 * 1024 * 1024))", 10, 'diagnostic output budget'),
    ('import time; time.sleep(30)', .25, 'time allowance'),
], ids=['bounded-real-child-diagnostics', 'bounded-real-child-deadline'])
def test_checker_owns_and_bounds_the_actual_child(tmp_path, program, timeout, message):
    from carr_library.audio.capture_types import AudioError
    checker = load_script('epubcheck_acceptance.py')
    with pytest.raises(AudioError, match=message):
        checker.run_checker([sys.executable, '-I', '-B', '-c', program],
                            tmp_path / 'report.json', dict(os.environ), timeout=timeout)


def test_checker_settles_retained_child_before_its_caller_can_remove_files(tmp_path, monkeypatch):
    from carr_library.audio import capture_process
    from carr_library.audio.capture_types import AudioCleanupPending
    checker = load_script('epubcheck_acceptance.py')
    pending = AudioCleanupPending('synthetic retained child', cleanup_owner=object())
    calls = []
    def running(*args, **kwargs):
        assert kwargs['owned_tree'] is True
        assert kwargs['allow_failure'] is True
        assert kwargs['watch'] == (tmp_path / 'report.json',)
        raise pending
    monkeypatch.setattr(capture_process, 'run_process', running)
    monkeypatch.setattr(capture_process, 'wait_for_cleanup', lambda owner: calls.append(owner))
    with pytest.raises(AudioCleanupPending) as raised:
        checker.run_checker(['selected-java'], tmp_path / 'report.json', {})
    assert raised.value is pending and calls == [pending]
