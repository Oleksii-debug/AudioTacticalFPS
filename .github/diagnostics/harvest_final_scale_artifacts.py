"""Read fixed, digest-bound capacity artifacts; never replay a product scenario."""
from contextlib import contextmanager
from hashlib import sha1, sha256
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = "Oleksii-debug/AudioTacticalFPS"
TARGET = "71ca92be691455d0ff51ed163b1d91749e5ae62e"
RUN = 46124999025
MIB = 1024**2
MAX_ZIP = 512*MIB
MAX_EXPANDED = 2304*MIB
MAX_MEMBER = 1024*MIB
MAX_JSON = 8*MIB
MAX_JSON_TOTAL = 64*MIB
MAX_ENTRIES = 200_050
DEADLINE = time.monotonic() + 1200
API = "https://api.github.com"
SELECTED_SCRIPTS = {
    "benchmark_library_scale.py": "81e569b33a6acc4a64f3ee04176598487f9efc07",
    "benchmark_library_catalog.py": "5c674cac4b02237c8cf65122fbc58dca397ed3d9",
    "benchmark_library_backup.py": "17d8ae5ae97ab57fe5ede1ea46a6f22b385e2b56",
}
TIERS = {
    "books-1000": (1000, 40_000, 10_279_003),
    "books-10000": (10_000, 400_000, 102_790_003),
    "books-50000": (50_000, 2_000_000, 513_950_003),
    "text-65m": (1, 17, 68_157_613),
    "text-129m": (1, 33, 135_266_477),
    "text-2g": (1, 1000, 2_147_483_648),
}

def require(value, message):
    if not value:
        raise AssertionError(message)

def check():
    if time.monotonic() >= DEADLINE:
        raise TimeoutError("The bounded read-only evidence extraction expired.")

def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON object key.")
        value[key] = item
    return value

def decode(raw):
    require(len(raw) <= MAX_JSON, "Selected JSON exceeds its bound.")
    def bad_constant(value):
        raise ValueError("Nonfinite JSON constant.")
    value = json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=bad_constant)
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(count <= 1_000_000 and depth <= 64, "Selected JSON structure exceeds its bound.")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, float):
            require(math.isfinite(item), "Nonfinite JSON value.")
        elif isinstance(item, str):
            require(len(item) <= MAX_JSON, "JSON string exceeds its bound.")
    return value

def local_bytes(path, maximum=MAX_JSON):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and not getattr(before, "st_file_attributes", 0) & 0x400
            and before.st_size <= maximum, "A local proof file is not bounded and ordinary.")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    after = path.lstat()
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    require(stamp(before) == stamp(after) and len(raw) == before.st_size,
            "A local proof file changed during capture.")
    return raw

def git(*args):
    check()
    result = subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30)
    require(len(result.stdout) <= 2*MIB, "Git metadata exceeds its bound.")
    return result.stdout

def git_blob(raw):
    return sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()

def source_identity():
    require(git("rev-parse", "HEAD").decode().strip() == os.environ["CARR_HARVEST_HEAD"],
            "Harvester checkout differs from its exact selected commit.")
    require(git("rev-parse", "HEAD^").decode().strip() == TARGET,
            "The isolated evidence commit is not directly based on the measured source.")
    records = git("ls-tree", "-r", "-z", TARGET, "--", "carr-library/src/carr_library").split(b"\0")
    inventory = []
    prefix = "carr-library/src/carr_library/"
    for record in records:
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, kind, digest = header.decode("ascii").split()
        path = name.decode("utf-8")
        if not path.endswith(".py"):
            continue
        require(mode == "100644" and kind == "blob" and path.startswith(prefix),
                "Unexpected canonical package entry.")
        raw = local_bytes(Path(path), 32*MIB)
        require(git_blob(raw) == digest, "Checked-out package differs from the exact target.")
        inventory.append([path[len(prefix):], sha256(raw).hexdigest()])
    inventory.sort()
    require(len(inventory) >= 400, "The complete target package inventory is unexpectedly small.")
    scripts = {}
    for name, digest in SELECTED_SCRIPTS.items():
        raw = local_bytes(Path("carr-library/scripts") / name)
        require(git_blob(raw) == digest, "A selected corpus script differs from its frozen blob.")
        scripts[name] = sha256(raw).hexdigest()
    return {"inventory": inventory,
            "package_sha256": sha256(json.dumps(inventory, separators=(",", ":")).encode()).hexdigest(),
            "scripts": scripts}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None

def request(path):
    check()
    require(path.startswith("/repos/" + REPOSITORY + "/"), "Unexpected API resource.")
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "Authorization": "Bearer " + os.environ["CARR_ARTIFACT_TOKEN"]}
    return urllib.request.Request(API + path, headers=headers)

def api(path):
    try:
        with urllib.request.build_opener(NoRedirect).open(request(path), timeout=30) as response:
            raw = response.read(MAX_JSON + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError("GitHub metadata request failed with status " + str(error.code)) from None
    return decode(raw)

@contextmanager
def archive(item):
    # Never carry the GitHub Authorization header to the signed artifact host.
    try:
        response = urllib.request.build_opener(NoRedirect).open(
            request("/repos/" + REPOSITORY + "/actions/artifacts/" + str(item["artifact_id"]) + "/zip"),
            timeout=30)
    except urllib.error.HTTPError as error:
        require(error.code == 302, "Artifact API did not issue the expected signed redirect.")
        location = error.headers.get("Location", "")
        error.close()
    else:
        response.close()
        raise AssertionError("Artifact API did not issue a signed redirect.")
    parsed = urllib.parse.urlsplit(location)
    require(parsed.scheme == "https" and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443) and parsed.hostname
            and (parsed.hostname.endswith(".blob.core.windows.net")
                 or parsed.hostname.endswith(".actions.githubusercontent.com")),
            "Artifact redirect host is outside the bounded download policy.")
    with tempfile.TemporaryFile("w+b") as output:
        size, digest = 0, sha256()
        try:
            with urllib.request.build_opener(NoRedirect).open(
                    urllib.request.Request(location), timeout=30) as response:
                require(response.status == 200, "Signed artifact download was not successful.")
                while True:
                    check()
                    chunk = response.read(64*1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    require(size <= item["size_in_bytes"] <= MAX_ZIP,
                            "Artifact exceeds its fixed compressed size.")
                    output.write(chunk)
                    digest.update(chunk)
        except urllib.error.HTTPError as error:
            raise RuntimeError("Signed artifact request failed with status " + str(error.code)) from None
        require(size == item["size_in_bytes"] and digest.hexdigest() == item["sha256"],
                "Downloaded artifact size or SHA256 differs from the pinned receipt.")
        output.seek(0)
        with zipfile.ZipFile(output) as zipped:
            infos = zipped.infolist()
            require(len(infos) <= MAX_ENTRIES, "Artifact member count exceeds its bound.")
            expanded, seen = 0, set()
            for info in infos:
                check()
                name = info.filename
                path = PurePosixPath(name)
                require(name and "\\" not in name and "\0" not in name
                        and not path.is_absolute() and ".." not in path.parts
                        and name == path.as_posix() + ("/" if info.is_dir() else "") and name not in seen,
                        "Artifact contains an ambiguous member path.")
                seen.add(name)
                mode = (info.external_attr >> 16) & 0xffff
                require(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR)
                        and not info.flag_bits & 1
                        and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        and 0 <= info.file_size <= MAX_MEMBER
                        and 0 <= info.compress_size <= MAX_ZIP,
                        "Artifact member type or declared size is unsupported.")
                expanded += info.file_size
                require(expanded <= MAX_EXPANDED, "Artifact expanded size exceeds its bound.")
            yield zipped, expanded

def selected(zipped, name, budget, *, required=True):
    try:
        info = zipped.getinfo(name)
    except KeyError:
        require(not required, "Required saved artifact report is absent: " + name)
        return None
    require(not info.is_dir() and info.file_size <= MAX_JSON, "Selected report exceeds its JSON bound.")
    budget[0] += info.file_size
    require(budget[0] <= MAX_JSON_TOTAL, "Selected report total exceeds its bound.")
    with zipped.open(info) as stream:
        raw = stream.read(MAX_JSON + 1)
    require(len(raw) == info.file_size, "Selected saved report is truncated.")
    return decode(raw), sha256(raw).hexdigest()

def harvest(item, source):
    check()
    prefix = "/repos/" + REPOSITORY
    job = api(prefix + "/actions/jobs/" + str(item["job_id"]))
    require(job["run_id"] == RUN and job["head_sha"] == TARGET
            and job["status"] == "completed" and job["conclusion"] == item["conclusion"]
            and job["name"] == item["tier"] + " / " + item["os"],
            "Completed job identity differs from the fixed selection.")
    metadata = api(prefix + "/actions/artifacts/" + str(item["artifact_id"]))
    require(metadata["id"] == item["artifact_id"] and not metadata["expired"]
            and metadata["size_in_bytes"] == item["size_in_bytes"]
            and metadata["digest"] == "sha256:" + item["sha256"]
            and metadata["name"] == "actual-library-" + item["os"] + "-" + item["tier"] + "-" + TARGET
            and metadata["workflow_run"]["id"] == RUN
            and metadata["workflow_run"]["head_sha"] == TARGET,
            "Artifact API metadata differs from its fixed identity, size or SHA256.")
    with archive(item) as (zipped, expanded):
        budget, hashes = [0], {}
        def report(name, required=True):
            pair = selected(zipped, name, budget, required=required)
            if pair is None:
                return None
            value, hashes[name] = pair
            return value
        before = report("installed-before.json")
        after = report("installed-after.json")
        for key in ("inventory", "package_sha256", "origins", "scripts", "executable", "commit"):
            require(before[key] == after[key], "Saved before/after identity changed: " + key)
        require(before["commit"] == TARGET and str(before["run"]) == str(RUN)
                and before["tier"] == item["tier"], "Saved target run or tier differs.")
        for key in ("inventory", "package_sha256", "scripts"):
            require(before[key] == source[key], "Artifact identity differs from the exact target source: " + key)
        require(before["package_files"] == len(source["inventory"]), "Saved package count differs.")
        package = dict(source["inventory"])
        require(len(before["origins"]) == 45, "The current final selected origin count differs.")
        for name, origin in before["origins"].items():
            relative = name.removeprefix("carr_library.").replace(".", "/") + ".py"
            require(package.get(relative) == origin["sha256"], "Saved origin differs from package: " + name)
        outcome = report("outcome.json")
        capacity = report("runner-capacity.json")
        early = report("backup-capacity-before-import.json")
        collected = report("collected-evidence.json")
        require(type(collected) is list and len(collected) <= MAX_ENTRIES,
                "Collected evidence manifest has an invalid shape.")
        collected_map = {}
        for row in collected:
            require(type(row) is dict and set(row) == {"path", "bytes", "sha256"}
                    and row["path"] not in collected_map, "Invalid collected evidence entry.")
            collected_map[row["path"]] = row
        prefix = "fixture-evidence/"
        saved = {}
        for name in ("generation.json", "preflight.json", "benchmark/state.json",
                     "benchmark/authored-crash.json", "benchmark/catalog-verification.json",
                     "benchmark/search-measurements.json", "benchmark/backup-restore.json"):
            value = report(prefix + name, required=False)
            if value is not None:
                row = collected_map.get(name)
                info = zipped.getinfo(prefix + name)
                require(row and row["bytes"] == info.file_size
                        and row["sha256"] == hashes[prefix + name],
                        "Saved report differs from the collected evidence manifest.")
                saved[name] = value
        books, segments, original_bytes = TIERS[item["tier"]]
        if item["conclusion"] == "success":
            require(outcome["status"] == "MEASURED_SYNTHETIC_CORPUS"
                    and outcome["commit"] == TARGET and str(outcome["run"]) == str(RUN)
                    and outcome["actual_books"] == books
                    and outcome["actual_original_segments"] == segments,
                    "Successful job does not contain the required exact corpus outcome.")
            require(saved["generation.json"]["generated_bytes"] == original_bytes,
                    "Actual original byte count differs.")
            require(outcome["generation"] == saved["generation.json"]
                    and outcome["catalog"] == saved["benchmark/catalog-verification.json"]
                    and outcome["search"] == saved["benchmark/search-measurements.json"],
                    "Outcome differs from independently collected phase reports.")
            require(saved["benchmark/authored-crash.json"]["committed"] == 1
                    and outcome["real_first_commit_process_exit"] == 73
                    and outcome["resume_same_saved_queue"] is True,
                    "Actual durable interruption/resume receipt is absent.")
            required = item["tier"] in ("books-50000", "text-2g")
            require(outcome["backup_roundtrip_scope"] ==
                    ("REQUIRED_AND_VERIFIED" if required else "NOT_REQUIRED_ON_THIS_TIER"),
                    "Backup acceptance scope differs.")
            if required:
                backup = saved["benchmark/backup-restore.json"]
                require(outcome["backup_roundtrip"] == backup
                        and backup["status"] == "BACKUP_RESTORE_VERIFIED"
                        and backup["source_books"] == books and backup["source_segments"] == segments,
                        "Required actual backup roundtrip did not finish.")
        else:
            require(outcome.get("status") in ("FAILED", "INCOMPLETE", "NOT_RUN"),
                    "A failed job must not produce a successful capacity verdict.")
        # Full source reports remain in the immutable artifact. Only compact
        # measurements, not the complete package/implementation lists, are printed.
        def compact(value):
            if isinstance(value, dict):
                return {k: compact(v) for k, v in value.items()
                        if k not in {"inventory", "implementation", "actual_implementation",
                                     "admission", "spec", "preflight", "workers", "import_budget",
                                     "source_inventory", "archive_inventory", "canonical_catalog"}}
            if isinstance(value, list):
                return [compact(v) for v in value]
            return value
        result = {"schema": "carr-final-scale-artifact-harvest/1",
            "scope": "READ_ONLY_SAVED_ACTUAL_REPORTS_NO_CORPUS_REPLAY",
            "target": TARGET, "run_id": RUN, "selection": item,
            "package_files": before["package_files"], "package_sha256": before["package_sha256"],
            "selected_origins": before["origins"], "scripts": before["scripts"],
            "python": before["python"], "sqlite": before["sqlite"], "platform": before["platform"],
            "artifact_expanded_bytes": expanded, "selected_json_bytes": budget[0], "selected_sha256": hashes,
            "runner_capacity": capacity, "largest_tier_capacity": early,
            "outcome": compact(outcome),
            "completed_saved_reports": ({name: compact(saved[name]) for name in (
                "generation.json", "benchmark/authored-crash.json",
                "benchmark/catalog-verification.json", "benchmark/search-measurements.json",
                "benchmark/backup-restore.json") if name in saved}
                if item["conclusion"] != "success" else {}),
            "phase_admissions": {name: value.get("admission") for name, value in saved.items()
                                if isinstance(value, dict) and "admission" in value},
            "job_steps": job["steps"]}
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        require(len(encoded) <= 256*1024, "Compact actual evidence exceeds its fixed log bound.")
        return result, encoded

def main():
    require(sys.flags.isolated and not os.environ.get("PYTHONPATH"), "Use the isolated evidence runner.")
    manifest_path = Path(__file__).with_name("final_scale_artifacts.json")
    manifest_raw = local_bytes(manifest_path)
    self_raw = local_bytes(Path(__file__))
    for path, raw in ((Path(__file__), self_raw), (manifest_path, manifest_raw)):
        relative = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        require(git_blob(raw) == git("rev-parse", "HEAD:" + relative).decode().strip(),
                "Harvester code or fixed selection differs from its committed blob.")
    manifest = decode(manifest_raw)
    require(set(manifest) == {"schema", "repository", "target", "run_id", "artifacts"}
            and manifest["schema"] == "carr-final-scale-artifact-selection/1"
            and manifest["repository"] == REPOSITORY and manifest["target"] == TARGET
            and manifest["run_id"] == RUN and type(manifest["artifacts"]) is list
            and 1 <= len(manifest["artifacts"]) <= 12, "Invalid fixed artifact selection.")
    seen, seen_jobs, seen_artifacts = set(), set(), set()
    for item in manifest["artifacts"]:
        require(set(item) == {"job_id", "artifact_id", "tier", "os", "conclusion", "size_in_bytes", "sha256"}
                and item["tier"] in TIERS and item["os"] in ("ubuntu-24.04", "windows-2022")
                and item["conclusion"] in ("success", "failure", "cancelled", "timed_out")
                and type(item["job_id"]) is int and item["job_id"] > 0
                and type(item["artifact_id"]) is int and item["artifact_id"] > 0
                and type(item["size_in_bytes"]) is int and 0 < item["size_in_bytes"] <= MAX_ZIP
                and type(item["sha256"]) is str and len(item["sha256"]) == 64
                and all(c in "0123456789abcdef" for c in item["sha256"]),
                "Artifact selection is not finite or canonical.")
        key = (item["tier"], item["os"])
        require(key not in seen and item["job_id"] not in seen_jobs
                and item["artifact_id"] not in seen_artifacts, "An artifact/job/tier identity is repeated.")
        seen.add(key)
        seen_jobs.add(item["job_id"])
        seen_artifacts.add(item["artifact_id"])
    source = source_identity()
    run = api("/repos/" + REPOSITORY + "/actions/runs/" + str(RUN))
    require(run["head_sha"] == TARGET and run["event"] == "pull_request",
            "The selected original workflow run differs.")
    output = Path("capacity-harvest-output")
    output.mkdir(exist_ok=False)
    try:
        for item in manifest["artifacts"]:
            result, encoded = harvest(item, source)
            path = output / (item["tier"] + "-" + item["os"] + ".json")
            with path.open("xb") as stream:
                stream.write(encoded + b"\n")
            print("CARR_FINAL_SCALE_ARTIFACT " + encoded.decode("utf-8"), flush=True)
    finally:
        require(source_identity() == source and local_bytes(manifest_path) == manifest_raw
                and local_bytes(Path(__file__)) == self_raw,
                "The read-only harvester or exact target source changed.")
        print("CARR_FINAL_SCALE_HARVEST_SOURCE_UNCHANGED " + TARGET, flush=True)

if __name__ == "__main__":
    main()
