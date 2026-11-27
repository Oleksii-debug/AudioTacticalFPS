"""The current sealed-bundle identity is accepted without relaxing its schema."""
import json

import pytest

from carr_library.native_checks import _manifest


def write_manifest(tmp_path, **updates):
    value = {
        "schema": 1,
        "target": "Windows x64 / CPython 3.11",
        "bundled_python": True,
        "bundled_model": True,
        "bundled_ocr": True,
        "files": {"ResearchLibrary.exe": "a" * 64},
    }
    value.update(updates)
    (tmp_path / "bundle-manifest.json").write_text(json.dumps(value), encoding="utf-8")
    return value


def test_current_source_bound_audio_bundle_manifest_is_accepted(tmp_path):
    expected = write_manifest(tmp_path, source_commit="b" * 40, source_tree="c" * 40,
                              bundled_native_runtime=True, bundled_audio_runtime=True)
    assert _manifest(tmp_path) == expected


def test_legacy_manifest_without_optional_fields_is_accepted(tmp_path):
    expected = write_manifest(tmp_path)
    assert _manifest(tmp_path) == expected


@pytest.mark.parametrize("field", ["bundled_native_runtime", "bundled_audio_runtime",
                                   "bundled_launcher", "bundled_trusted_client"])
@pytest.mark.parametrize("invalid", ["true", 1, None])
def test_optional_runtime_flags_remain_strict_booleans(tmp_path, field, invalid):
    write_manifest(tmp_path, **{field: invalid})
    with pytest.raises(ValueError, match="boolean"):
        _manifest(tmp_path)


@pytest.mark.parametrize("field", ["source_commit", "source_tree"])
@pytest.mark.parametrize("invalid", ["A" * 40, "a" * 39, "", None, 1])
def test_source_identities_require_full_lowercase_git_sha(tmp_path, field, invalid):
    write_manifest(tmp_path, **{field: invalid})
    with pytest.raises(ValueError, match="Invalid installation"):
        _manifest(tmp_path)


def test_unknown_installation_fields_are_still_rejected(tmp_path):
    write_manifest(tmp_path, arbitrary_runtime=True)
    with pytest.raises(ValueError, match="Unsupported installation manifest"):
        _manifest(tmp_path)


def test_audio_runtime_false_and_source_commit_only_remain_valid(tmp_path):
    expected = write_manifest(tmp_path, source_commit="d" * 40, bundled_audio_runtime=False)
    assert _manifest(tmp_path) == expected


@pytest.mark.parametrize("included", [False, True])
def test_exact_source_launcher_and_trusted_dependency_flags_are_accepted(tmp_path, included):
    expected = write_manifest(tmp_path, source_commit="b" * 40, source_tree="c" * 40,
                              bundled_launcher=included, bundled_trusted_client=included,
                              files={"ResearchLibrary.exe": "a" * 64,
                                     "native-launcher-build.json": "d" * 64,
                                     "runtime-sbom.cdx.json": "e" * 64,
                                     "trusted-client-dependencies.json": "f" * 64})
    assert _manifest(tmp_path) == expected
