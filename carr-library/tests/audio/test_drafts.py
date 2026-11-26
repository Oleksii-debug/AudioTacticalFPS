"""Recovery storage uses real files; engine invocation is never recovery."""
import os
from types import SimpleNamespace

import pytest

from carr_library.audio.contracts import digest
from carr_library.audio.drafts import DraftStore, decode_draft, draft_path, new_session_id
from carr_library.audio.project import file_sha256, json_bytes, read_json, restore_project
from carr_library.ui.audio_studio import StudioSession


def studio(root=None):
    session = StudioSession()
    session.voices = (SimpleNamespace(provider="flite", id="slt"),)
    if root is not None:
        session.prepare("Narrator: Committed source.")
        session.save(new_directory=root)
    return session


def save_draft(session, text="Narrator: Unsaved words.", **settings):
    session.dirty = True
    return session.autosave(text=text, title="Working title", settings={
        "interpret_speakers": True, "export_format": "wav", "stems": True, **settings})


def test_draft_preserves_invalid_text_and_source_file(tmp_path):
    session = studio(tmp_path / "project")
    source = session.store.path.read_bytes()
    invalid = "#\n[pause not-a-number]\n\0unfinished"
    draft = save_draft(session, invalid)
    with pytest.raises(ValueError):
        session.prepare(invalid)
    loaded = session.draft_store.read(session.session_id)
    assert loaded.text == invalid
    assert loaded.title == "Working title"
    assert loaded.base_project_sha256 == session.file_sha256
    assert loaded.revision == draft.revision == 1
    assert session.store.path.read_bytes() == source
    if os.name == "posix":
        assert (session.draft_store.directory / f"{session.session_id}.json").stat().st_mode & 0o077 == 0


def test_autosave_requires_selected_folder_and_reserved_folder_is_not_a_project(tmp_path):
    session = studio()
    assert save_draft(session, "") is None
    root = session.reserve_draft_folder(tmp_path / "chosen")
    save_draft(session, "")
    assert not (root / "project.json").exists()
    reopened = StudioSession()
    assert reopened.open(root) is None
    assert reopened.draft_store.list()[0].text == ""
    session.prepare("Narrator: First valid committed script.")
    session.save()
    assert session.store.path.is_file()
    assert not session.draft_store.list()


def test_windows_have_separate_stable_ids_and_only_own_draft_is_cleared(tmp_path):
    first = studio(tmp_path / "project")
    second = StudioSession()
    second.open(first.store.root)
    first_id = first.session_id
    save_draft(first, "First window")
    save_draft(second, "Second window")
    latest = save_draft(first, "First window revised")
    assert first.session_id == first_id != second.session_id
    assert latest.revision == 2
    assert len(first.draft_store.list()) == 2
    first.prepare("Narrator: First window committed.")
    first.save()
    assert [draft.session_id for draft in first.draft_store.list()] == [second.session_id]
    assert second.draft_store.read(second.session_id).text == "Second window"


def test_stale_commit_retains_own_draft_and_recovered_base(tmp_path):
    first = studio(tmp_path / "project")
    old_sha = first.file_sha256
    saved = save_draft(first, "Narrator: Recovered old edits.")
    other = StudioSession()
    other.open(first.store.root)
    other.prepare("Narrator: Newer committed source.")
    other.save()
    first.prepare(saved.text)
    with pytest.raises(ValueError, match="changed on disk"):
        first.save()
    assert first.draft_store.read(first.session_id).base_project_sha256 == old_sha
    recovered = StudioSession()
    recovered.open(first.store.root)
    recovered.recover(saved)
    assert recovered.file_sha256 == old_sha
    recovered.prepare(saved.text)
    save_draft(recovered, saved.text)
    with pytest.raises(ValueError, match="changed on disk"):
        recovered.save()
    recovered.save(new_directory=tmp_path / "recovered-copy")
    assert recovered.project.source_text == saved.text
    assert first.session_id in {draft.session_id for draft in first.draft_store.list()}


def test_recovery_keeps_role_overrides_and_never_configures_or_starts_an_engine(tmp_path, monkeypatch):
    session = studio(tmp_path / "project")
    session.assign("Narrator", provider="flite", voice="rms", pan=-.75, rate=1.2)
    draft = save_draft(session, session.project.source_text,
                       ffmpeg="/untrusted/from/backup", ffprobe="/untrusted/probe")
    def forbidden(*args, **kwargs):
        pytest.fail("Recovery attempted to execute an engine")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    recovered = StudioSession()
    recovered.open(session.store.root)
    settings = recovered.recover(draft)
    assert "ffmpeg" not in settings and "ffprobe" not in settings
    recovered.prepare(draft.text)
    assert recovered.project.roles[0].voice == "rms"
    assert recovered.project.roles[0].pan == -.75
    assert recovered.dirty


@pytest.mark.parametrize("include_audio", [False, True])
def test_backup_restore_keeps_drafts_without_changing_original_source(tmp_path, include_audio):
    session = studio(tmp_path / "project")
    first = save_draft(session, "\0invalid but recoverable")
    second_id = new_session_id()
    session.draft_store.save(second_id, text="another window", title="Second", settings={})
    original = session.store.path.read_bytes()
    backup = tmp_path / "backup"
    checksum = session.store.backup(backup, include_audio=include_audio)
    restored = restore_project(backup, tmp_path / "restored", manifest_sha256=checksum)
    drafts = DraftStore(restored.root).list()
    assert {draft.session_id for draft in drafts} == {session.session_id, second_id}
    assert DraftStore(restored.root).read(session.session_id) == first
    assert restored.path.read_bytes() == original == session.store.path.read_bytes()
    assert read_json(backup / "backup-manifest.json")["includes_completed_audio"] is include_audio


def test_corrupt_draft_is_never_overwritten_or_published_in_backup(tmp_path):
    session = studio(tmp_path / "project")
    save_draft(session)
    path = session.draft_store.directory / f"{session.session_id}.json"
    document = read_json(path)
    document["payload"]["text"] = "tampered"
    path.write_bytes(json_bytes(document))
    corrupted = path.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        save_draft(session, "replacement")
    with pytest.raises(ValueError, match="checksum"):
        session.store.backup(tmp_path / "backup")
    assert path.read_bytes() == corrupted
    assert not (tmp_path / "backup").exists()
    assert not list(tmp_path.glob(".audio-backup-*"))


def test_restore_rejects_corrupt_envelope_even_if_backup_hashes_are_recomputed(tmp_path):
    session = studio(tmp_path / "project")
    save_draft(session)
    backup = tmp_path / "backup"
    session.store.backup(backup)
    relative = f"editor-drafts/{session.session_id}.json"
    path = backup / relative
    document = read_json(path)
    document["payload"]["text"] = "forged draft"
    path.write_bytes(json_bytes(document))
    manifest = read_json(backup / "backup-manifest.json")
    manifest["files"][relative] = file_sha256(path)
    (backup / "backup-manifest.json").write_bytes(json_bytes(manifest))
    with pytest.raises(ValueError, match="checksum"):
        restore_project(backup, tmp_path / "restored", manifest_sha256=file_sha256(backup / "backup-manifest.json"))
    assert not (tmp_path / "restored").exists()


def test_cancelled_backup_and_restore_publish_nothing_and_leave_drafts(tmp_path):
    session = studio(tmp_path / "project")
    save_draft(session)
    with pytest.raises(InterruptedError):
        session.store.backup(tmp_path / "cancelled", cancel=lambda: True)
    checksum = session.store.backup(tmp_path / "backup")
    with pytest.raises(InterruptedError):
        restore_project(tmp_path / "backup", tmp_path / "restored", manifest_sha256=checksum, cancel=lambda: True)
    assert not (tmp_path / "cancelled").exists() and not (tmp_path / "restored").exists()
    assert session.draft_store.read(session.session_id)


def test_backup_freezes_draft_snapshot_while_render_and_editor_continue(tmp_path, monkeypatch):
    from carr_library.audio.jobs import RenderJournal
    from carr_library.audio import project as project_module
    session = studio(tmp_path / "project")
    first = save_draft(session, "Snapshot text")
    journal = RenderJournal(session.store.root)
    journal.start(session.project)
    publish = project_module.atomic_bytes
    changed = False
    def update_editor_after_snapshot(path, data, **options):
        nonlocal changed
        if path.name == "project.json" and path.parent.name.startswith(".audio-backup-"):
            changed = True
            save_draft(session, "Newer live editor text")
        return publish(path, data, **options)
    monkeypatch.setattr(project_module, "atomic_bytes", update_editor_after_snapshot)
    checksum = session.store.backup(tmp_path / "backup")
    restored = restore_project(tmp_path / "backup", tmp_path / "restored", manifest_sha256=checksum)
    assert changed
    assert DraftStore(restored.root).read(session.session_id) == first
    assert session.draft_store.read(session.session_id).text == "Newer live editor text"
    assert read_json(session.store.root / "render-state.json")["status"] == "running"
    assert read_json(restored.root / "render-state.json")["status"] == "interrupted"


@pytest.mark.parametrize("session_id", ["../outside", "A" * 32, "x/y", "0" * 31, "0" * 32 + ".json"])
def test_draft_identity_cannot_select_arbitrary_paths(tmp_path, session_id):
    drafts = DraftStore.reserve(tmp_path / "drafts")
    with pytest.raises(ValueError):
        drafts.save(session_id, text="text", title="title", settings={})
    assert not drafts.directory.exists()
    assert not draft_path("editor-drafts/../project.json")


def test_draft_symlink_and_linked_parent_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating a symlink requires privilege on this platform")
    with pytest.raises(ValueError):
        DraftStore.reserve(linked / "new")
    assert not (outside / "new").exists()
    drafts = DraftStore.reserve(tmp_path / "drafts")
    drafts.directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        drafts.save(new_session_id(), text="x", title="x", settings={})
    assert not list(outside.iterdir())


def test_parser_rejects_unknown_settings_and_session_filename_mismatch(tmp_path):
    session = studio(tmp_path / "project")
    draft = save_draft(session)
    document = read_json(session.draft_store.directory / f"{draft.session_id}.json")
    with pytest.raises(ValueError, match="different session"):
        decode_draft(document, expected_session=new_session_id())
    document["payload"]["settings"]["shell_command"] = "anything"
    document["sha256"] = digest(document["payload"])
    with pytest.raises(ValueError, match="settings"):
        decode_draft(document)


def test_failed_atomic_replacement_preserves_previous_draft(tmp_path, monkeypatch):
    session = studio(tmp_path / "project")
    first = save_draft(session, "original draft")
    def fail(*args, **kwargs):
        raise OSError("Simulated interrupted publication")
    monkeypatch.setattr("carr_library.audio.project.os.replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        save_draft(session, "not fully saved")
    assert session.draft_store.read(session.session_id) == first
    assert not list(session.draft_store.directory.glob(".audio-save-*"))
