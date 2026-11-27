"""Independent acceptance against an explicit, unmodified public CARR adapter."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


@pytest.fixture(scope="session")
def api():
    configured = os.environ.get("CARR_DORDIAI_ADAPTER_FILE")
    public_name = "carr_library.integrations.dordiai_voices"
    if configured:
        path = Path(configured)
    else:
        try:
            installed = importlib.util.find_spec(public_name)
        except ModuleNotFoundError as error:
            if error.name not in {"carr_library", "carr_library.integrations"}:
                raise
            installed = None
        if installed is None or installed.origin is None:
            pytest.skip("Dordiai adapter not integrated; select the actual adapter source explicitly")
        path = Path(installed.origin)
    if not path.is_file():
        if configured:
            pytest.fail("The explicitly selected CARR adapter does not exist")
        pytest.skip("Dordiai adapter not integrated; select the actual adapter source explicitly")
    path = path.resolve()
    helper = path.with_name("_dordiai_probe.py")
    identities = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, helper)}
    if configured:
        name = "_independent_carr_dordiai_adapter"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(public_name)
        assert Path(module.__file__).resolve() == path
    yield module
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in identities} == identities, (
        "Adapter source changed during acceptance; rerun against one stable source")


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "Synthetic engine \u2014 fixture" / "kx47Runtime"
    (root / "models/voices").mkdir(parents=True)
    (root / "models/Holos").mkdir()
    (root.parent / "manifest.ini").write_text('version = "acceptance-fixture.1"\n', encoding="utf-8")
    actor = Path(__file__).with_name("protocol_actor.py").read_bytes()
    (root / "nvda_worker.py").write_bytes(actor)
    (root / "scenario.json").write_text('{"mode":"normal"}', encoding="utf-8")
    (root / "inventory.json").write_text(json.dumps({
        "present": {"uk": True, "en": True, "ru": False},
        "compatible": {"speaker_a": ["uk", "en"], "not_installed": []},
        "managed": {},
    }), encoding="utf-8")
    (root / "models/voices/catalog.json").write_text(json.dumps({
        "speaker_a": "Synthetic speaker", "blocked": "Blocked fixture",
        "not_installed": "Missing fixture profile",
    }), encoding="utf-8")
    # These intentionally tiny metadata doubles are original acceptance code.
    # They contain no private engine, model, catalogue, or acoustic assets.
    (root / "windows_runtime.py").write_text(
        "def reset_inherited_dll_directory(): return {}\n"
        "def configure_worker_import_paths(): return {}\n", encoding="utf-8")
    (root / "voice_policy.py").write_text(
        "def filter_voice_catalog(rows):\n"
        " return {k:v for k,v in rows.items() if k.split(':')[0] != 'blocked'}\n", encoding="utf-8")
    (root / "voice_assets.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "def discover_voices(references,engine_id=None):\n"
        " return json.loads(Path(__file__).with_name('inventory.json').read_text())['managed']\n",
        encoding="utf-8")
    (root / "language_catalog.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "def state(): return json.loads(Path(__file__).with_name('inventory.json').read_text())\n"
        "def canonical_code(value): return value.lower().replace('_','-')\n"
        "def coverage(models=None,selected_only=True):\n"
        " return [dict(runtime_code=k,settings_label_uk=k,resources_present=v,status='fixture') for k,v in state()['present'].items()]\n"
        "def compatible_voice_ids(models,language,allowed_voice_ids):\n"
        " return tuple(k for k in allowed_voice_ids if language in state()['compatible'].get(k,[]))\n",
        encoding="utf-8")
    (root / "wide_languages.py").write_text("def registered(code): return None\n", encoding="utf-8")
    return root


@pytest.fixture
def adapter(api, runtime):
    value = api.DordiaiVoicesAdapter(
        runtime, python_executable=Path(sys.executable).resolve(strict=True),
        expected_worker_sha256=hashlib.sha256((runtime / "nvda_worker.py").read_bytes()).hexdigest(),
        request_timeout=1.0,
    )
    try:
        yield value
    finally:
        value.close()

