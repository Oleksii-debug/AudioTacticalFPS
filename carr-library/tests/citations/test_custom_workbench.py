"""Explicit local styles remain captured across preview, file changes and export."""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import socket

from docx import Document
import pytest

from carr_library.application import LibraryApp
from carr_library.bibliography_cli import main
from carr_library.citations.bibliography_workbench import BibliographyWorkbench, load_style_file
from carr_library.ui.academic_library_dialog import AcademicController


STYLE = b'''<?xml version="1.0" encoding="utf-8"?>
<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
  <info><title>My explicit local style</title>
    <id>https://example.invalid/styles/my-local-style</id>
    <updated>2026-11-26T00:00:00+00:00</updated></info>
  <citation><layout prefix="[Local: " suffix="]"><text variable="title"/></layout></citation>
  <bibliography><layout><text variable="title" prefix="LOCAL: " font-style="italic"/></layout></bibliography>
</style>'''


@pytest.fixture
def research(tmp_path, monkeypatch):
    def offline(*args, **kwargs):
        raise AssertionError('Custom styles must not make network requests.')
    monkeypatch.setattr(socket, 'create_connection', offline)
    monkeypatch.setattr(socket, 'getaddrinfo', offline)
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Local style research')
    controller = AcademicController(app, workspace)
    identity = controller.create({'title': 'Original bibliography title',
                                  'notes': 'PrivateResearchNoteMustRemainPrivate'}).id
    path = tmp_path / 'my-style.csl'
    path.write_bytes(STYLE)
    return app, workspace, controller, identity, path


@pytest.mark.parametrize('format', ['text', 'html', 'docx'])
def test_preview_and_export_reuse_captured_style_after_file_replacement(research, tmp_path, format):
    app, workspace, controller, identity, path = research
    before = controller.get(identity)
    custom = load_style_file(path)
    workbench = BibliographyWorkbench(controller)
    preview = workbench.preview(identity=identity, style=custom)
    assert preview.style is custom
    assert preview.result.custom_style is True
    assert preview.result.style_sha256 == sha256(STYLE).hexdigest()
    assert 'LOCAL: Original bibliography title' in preview.bibliography
    assert 'Custom CSL style loaded' in preview.explanation
    path.write_bytes(STYLE.replace(b'LOCAL:', b'REPLACED:'))
    target = tmp_path / ('bibliography.' + format)
    workbench.export(preview, target, format=format, require_absent=True)
    raw = target.read_bytes()
    if format == 'docx':
        text = '\n'.join(paragraph.text for paragraph in Document(BytesIO(raw)).paragraphs)
    else:
        text = raw.decode('utf-8')
    assert 'LOCAL: ' in text and 'REPLACED:' not in text
    assert 'Original bibliography title' in text
    assert 'PrivateResearchNoteMustRemainPrivate' not in text
    assert controller.get(identity) == before


def test_custom_style_cannot_bypass_changed_metadata_guard(research, tmp_path):
    _, _, controller, identity, path = research
    workbench = BibliographyWorkbench(controller)
    preview = workbench.preview(style=load_style_file(path))
    controller.update(controller.get(identity), {'title': 'A changed reference'})
    target = tmp_path / 'stale.txt'
    with pytest.raises(ValueError, match='changed'):
        workbench.export(preview, target)
    assert not target.exists()


def test_cli_loads_only_explicit_style_and_reports_exact_identity(research, tmp_path, capsys):
    app, workspace, _, _, path = research
    target = tmp_path / 'cli-bibliography.html'
    assert main(['format', '--data-dir', str(app.root), '--workspace', workspace,
                 '--style-file', str(path), '--output', str(target), '--format', 'html']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['custom_style'] is True
    assert report['style'] == 'https://example.invalid/styles/my-local-style'
    assert report['style_sha256'] == sha256(STYLE).hexdigest()
    assert 'LOCAL: ' in target.read_text() and 'PrivateResearchNote' not in target.read_text()


def test_cli_style_choices_are_explicit_and_mutually_exclusive(research, capsys):
    app, workspace, _, _, path = research
    with pytest.raises(SystemExit) as result:
        main(['format', '--data-dir', str(app.root), '--workspace', workspace,
              '--style', 'harvard', '--style-file', str(path)])
    assert result.value.code == 2
    assert 'not allowed with argument' in capsys.readouterr().err


def test_invalid_custom_style_does_not_create_output(research, tmp_path, capsys):
    app, workspace, _, _, path = research
    path.write_bytes(b'<!DOCTYPE style [<!ENTITY e SYSTEM "file:///private">]><style>&e;</style>')
    target = tmp_path / 'never-created.txt'
    assert main(['format', '--data-dir', str(app.root), '--workspace', workspace,
                 '--style-file', str(path), '--output', str(target)]) == 1
    assert not target.exists()
    assert 'Research bibliography:' in capsys.readouterr().err


def test_regular_file_loader_rejects_oversize_before_reading(research):
    from carr_library.citations.csl import MAX_CUSTOM_STYLE_BYTES
    *_, path = research
    with path.open('wb') as output:
        output.truncate(MAX_CUSTOM_STYLE_BYTES + 1)
    with pytest.raises(ValueError, match='512 KiB'):
        load_style_file(path)


def test_regular_file_loader_refuses_directories_and_missing_files(tmp_path):
    with pytest.raises((OSError, ValueError)):
        load_style_file(tmp_path)
    with pytest.raises(OSError):
        load_style_file(tmp_path / 'missing.csl')


@pytest.mark.parametrize('path', [r'\\server\share\style.csl', r'\\.\pipe\style',
    r'\\?\C:\style.csl', r'\??\C:\style.csl', '//server/share/style.csl',
    'NUL', 'CON.csl', r'C:\folder\PRN.csl', r'C:\folder\style.csl:stream', 'C:style.csl'])
def test_network_devices_and_aliases_are_rejected_before_open(path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('A disallowed file path reached the operating-system open call.')
    monkeypatch.setattr(os, 'open', forbidden)
    with pytest.raises(ValueError):
        load_style_file(path)


def test_regular_file_loader_bounds_file_growth(research, monkeypatch):
    """The same read remains bounded if size changes after descriptor inspection."""
    from carr_library.citations.csl import MAX_CUSTOM_STYLE_BYTES
    *_, path = research
    real_fstat = os.fstat
    def grow(descriptor):
        snapshot = real_fstat(descriptor)
        with path.open('ab') as output:
            output.truncate(MAX_CUSTOM_STYLE_BYTES + 1)
        return snapshot
    monkeypatch.setattr(os, 'fstat', grow)
    with pytest.raises(ValueError):
        load_style_file(path)
