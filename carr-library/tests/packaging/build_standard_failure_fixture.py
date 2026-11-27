"""Build a disposable native setup fixture whose verified bootstrap exits 23.

This test helper never modifies the supplied complete bundle. Its output is a
clearly marked negative fixture, never a deliverable or runtime qualification.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

NEGATIVE_BOOTSTRAP = (
    b"from pathlib import Path\n"
    b"root = Path(__file__).resolve().parent\n"
    b"environment = root / '.venv'\n"
    b"environment.mkdir(exist_ok=False)\n"
    b"(environment / 'failed-install-state.txt').write_bytes(b'preserve failed environment\\n')\n"
    b"(root / 'bootstrap-negative-reached.txt').write_bytes(b'23\\n')\n"
    b"raise SystemExit(23)\n"
)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("carr_negative_setup_builder", ROOT / "packaging/build_standard_installer.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def create(bundle, source_commit, manifest_sha256, output, compiler, compiler_sha256):
    if os.name != "nt" or os.environ.get("CI", "").lower() != "true":
        raise RuntimeError("The deliberately failing installer fixture is restricted to disposable Windows CI.")
    delivery = builder._delivery()
    bundle, output = delivery._absolute(bundle), delivery._absolute(output)
    raw = delivery._read_manifest(bundle, lambda: False)
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("The complete input bundle differs from its recorded manifest.")
    manifest = delivery._manifest(raw, source_commit)
    delivery._require_transferable_audio_inventory(manifest)
    delivery._trusted_verify(bundle, manifest_bytes=raw)
    with TemporaryDirectory(prefix="native-negative-setup-", dir=output.parent) as temporary:
        fixture = Path(temporary) / "fixture"
        fixture.mkdir()
        for name in manifest["files"]:
            source, target = bundle / name, fixture / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name == "install_windows.py":
                target.write_bytes(NEGATIVE_BOOTSTRAP)
                manifest["files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
            else:
                # Save CI disk without mutating any linked source; prepare()
                # later copies and rehashes all bytes into its own output.
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)
        manifest["fixture_purpose"] = "NEGATIVE TEST ONLY: bootstrap exits 23"
        modified = json.dumps(manifest, indent=2).encode("utf-8") + b"\n"
        (fixture / "bundle-manifest.json").write_bytes(modified)
        result = builder.prepare(fixture, source_commit, hashlib.sha256(modified).hexdigest(), output,
                                 compiler=compiler, compiler_sha256=compiler_sha256)
    if delivery._read_manifest(bundle, lambda: False) != raw:
        raise AssertionError("The original complete bundle changed during fixture preparation.")
    delivery._trusted_verify(bundle, manifest_bytes=raw)
    return {**result, "negative_fixture_only": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--compiler-sha256", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(create(args.bundle, args.source_commit, args.manifest_sha256,
                            args.output, args.compiler, args.compiler_sha256), indent=2))


if __name__ == "__main__":
    main()
