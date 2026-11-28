"""Read one fixed failed native artifact; never extract or execute its members."""
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
TARGET = "bdc3c6f1383f39e9c0b8e6f60c694a11a5fbb515"
RUN = 46130308277
JOB = 140935317017
JOB_NAME = "All eight final installed native scope contracts"
ARTIFACT = {
    "artifact_id": 15258462236,
    "size_in_bytes": 136227,
    "sha256": "3eb7a144b7b0e7fd7a77d58ff360f42af7e3844b663c3218153c9431635b68d2",
}
MIB = 1024**2
MAX_ZIP = 4*MIB
MAX_EXPANDED = 16*MIB
MAX_MEMBER = 4*MIB
MAX_JSON = 2*MIB
MAX_ENTRIES = 256
MAX_TEXT = 256*1024
MAX_SELECTED = 2*MIB
DEADLINE = time.monotonic() + 360
API = "https://api.github.com"

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

def output(directory, name, value):
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True).encode("ascii")
    require(len(raw) <= 4*MIB, "Diagnostic output exceeds its bound.")
    with (directory / name).open("xb") as stream:
        stream.write(raw)
    return raw

def main():
    require(sys.flags.isolated and not os.environ.get("PYTHONPATH"),
            "Use the isolated evidence reader.")
    self_path = Path(__file__)
    self_raw = local_bytes(self_path)
    relative = self_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    head = git("rev-parse", "HEAD").decode("ascii").strip()
    require(head == os.environ["CARR_HARVEST_HEAD"]
            and git("rev-parse", "HEAD^").decode("ascii").strip() == TARGET,
            "Evidence checkout differs from the exact selected parent and head.")
    require(git_blob(self_raw) == git("rev-parse", "HEAD:" + relative).decode("ascii").strip(),
            "Evidence reader differs from its committed blob.")
    prefix = "/repos/" + REPOSITORY
    run = api(prefix + "/actions/runs/" + str(RUN))
    require(run["id"] == RUN and run["head_sha"] == TARGET
            and run["status"] == "completed" and run["conclusion"] == "failure",
            "Selected original run identity or completed failure changed.")
    job = api(prefix + "/actions/jobs/" + str(JOB))
    require(job["id"] == JOB and job["run_id"] == RUN and job["head_sha"] == TARGET
            and job["name"] == JOB_NAME and job["status"] == "completed"
            and job["conclusion"] == "failure",
            "Selected original native job identity or failure changed.")
    metadata = api(prefix + "/actions/artifacts/" + str(ARTIFACT["artifact_id"]))
    require(metadata["id"] == ARTIFACT["artifact_id"] and not metadata["expired"]
            and metadata["size_in_bytes"] == ARTIFACT["size_in_bytes"]
            and metadata["digest"] == "sha256:" + ARTIFACT["sha256"]
            and metadata["name"] == "final-scope-native-" + TARGET
            and metadata["workflow_run"]["id"] == RUN
            and metadata["workflow_run"]["head_sha"] == TARGET,
            "Selected artifact differs from its exact run, head, size or digest.")
    directory = Path("native-artifact-evidence")
    directory.mkdir()
    with archive(ARTIFACT) as (zipped, expanded):
        infos = zipped.infolist()
        require(all(len(info.filename) <= 1024 for info in infos),
                "Artifact member name exceeds its bound.")
        listing = [dict(name=info.filename, bytes=info.file_size,
                        compressed_bytes=info.compress_size,
                        directory=info.is_dir()) for info in infos]
        listing_record = dict(schema="carr-native-artifact-listing/1", target=TARGET,
            run=RUN, job=JOB, artifact=ARTIFACT, expanded_bytes=expanded, members=listing,
            original_job_conclusion="failure", product_replayed=False)
        output(directory, "listing.json", listing_record)
        print("CARR_NATIVE_ARTIFACT_LISTING " + json.dumps(listing_record,
              ensure_ascii=True, allow_nan=False, sort_keys=True), flush=True)
        files = {info.filename: info for info in infos if not info.is_dir()}
        def resolve(logical):
            leaf = PurePosixPath(logical).name
            matches = [name for name in files
                       if name == logical or name == leaf or name.endswith("/" + logical)]
            require(len(matches) == 1, "Required diagnostic member is absent or ambiguous: " + logical)
            return matches[0]
        required = [resolve("native/speech-preparation.stderr.txt"), resolve("native/result.json")]
        stdout = sorted(name for name in files if name.endswith(".stdout.txt"))
        require(len(stdout) <= 16, "Native stdout file count exceeds its bound.")
        names = list(dict.fromkeys(required + stdout))
        captured, summaries, total = {}, {}, 0
        for name in names:
            check()
            info = files[name]
            require(info.file_size <= MAX_TEXT, "Selected text exceeds its member bound.")
            total += info.file_size
            require(total <= MAX_SELECTED, "Selected texts exceed their total bound.")
            with zipped.open(info) as stream:
                raw = stream.read(MAX_TEXT + 1)
            require(len(raw) == info.file_size, "Selected diagnostic member is truncated.")
            text_value = raw.decode("utf-8-sig", errors="strict")
            captured[name] = dict(bytes=len(raw), sha256=sha256(raw).hexdigest(), text=text_value)
            if name == required[1]:
                captured[name]["value"] = decode(raw)
            summaries[name] = dict(bytes=len(raw), sha256=sha256(raw).hexdigest(),
                text=text_value if name in required else "\n".join(text_value.splitlines()[-40:]))
        require(local_bytes(self_path) == self_raw
                and git("rev-parse", "HEAD").decode("ascii").strip() == head
                and git("rev-parse", "HEAD^").decode("ascii").strip() == TARGET,
                "Evidence reader or selected checkout changed.")
        result = dict(schema="carr-native-artifact-read/1", status="READ_ONLY_ARTIFACT_CAPTURED",
            target=TARGET, run=RUN, job=JOB, artifact=ARTIFACT, harvester_head=head,
            original_job_conclusion="failure", expanded_bytes=expanded,
            selected_bytes=total, members=captured,
            product_replayed=False, application_imported=False, extracted_members=False,
            qualification="DIAGNOSTIC_READ_ONLY; original native job remains FAILURE")
        output(directory, "capture.json", result)
        print("CARR_NATIVE_ARTIFACT_CAPTURE " + json.dumps(dict(
            schema=result["schema"], status=result["status"], target=TARGET, run=RUN, job=JOB,
            artifact=ARTIFACT, harvester_head=head, original_job_conclusion="failure",
            selected_bytes=total, members=summaries, product_replayed=False,
            qualification=result["qualification"]),
            ensure_ascii=True, allow_nan=False, sort_keys=True), flush=True)

if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        # Never print an exception object that could retain a signed download URL.
        print("CARR_NATIVE_ARTIFACT_READ_FAILED " + type(error).__name__, file=sys.stderr, flush=True)
        raise SystemExit(1) from None
