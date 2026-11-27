"""Acceptance-tool boundary tests; these do not replace actual checker runs."""
import importlib.util
import json
from pathlib import Path
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
        def resolve_filename(self, filename, context):
            return filename
    paths = {'SMIL10.dtd': tmp_path / 'SMIL10.dtd', 'xhtml-lat1.ent': tmp_path / 'xhtml-lat1.ent'}
    resolve = checker.local_resolver(SimpleNamespace(Resolver=Resolver), paths)().resolve
    assert resolve('http://www.w3.org/TR/REC-SMIL/SMIL10.dtd', None, None) == str(paths['SMIL10.dtd'])
    assert resolve(paths['xhtml-lat1.ent'].as_uri(), None, None) == str(paths['xhtml-lat1.ent'])
    for request in ('https://unapproved.example/SMIL10.dtd', '../SMIL10.dtd',
                    'file:///unselected/private.dtd', 'http://127.0.0.1/SMIL10.dtd'):
        with pytest.raises(ValueError, match='unapproved'):
            resolve(request, None, None)


@pytest.mark.parametrize('version,exit_code,errors,warnings', [
    ('5.2.1', 0, 0, 0), ('5.3.0', 1, 0, 0), ('5.3.0', 0, 1, 0), ('5.3.0', 0, 0, 1)],
    ids=['wrong-version', 'failed-process', 'error-in-report', 'warning-in-report'])
def test_zero_or_forged_success_cannot_satisfy_checker_gate(tmp_path, monkeypatch, version, exit_code, errors, warnings):
    checker = load_script('epubcheck_acceptance.py')
    publication, report = tmp_path / 'fixture.epub', tmp_path / 'report.json'
    publication.write_bytes(b'synthetic harness bytes')
    def invoke(arguments, **kwargs):
        assert kwargs['shell'] is False and kwargs['timeout'] == 180
        assert arguments[1] == '-Xmx512m'
        report.write_text(json.dumps({'checker': {'checkerVersion': version, 'nFatal': 0,
            'nError': errors, 'nWarning': warnings, 'nUsage': 0}, 'messages': []}), encoding='utf-8')
        return SimpleNamespace(returncode=exit_code)
    monkeypatch.setattr(checker.subprocess, 'run', invoke)
    with pytest.raises(RuntimeError):
        checker.validate(tmp_path / 'java', tmp_path / 'validator.jar', publication, report)


def test_acceptance_report_does_not_replace_an_existing_report(tmp_path):
    checker = load_script('epubcheck_acceptance.py')
    path = tmp_path / 'report.json'
    path.write_bytes(b'previous evidence')
    with pytest.raises(FileExistsError):
        checker._write_report(path, {'status': 'PASS'})
    assert path.read_bytes() == b'previous evidence'
