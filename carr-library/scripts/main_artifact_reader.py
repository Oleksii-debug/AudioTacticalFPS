"""Inert, source-bound reader for one completed full-main artifact.

The HTTPS provenance/ZIP transport is adapted from the reviewed b277 reader.
No archive member is extracted, imported, evaluated or executed. Product and
test modules are read only as bytes from the exact reviewed source checkout.
"""
from __future__ import annotations

import argparse
import ast
import base64
from collections import Counter
import csv
from email.parser import BytesParser
import fnmatch
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

REPOSITORY = "Oleksii-debug/AudioTacticalFPS"
ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "carr-library"
PLAN_PATH = "carr-library/docs/validation/FINAL_MAIN_ARTIFACT_READ_PLAN_20261127.json"
CONFIG_PATH = PROJECT / "scripts/main_artifact_reader.json"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
BLOB_CACHE = {}
SUMMARY = {"schema": "carr-full-main-inert-read/1", "status": "FAIL",
           "archive_read": "NOT_RUN", "application_evidence": "NOT_RUN",
           "no_archive_execution": True, "phases": {}, "members": {}}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def same(actual, expected, reason):
    require(type(actual) is type(expected) and actual == expected, reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def counter_digest(value):
    return digest(canonical(dict(sorted(value.items()))).encode("utf-8"))


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "Duplicate JSON key.")
        value[key] = item
    return value


def nonfinite(value):
    raise ValueError("Nonfinite JSON number.")


def json_bytes(raw):
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                      parse_constant=nonfinite)


def relative(name):
    require(type(name) is str and bool(name) and len(name) <= 4096,
            "Invalid bounded member path.")
    require("\\" not in name and "\0" not in name and ":" not in name
            and not name.startswith("/") and not name.endswith("/")
            and all(part not in ("", ".", "..") for part in name.split("/")),
            "Noncanonical member path.")
    require(all(ord(char) >= 32 and ord(char) != 127 for char in name),
            "Control character in member path.")
    return name


def artifact_name(name):
    relative(name)
    if name.startswith("ci-results/"):
        name = name[len("ci-results/"):]
        relative(name)
    return name


def winpath(value):
    require(type(value) is str and value and len(value) <= 32768,
            "Missing bounded Windows origin.")
    path = PureWindowsPath(value)
    require(path.is_absolute() and ".." not in path.parts and
            not any(":" in part for part in path.parts[1:]),
            "Foreign or ambiguous Windows origin.")
    return path


def installed_origin(record, name, source, *, prefix=None):
    require(type(record) is dict and set(record) == {"path", "sha256"},
            "Unexpected installed origin fields: " + name)
    path = winpath(record["path"])
    parts = tuple(part.casefold() for part in path.parts)
    require(parts.count("site-packages") == 1, "Installed origin lacks exact site-packages.")
    at = parts.index("site-packages")
    require(at >= 1 and parts[at - 1] == "lib", "Unexpected installed package parent.")
    found_prefix = PureWindowsPath(*path.parts[:at - 1])
    if prefix is not None:
        require(found_prefix == winpath(prefix), "Installed origin changed its selected prefix.")
    relative_path, raw = source.module(name)
    expected_tail = tuple(relative_path.split("/"))
    require(tuple(path.parts[at + 1:]) == expected_tail,
            "Installed origin names another module: " + name)
    same(record["sha256"], digest(raw), "Installed module differs from exact source: " + name)
    return str(found_prefix)


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        if not url.startswith("https://"):
            raise ValueError("Only HTTPS artifact redirects are allowed.")
        result = super().redirect_request(request, fp, code, message, headers, url)
        result.remove_header("Authorization")
        return result


def read(url, maximum):
    require(url.startswith("https://api.github.com/repos/" + REPOSITORY + "/"),
            "Transport must start at this fixed GitHub repository.")
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.build_opener(Redirect).open(request, timeout=90) as response:
            raw = response.read(maximum + 1)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise ValueError("GitHub transport HTTP status " + str(code)) from None
    except urllib.error.URLError:
        raise ValueError("GitHub transport failed.") from None
    require(len(raw) <= maximum, "GitHub response exceeds its explicit byte bound.")
    return raw


def api(path, maximum=8 * 1024**2):
    return json_bytes(read("https://api.github.com/repos/" + REPOSITORY + path, maximum))


class Source:
    def __init__(self, config, *, remote=False):
        self.remote = remote
        self.commit = config["source_commit"]
        self.tree = config["source_tree"]
        commit = api("/git/commits/" + self.commit)
        same(commit["sha"], self.commit, "Source commit differs.")
        same(commit["tree"]["sha"], self.tree, "Source tree differs.")
        tree = api("/git/trees/" + self.tree + "?recursive=1")
        same(tree.get("truncated"), False, "Source tree is truncated.")
        same(tree["sha"], self.tree, "Source inventory tree differs.")
        self.entries = {row["path"]: row for row in tree["tree"]}
        require(len(self.entries) == len(tree["tree"]), "Duplicate source tree path.")
        self.used = {}
        self.cache = {}

    def raw(self, name):
        relative(name)
        entry = self.entries.get(name)
        require(entry is not None and entry["type"] == "blob"
                and entry["mode"] in ("100644", "100755"),
                "Missing exact regular source file: " + name)
        path = ROOT.joinpath(*name.split("/"))
        require(path.resolve().is_relative_to(ROOT.resolve()),
                "Source path escaped the checked-out repository.")
        raw = BLOB_CACHE.get(entry["sha"]) if self.remote else None
        local_bytes = None
        if raw is None and path.is_file():
            local_bytes = path.read_bytes()
            local_blob = hashlib.sha1(
                b"blob " + str(len(local_bytes)).encode("ascii") + b"\0" + local_bytes).hexdigest()
            if local_blob == entry["sha"]:
                raw = local_bytes
                self.cache[name] = raw
        if raw is None and self.remote:
            blob = api("/git/blobs/" + entry["sha"], 8 * 1024**2)
            same(blob["sha"], entry["sha"], "Historical source blob differs.")
            same(blob["encoding"], "base64", "Unexpected historical blob encoding.")
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
            same(len(raw), blob["size"], "Historical source blob size differs.")
        require(raw is not None, "Local source differs from the actual candidate: " + name)
        expected_blob = hashlib.sha1(
            b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        same(expected_blob, entry["sha"], "Source blob content differs: " + name)
        BLOB_CACHE[entry["sha"]] = raw
        self.used[name] = {"blob": entry["sha"], "sha256": digest(raw), "bytes": len(raw)}
        return raw

    def json(self, name):
        return json_bytes(self.raw(name))

    def module(self, name):
        require(name == "bootstrap" or name.startswith("bootstrap.") or
                name == "carr_library" or name.startswith("carr_library."),
                "Unreviewed package origin.")
        require(all(part.isidentifier() for part in name.split(".")), "Invalid module identity.")
        package = name.replace(".", "/")
        for tail in (package + ".py", package + "/__init__.py"):
            source = "carr-library/" + ("src/" if name.startswith("carr_library") else "") + tail
            if source in self.entries:
                return tail, self.raw(source)
        raise ValueError("Exact module source is missing: " + name)

    def check_hashes(self, values, *, prefix="carr-library/", exact=None):
        require(type(values) is dict and bool(values), "Missing source digest inventory.")
        if exact is not None:
            require(set(values) == set(exact), "Source digest inventory changed.")
        for name, value in values.items():
            same(value, digest(self.raw(prefix + relative(name))), "Source digest differs: " + name)

    def unchanged(self):
        for name, raw in self.cache.items():
            require(ROOT.joinpath(*name.split("/")).read_bytes() == raw,
                    "A checked-out source file changed during inert reading.")


def zip_inventory(archive, limits, *, outer):
    entries = archive.infolist()
    require(len(entries) <= limits["members"], "Archive member count exceeds its bound.")
    require(sum(item.file_size for item in entries) <= limits["expanded_bytes"],
            "Archive expanded size exceeds its bound.")
    found, raw_names, aliases = {}, set(), set()
    for entry in entries:
        raw = entry.filename[:-1] if entry.is_dir() else entry.filename
        require(entry.orig_filename == entry.filename, "Archive filename has an embedded alias.")
        relative(raw)
        name = artifact_name(raw) if outer else raw
        alias = name.casefold()
        require(entry.filename not in raw_names and alias not in aliases,
                "Duplicate or normalized archive member.")
        raw_names.add(entry.filename)
        aliases.add(alias)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        require(not entry.flag_bits & 1, "Encrypted evidence member.")
        if entry.is_dir():
            require(mode in (0, stat.S_IFDIR) and entry.file_size == 0,
                    "Invalid archive directory entry.")
            continue
        require(mode in (0, stat.S_IFREG), "Nonregular evidence member.")
        found[name] = entry
    return found


class Evidence:
    def __init__(self, raw, plan, limits):
        self.archive = zipfile.ZipFile(io.BytesIO(raw))
        self.entries = zip_inventory(self.archive, limits, outer=True)
        self.plan = plan
        self.limits = limits
        self.cache = {}
        self.selected = set()

    def raw(self, name):
        relative(name)
        require(name in self.entries, "Required evidence member is absent: " + name)
        entry = self.entries[name]
        require(entry.file_size <= self.limits["member_bytes"],
                "Evidence member exceeds its unchanged read bound: " + name
                + " (" + str(entry.file_size) + " bytes)")
        if name not in self.cache:
            raw = self.archive.read(entry)
            same(len(raw), entry.file_size, "ZIP member length differs.")
            self.cache[name] = raw
            SUMMARY["members"][name] = {"bytes": len(raw), "sha256": digest(raw)}
        self.selected.add(name)
        return self.cache[name]

    def json(self, name):
        return json_bytes(self.raw(name))

    def text(self, name):
        raw = self.raw(name)
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        # Match producer Path.read_text universal-newline interpretation only.
        # raw() keeps the original bytes for every size and SHA256 comparison.
        return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")

    def terminal(self, name):
        values = []
        for line in self.text(name).splitlines():
            if line.lstrip().startswith("{"):
                value = json.loads(line, object_pairs_hook=unique, parse_constant=nonfinite)
                if type(value) is dict and value.get("status") in ("PASS", "FAIL", "NOT_RUN"):
                    values.append(value)
        require(len(values) == 1, "Expected exactly one terminal receipt: " + name)
        return values[0]

    def close(self):
        self.archive.close()


def function_counter(root):
    require(root.tag in ("testsuite", "testsuites"), "Invalid JUnit root.")
    cases = list(root.iter("testcase"))
    require(bool(cases), "JUnit has no executed test cases.")
    functions = Counter()
    failures = []
    for case in cases:
        module, name = case.get("classname", ""), case.get("name", "")
        require(module and name, "JUnit case identity is missing.")
        functions[module + "::" + name.partition("[")[0]] += 1
        for kind in ("failure", "error", "skipped"):
            element = case.find(kind)
            if element is not None:
                failures.append({"case": (module + "::" + name)[:512], "kind": kind,
                                 "detail": (element.text or "")[-1600:]})
    return functions, failures, len(cases)


def junit(raw):
    require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
            "DTD/entity declarations are not evidence.")
    class NoDTD(ET.TreeBuilder):
        def doctype(self, name, pubid, system):
            raise ValueError("DTD declarations are not evidence.")
    return function_counter(ET.fromstring(raw, parser=ET.XMLParser(target=NoDTD())))


def flatten(population):
    return Counter({module + "::" + name: count
                    for module, names in population.items() for name, count in names.items()})


def compare_counter(observed, expected, reason):
    if observed != expected:
        missing, extra = expected - observed, observed - expected
        raise ValueError(reason + "; missing=" + repr(dict(list(missing.items())[:12]))
                         + "; extra=" + repr(dict(list(extra.items())[:12])))


def bound_receipt(value, config, *, tree=True):
    same(value.get("source_commit"), config["source_commit"], "Receipt source commit differs.")
    if tree:
        same(value.get("source_tree"), config["source_tree"], "Receipt source tree differs.")


def metric(value, field):
    for part in field.split("."):
        value = value[part]
    return value


def positive(value, reason):
    require(type(value) in (int, float) and math.isfinite(value) and value > 0, reason)


def sha_value(value, reason):
    require(type(value) is str and HEX64.fullmatch(value) is not None, reason)


def phase(label, operation):
    try:
        result = operation()
        SUMMARY["phases"][label] = {"status": "PASS", **(result or {})}
    except Exception as error:
        SUMMARY["phases"][label] = {"status": "FAIL", "reason":
                                    type(error).__name__ + ": " + str(error)[:2400]}
    value = SUMMARY["phases"][label]
    print("PHASE", json.dumps({"phase": label, **value}, ensure_ascii=False, sort_keys=True), flush=True)
    return value["status"] == "PASS"


def wheel_basename(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", value) is not None,
            "Only a safe manifest-declared public wheel basename is admitted.")
    return value


def pattern(path, glob):
    relative(glob)
    require("**" not in glob.split("/"), "Recursive package-data patterns are not admitted.")
    a, b = path.split("/"), glob.split("/")
    return len(a) == len(b) and all(fnmatch.fnmatchcase(x, y) for x, y in zip(a, b))


def wheel_sources(project, source):
    recipe = project["tool"]["setuptools"]
    packages = recipe["packages"]
    require(type(packages) is list and packages and len(packages) == len(set(packages))
            and all(type(p) is str and all(x.isidentifier() for x in p.split(".")) for p in packages),
            "Expected explicit source package inventory.")
    require(not recipe.get("py-modules") and not recipe.get("exclude-package-data"),
            "Unsupported source package recipe.")
    directories, data = recipe.get("package-dir", {}), recipe.get("package-data", {})
    admitted, required = {}, set()
    for package in packages:
        parents = [key for key in directories
                   if key == "" or key == package or package.startswith(key + ".")]
        key = max(parents, key=len) if parents else ""
        base = directories.get(key, "")
        if base:
            relative(base)
        tail = package[len(key):].lstrip(".").replace(".", "/")
        directory = "/".join(x for x in (base, tail) if x)
        relative(directory)
        prefix = "carr-library/" + directory + "/"
        patterns = data.get("*", []) + data.get(package, [])
        require(type(patterns) is list and all(type(p) is str for p in patterns),
                "Invalid explicit package-data recipe.")
        for filename in source.entries:
            if not filename.startswith(prefix):
                continue
            suffix = filename[len(prefix):]
            runtime = "/" not in suffix and suffix.endswith((".py", ".sql"))
            if not runtime and not any(pattern(suffix, glob) for glob in patterns):
                continue
            if suffix.endswith((".pyc", ".pyo")) or "__pycache__" in suffix.split("/"):
                continue
            if suffix.endswith(".py") and not runtime:
                continue
            destination = package.replace(".", "/") + "/" + suffix
            require(destination not in admitted or admitted[destination] == filename,
                    "Ambiguous package destination.")
            admitted[destination] = filename
            if runtime or suffix.endswith(".sql"):
                required.add(destination)
    require(required, "No required runtime source was inventoried.")
    return admitted, required


def verify_wheel(raw, filename, source):
    wheel_basename(filename)
    project = tomllib.loads(source.raw("carr-library/pyproject.toml").decode("utf-8"))
    metadata = project["project"]
    name, version = metadata["name"], metadata["version"]
    require(type(name) is str and type(version) is str, "Static source name/version required.")
    normalized = re.sub(r"[-_.]+", "_", name).lower()
    same(filename, normalized + "-" + version + "-py3-none-any.whl",
         "Public wheel name/version/tag differs from exact source.")
    admitted, required = wheel_sources(project, source)
    limits = {"members": 10000, "expanded_bytes": 128 * 1024**2}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        inventory = zip_inventory(archive, limits, outer=False)
        files = {}
        for name_in_wheel, entry in inventory.items():
            require(entry.file_size <= 4 * 1024**2, "Public wheel member exceeds its read bound.")
            require(not name_in_wheel.endswith((".pyc", ".pyo"))
                    and "__pycache__" not in name_in_wheel.split("/"),
                    "Public source wheel contains executable bytecode.")
            files[name_in_wheel] = archive.read(entry)
    prefix = normalized + "-" + version + ".dist-info/"
    record_name = prefix + "RECORD"
    require({prefix + n for n in ("METADATA", "WHEEL", "RECORD")} <= files.keys(),
            "Public wheel metadata is incomplete.")
    rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
    require(all(len(row) == 3 for row in rows), "Malformed wheel RECORD row.")
    records = {row[0]: row[1:] for row in rows}
    require(len(records) == len(rows) and set(records) == set(files), "Wheel RECORD inventory differs.")
    for path, (checksum, size) in records.items():
        if path == record_name:
            same([checksum, size], ["", ""], "Wheel RECORD must not claim a self-hash.")
            continue
        expected = base64.urlsafe_b64encode(hashlib.sha256(files[path]).digest()).decode("ascii").rstrip("=")
        same(checksum, "sha256=" + expected, "Public wheel RECORD hash differs.")
        same(size, str(len(files[path])), "Public wheel RECORD size differs.")
    parsed = BytesParser().parsebytes(files[prefix + "METADATA"])
    same(parsed.get_all("Name"), [name], "Public wheel metadata name differs.")
    same(parsed.get_all("Version"), [version], "Public wheel metadata version differs.")
    wheel_metadata = BytesParser().parsebytes(files[prefix + "WHEEL"])
    same(wheel_metadata.get_all("Root-Is-Purelib"), ["true"], "Only a pure source wheel is admitted.")
    same(wheel_metadata.get_all("Tag"), ["py3-none-any"], "Wheel tag differs.")
    require(admitted.keys() <= files.keys() and required <= files.keys(),
            "Public wheel omitted committed package bytes.")
    licenses = set()
    for glob in metadata.get("license-files", []):
        matches = {path[len("carr-library/"):] for path in source.entries
                   if path.startswith("carr-library/") and pattern(path[len("carr-library/"):], glob)}
        require(matches, "Declared source license has no matching file.")
        licenses.update(matches)
    expected_licenses = {prefix + "licenses/" + path for path in licenses}
    require(expected_licenses <= files.keys(), "Public wheel omitted a source license.")
    standard = {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt"}
    payload_count = 0
    for path, content in files.items():
        if path in admitted:
            source_name = admitted[path]
            payload_count += 1
        elif path in expected_licenses:
            source_name = "carr-library/" + path[len(prefix + "licenses/"):]
        elif path.startswith(prefix) and path[len(prefix):] in standard:
            continue
        else:
            raise ValueError("Public wheel contains undeclared payload: " + path)
        same(content, source.raw(source_name), "Public wheel payload differs from exact source: " + path)
    # Entry-point declarations are inert configuration, not executable imports.
    import configparser
    entries = configparser.ConfigParser(interpolation=None)
    entries.optionxform = str
    entries.read_string(files.get(prefix + "entry_points.txt", b"").decode("utf-8"))
    expected_groups = {
        group: metadata[key] for group, key in
        (("console_scripts", "scripts"), ("gui_scripts", "gui-scripts")) if metadata.get(key)}
    same(set(entries.sections()), set(expected_groups), "Wheel entry-point groups differ.")
    for group, values in expected_groups.items():
        same(dict(entries[group]), values, "Wheel entry points differ from committed metadata.")
    hashes = {name: digest(content) for name, content in files.items()
              if name.split("/")[0] in ("bootstrap", "carr_library")}
    return {"source_commit": source.commit, "source_tree": source.tree,
            "wheel_sha256": digest(raw), "project_name": name, "project_version": version,
            "payload_files": payload_count, "required_runtime_files": len(required),
            "expanded_bytes": sum(map(len, files.values())), "package_hashes": hashes}


def source_and_public_wheel(evidence, source, config):
    same(evidence.text("tested-commit.txt").strip(), config["source_commit"], "Tested commit differs.")
    same(evidence.text("tested-tree.txt").strip(), config["source_tree"], "Tested tree differs.")
    proof = evidence.json("source-wheel-proof.json")
    selected = evidence.json("tested-application-wheel.json")
    for receipt in (proof, selected):
        same(receipt.get("status"), "PASS", "Current public wheel source proof failed.")
        bound_receipt(receipt, config)
    same(selected.get("installed_bundle"), "PASS", "Public wheel was not copied from the installed seal.")
    filename = wheel_basename(selected["wheel"])
    raw = evidence.raw("tested-application-wheel/" + filename)
    same(digest(raw), selected["sha256"], "Retained B wheel differs from selection.")
    result = verify_wheel(raw, filename, source)
    for name in ("source_commit", "source_tree", "wheel_sha256", "project_name",
                 "project_version", "payload_files", "required_runtime_files"):
        same(proof.get(name), result[name], "Current wheel-source receipt differs: " + name)
    same(selected["sha256"], result["wheel_sha256"], "Copied current wheel hash differs.")
    return result


def source_selectors(source):
    workflow = source.raw(".github/workflows/carr-windows.yml").decode("utf-8")
    start = workflow.index("$suites = @(")
    end = workflow.index("\n          )", start)
    rows = re.findall(r"'([^']+)'", workflow[start:end])
    require(len(rows) == 451 and len(rows) == len(set(rows)), "Exact source selectors changed.")
    return rows


SOURCE_SUITE_RESULT = re.compile(
    r"(?<![A-Za-z0-9_])(PASS|FAIL): ([^\r\n]+): junit_passed=(\d+), failed=(\d+), skipped=(\d+), pytest_exit=(-?\d+)$",
    re.M)
SOURCE_SUITE_COMPLETE = re.compile(
    r"^Required suites complete: (\d+); passed=(\d+); failed=(\d+)$", re.M)


def source_population(evidence, source, config):
    oracle = source.json("carr-library/scripts/v3_acceptance_oracles.json")["source"]
    expected_modules = {module.replace(".", "/") + ".py": sum(functions.values())
                        for module, functions in oracle.items()}
    require(len(expected_modules) == 252 and sum(expected_modules.values()) == 5813,
            "Protected source oracle population differs.")
    text = evidence.text("integrated-tests.txt")
    # Parallel pytest progress can precede a complete verdict on the same line.
    # Exact selector/status/count Counters below still reject missing or extra rows.
    receipts = SOURCE_SUITE_RESULT.findall(text)
    selectors = source_selectors(source)
    compare_counter(Counter(row[1] for row in receipts), Counter(selectors),
                    "Source suite-result selector inventory differs.")
    failed_rows = [row for row in receipts if row[0] != "PASS"
                   or any(int(n) != 0 for n in row[3:]) or int(row[2]) <= 0]
    require(not failed_rows, "Required source suites failed: " + repr(failed_rows[:12]))
    summaries = SOURCE_SUITE_COMPLETE.findall(text)
    same(summaries, [("451", "451", "0")], "Source aggregate completion differs.")
    observed_totals = {row[1]: int(row[2]) for row in receipts}
    for selector, count in expected_modules.items():
        same(observed_totals.get(selector), count, "Protected source module count differs: " + selector)
    # Three parallel suites can interleave logs. Compare the complete function
    # Counter with its exact selector multiplicity; do not invent per-suite
    # boundaries or call repeated directory+file execution a unique population.
    starts = Counter()
    # Actual three-worker stdout can append CASE_START after another suite's
    # duration line. Match the bounded marker token, not a fabricated line boundary.
    for match in re.finditer(r"CASE_START ([A-Za-z0-9_./\\-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*)+)", text):
        label = match.group(1).replace("\\", "/")
        path, _, name = label.partition("::")
        module = path[:-3].replace("/", ".")
        if module not in oracle:
            continue
        require(len(label) < 240, "A protected source case label was truncated.")
        function = name.split("::")[-1]
        starts[module + "::" + function] += 1
    expected_starts = Counter()
    for module, functions in oracle.items():
        path = module.replace(".", "/") + ".py"
        multiplicity = sum(path == selector or path.startswith(selector.rstrip("/") + "/")
                           for selector in selectors)
        require(multiplicity > 0, "Protected source module is unselected.")
        for name, count in functions.items():
            expected_starts[module + "::" + name] = count * multiplicity
    compare_counter(starts, expected_starts, "Protected source CASE_START Counter differs.")
    proof = evidence.json("v3-additive-source-proof.json")
    bound_receipt(proof, config, tree=False)
    for key, value in {"status": "PASS", "required_modules": 252, "required_cases": 5813,
                       "cases": expected_modules}.items():
        same(proof.get(key), value, "Protected source receipt differs: " + key)
    return {"suites": len(receipts), "protected_modules": 252, "protected_cases": 5813,
            "executed_passed_including_overlap": sum(observed_totals.values()),
            "protected_function_counter_sha256": counter_digest(flatten(oracle)),
            "observed_case_start_counter_sha256": counter_digest(starts)}


def installed_population(evidence, source, config):
    oracle = source.json("carr-library/scripts/v3_acceptance_oracles.json")["installed"]
    observed, failures, total = junit(evidence.raw("full-bundle-workflows.xml"))
    require(not failures, "Installed JUnit has failure/error/skip: " + repr(failures[:6]))
    expected = flatten(oracle)
    selected = Counter({key: count for key, count in observed.items()
                        if key.split("::", 1)[0] in oracle})
    compare_counter(selected, expected, "Protected installed function Counter differs.")
    require(len(oracle) == 239 and sum(expected.values()) == 5485,
            "Protected installed oracle changed.")
    proof = evidence.json("v3-additive-installed-proof.json")
    bound_receipt(proof, config, tree=False)
    for key, value in {"status": "PASS", "installed_package": True, "required_cases": 5485,
                       "cases": {module: sum(rows.values()) for module, rows in oracle.items()}}.items():
        same(proof.get(key), value, "Protected installed receipt differs: " + key)
    full = evidence.json("full-bundle-workflows.json")
    for key, value in {"status": "PASS", "passed": total, "failed": 0, "skipped": 0}.items():
        same(full.get(key), value, "Full installed aggregate differs: " + key)
    return {"all_executed_cases": total, "protected_modules": 239, "protected_cases": 5485,
            "complete_function_counter_sha256": counter_digest(observed),
            "protected_function_counter_sha256": counter_digest(selected)}


def read_json_lines(text):
    return [json.loads(line, object_pairs_hook=unique, parse_constant=nonfinite)
            for line in text.splitlines() if line.lstrip().startswith("{")]


def native_kind(evidence, source, config, oracle, row, ffmpeg_sha):
    kind, path = row["kind"], row["path"]
    spec = oracle[kind]
    value = evidence.json(path)
    bound_receipt(value, config)
    for key, expected in {"schema": 1, "status": "PASS", "kind": kind,
                          "actual_windows": True, "isolated": True,
                          "installed_package": True, "full_platform": "NOT_CLAIMED"}.items():
        same(value.get(key), expected, "Native outer receipt differs: " + kind + "/" + key)
    origins = value["module_origins"]
    require(type(origins) is dict and set(origins) == set(spec["modules"]),
            "Native outer installed module set differs: " + kind)
    prefixes = {installed_origin(record, name, source) for name, record in origins.items()}
    require(len(prefixes) == 1, "Native modules use different installed prefixes.")
    same(value["probe_sha256"], digest(source.raw("carr-library/" + spec["probe"])),
         "Native probe source differs: " + kind)
    report = value["native"]
    for key, expected in spec["fields"].items():
        same(report.get(key), expected, "Native inner receipt differs: " + kind + "/" + key)
    expected = Counter(spec["checks"])
    if type(report.get("checks")) is list:
        require(all(type(item) is str for item in report["checks"]), "Native check must be a string.")
        actual = Counter(report["checks"])
    elif type(report.get("checks")) is int:
        log_name = path[:-5] + ".txt"
        text = evidence.text(log_name)
        actual = Counter(line[6:] for line in text.splitlines() if line.startswith("PASS: "))
        same(report["checks"], sum(expected.values()), "Native integer total differs.")
        wrappers = [item for item in read_json_lines(text)
                    if type(item) is dict and item.get("kind") == kind]
        same(wrappers, [value], "Retained native wrapper differs from its JSON receipt.")
        inner = [item for item in read_json_lines(text)
                 if type(item) is dict and "kind" not in item and item.get("status") in ("PASS", "FAIL", "NOT_RUN")]
        same(inner, [report], "Retained native terminal differs.")
    else:
        raise ValueError("Missing native check Counter: " + kind)
    compare_counter(actual, expected, "Native exact label Counter differs: " + kind)
    same(value["checks"], sum(expected.values()), "Native outer total differs.")
    if "probe_modules" in spec:
        internal = report.get("module_origins")
        require(type(internal) is dict and set(internal) == set(spec["probe_modules"]),
                "Native inner source inventory differs: " + kind)
        for name, record in internal.items():
            same(record, origins[name], "Native inner and outer source identities differ.")
    elif "module_origins" in report:
        raise ValueError("Unexpected native inner source inventory without an oracle.")
    if "source_commit" in report:
        same(report["source_commit"], config["source_commit"], "Native inner commit differs.")
    for field in spec.get("positive_metrics", []):
        positive(metric(report, field), "Native positive metric is invalid: " + field)
    for field in spec.get("digest_fields", []):
        sha_value(metric(report, field), "Native digest is invalid: " + field)
    for field, path_in_source in spec.get("source_digest_fields", {}).items():
        same(metric(report, field), digest(source.raw("carr-library/" + path_in_source)),
             "Native fixture digest differs: " + field)
    for left, right in spec.get("equal_fields", []):
        same(metric(report, left), metric(report, right), "Native paired identities differ.")
    for field, count in spec.get("digest_lists", {}).items():
        values = metric(report, field)
        require(type(values) is list and len(values) == count and len(set(values)) == count,
                "Native digest list count differs.")
        for item in values:
            sha_value(item, "Invalid native digest-list member.")
    for field, expected_value in spec.get("exact_metrics", {}).items():
        actual_value = metric(report, field)
        require(type(actual_value) in (int, float) and math.isfinite(actual_value)
                and actual_value == expected_value, "Native exact metric differs: " + field)
    if "runtime_digest_field" in spec:
        same(spec["runtime_environment"], "CARR_FFMPEG", "Unknown native runtime binding.")
        same(metric(report, spec["runtime_digest_field"]), ffmpeg_sha,
             "Native codec digest differs from the selected runtime.")
    if kind in ("live", "live-playback"):
        original = "Prefix 😀.\r\nSelected source sentence 🦉. Second source sentence.\nLast paragraph."
        same(report["actual_sapi_pcm"]["source_sha256"], digest(original.encode("utf-8")),
             "Native live source fixture differs.")
    return {"checks": sum(expected.values()), "label_counter_sha256": counter_digest(actual),
            "modules": len(origins), "installed_prefix": next(iter(prefixes))}


def reader_interactive(evidence, source, config):
    directory = "full-bundle-reader-interactive-native/"
    value = evidence.json(directory + "native-proof.json")
    same(value.get("status"), "PASS", "Reader-interactive aggregate failed.")
    bound_receipt(value, config)
    names = ["scripts/qualify_assistant_tools.py", "scripts/qualify_reader_appearance.py",
             "scripts/windows_reader_appearance_smoke.py", "scripts/windows_assistant_interactive_smoke.py",
             "scripts/reader_appearance_acceptance_oracle.json",
             "scripts/assistant_interactive_acceptance_oracle.json",
             "scripts/reader_interactive_native_gate.py"]
    source.check_hashes(value["source_files"], exact=names)
    same(set(value["native"]), {"appearance", "assistant"}, "Reader native families differ.")
    summaries = {}
    prefixes = set()
    for label, filename in (("appearance", "reader_appearance_acceptance_oracle.json"),
                            ("assistant", "assistant_interactive_acceptance_oracle.json")):
        spec = source.json("carr-library/scripts/" + filename)["native"]
        outer = value["native"][label]
        report = outer["receipt"]
        stdout = directory + label + ".stdout.txt"
        same(outer["stdout_sha256"], digest(evidence.raw(stdout)), "Reader native stdout hash differs.")
        same(evidence.terminal(stdout), report, "Reader terminal and retained receipt differ.")
        evidence.raw(directory + label + ".stderr.txt")
        for key, expected in spec["required_status"].items():
            same(report.get(key), expected, "Reader native status differs: " + key)
        expected = Counter(spec["checks"])
        checks = report.get("checks")
        require(type(checks) is list, "Reader native check inventory missing.")
        if label == "appearance":
            contrast = report.get("high_contrast")
            require(contrast is None or type(contrast) is bool, "Invalid actual contrast state.")
            if contrast is False:
                expected.update(spec["when_high_contrast_false"])
            require(all(type(item) is str for item in checks), "Malformed appearance check.")
            actual = Counter(checks)
            same(report["check_count"], len(checks), "Appearance count differs.")
            text = "Exact original 😀.\r\nУкраїнський текст.\r\nThird line."
            same(report["source_sha256"], digest(text.encode("utf-8")), "Appearance Unicode source differs.")
            sha_value(report["project_sha256"], "Appearance project hash missing.")
        else:
            require(all(type(item) is dict and set(item) == {"name", "passed"}
                        and type(item["name"]) is str and item["passed"] is True for item in checks),
                    "Malformed or failed interactive assistant check.")
            actual = Counter(item["name"] for item in checks)
            metrics = report["metrics"]
            require(all(type(metrics.get(k)) is int and metrics[k] > 0 for k in ("frames", "sample_rate")),
                    "Interactive assistant PCM metrics missing.")
            text = "Prefix 😀.\r\nSelected source sentence for actual speech preparation. Another sentence."
            same(metrics["source_sha256"], digest(text.encode("utf-8")), "Interactive Unicode source differs.")
            sha_value(metrics["pcm_sha256"], "Interactive PCM hash missing.")
        compare_counter(actual, expected, "Reader native exact Counter differs: " + label)
        same(outer["checks"], len(checks), "Reader aggregate native count differs.")
        origins = report.get("module_origins")
        require(type(origins) is dict and set(origins) == set(spec["modules"]),
                "Reader native installed module set differs.")
        for name, record in origins.items():
            same(record["sha256"], digest(source.raw("carr-library/" + spec["modules"][name])),
                 "Reader source digest differs.")
            prefixes.add(installed_origin(record, name, source))
        summaries[label] = {"checks": len(checks), "counter_sha256": counter_digest(actual)}
    require(len(prefixes) == 1, "Reader probes used different installed prefixes.")
    return {"native": summaries, "source_files": len(names), "installed_prefix": next(iter(prefixes))}


def literal(source, path, name):
    matches = []
    for node in ast.parse(source.raw(path), filename=path).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            matches.append(ast.literal_eval(node.value))
    require(len(matches) == 1, "Expected one inert literal source declaration: " + name)
    return matches[0]


def runtime_boundaries(evidence, source):
    lock = source.raw("carr-library/packaging/windows-integrated-cp311.lock").decode("utf-8")
    pairs = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s]+)", lock, re.M)
    pins = dict(pairs)
    require(len(pairs) == len(pins) == 56, "Exact full runtime lock changed.")
    same(evidence.json("full-lock-source-inventory.json"), pins, "Full installed runtime pins differ.")
    runtime = evidence.json("audio-runtime.json")
    path = "carr-library/src/carr_library/audio/runtime.py"
    for key, constant in (("version", "FFMPEG_VERSION"), ("source_commit", "FFMPEG_SOURCE_COMMIT"),
                          ("archive_url", "FFMPEG_ARCHIVE_URL"),
                          ("archive_sha256", "FFMPEG_ARCHIVE_SHA256")):
        same(runtime.get(key), literal(source, path, constant), "Pinned codec provenance differs: " + key)
    files = literal(source, path, "ARCHIVE_MEMBERS")
    expected = {name: {"bytes": row[1], "sha256": row[2]} for name, row in files.items()}
    same(runtime["files"], expected, "Selected codec file inventory differs.")
    same(runtime["native_validation"]["status"], "PASS", "Selected codec native validation failed.")
    ffmpeg = literal(source, path, "FFMPEG_WINDOWS_SHA256")
    same(runtime["files"]["ffmpeg.exe"]["sha256"], ffmpeg, "Selected FFmpeg differs.")
    sbom = evidence.json("sealed-sbom.json")
    same(sbom.get("status"), "PASS", "SBOM seal failed.")
    same(sbom["sha256"], digest(source.raw("carr-library/packaging/windows-integrated-sbom.cdx.json")),
         "Sealed SBOM differs from exact source.")
    seal = evidence.json("full-bundle-verified.json")
    for key, expected_value in {"status": "PASS", "runtime_sbom": "VERIFIED",
            "manifest_unchanged": True, "sealed_files_verified": True, "model": "INCLUDED",
            "ocr": "INCLUDED", "private_native_runtime": "INCLUDED", "audio_runtime": "INCLUDED",
            "native_launcher": "INCLUDED", "nvda": "NOT_RUN"}.items():
        same(seal.get(key), expected_value, "Full bundle verification field differs: " + key)
    SUMMARY["runtime"] = {"pins": 56, "ffmpeg_sha256": ffmpeg, "codec_files": len(files),
        "sealed_sbom_sha256": sbom["sha256"],
        "physical_native_model_payload_rehash": "NOT_AVAILABLE_IN_THIS_ARTIFACT",
        "redistribution_or_corresponding_source_clearance": "NOT_CLAIMED"}
    return ffmpeg


def loopback(evidence, source, config):
    selector = "tests/loopback/test_local_planning.py"
    exact = "PASS: " + selector + ": junit_passed=14, failed=0, skipped=0, pytest_exit=0"
    for path in ("local-planning-loopback-source.txt", "full-bundle-local-planning-loopback.txt"):
        text = evidence.text(path)
        same(text.splitlines().count(exact), 1, "Required planning verdict differs: " + path)
    text = evidence.text("local-planning-loopback-source.txt")
    same(text.splitlines().count("Required suites complete: 1; passed=1; failed=0"), 1,
         "Planning source aggregate differs.")
    value = evidence.json("full-bundle-local-planning-loopback-proof.json")
    bound_receipt(value, config, tree=False)
    for key, expected in {"status": "PASS", "installed_package": True, "required_cases": 14,
            "failed": 0, "skipped": 0, "exact_selector": selector,
            "policy": "numeric loopback TCP only; no DNS or external access",
            "actual_local_model": "NOT_RUN", "nvda": "NOT_RUN"}.items():
        same(value.get(key), expected, "Planning installed receipt differs: " + key)
    source.check_hashes(value["staging_closure_sha256"],
        exact=["scripts/ci_check.py", "scripts/ci_watchdog.py", selector])
    prefix = installed_origin(value["installed_process_owner"], "carr_library.audio.windows_process", source)
    sha_value(value["manifest_sha256"], "Planning sealed manifest digest missing.")
    same(value["manifest_sha256"], evidence.json("full-bundle-native-runtime.json")["manifest_sha256"],
         "Planning proof names another full bundle seal.")
    return {"source": 14, "installed": 14, "staging_files": 3, "installed_prefix": prefix}


def translation(evidence, source, config, current_wheel):
    directory = "full-bundle-local-translation/"
    spec = source.json("carr-library/scripts/local_translation_oracles.json")
    old = source.json("carr-library/scripts/translation_scope_oracles.json")
    report = evidence.json(directory + "local-translation-gate.json")
    bound_receipt(report, config)
    for key, expected in {"status": "PASS", "source_cases": 324, "installed_cases": 324,
            "native_flows": 8, "local_loopback_cases_per_population": 38,
            "existing_cases_per_population": 286, "actual_google_api": "NOT_RUN",
            "actual_local_engine": "NOT_RUN", "translation_quality": "NOT_QUALIFIED",
            "nvda": "NOT_RUN", "production_models": "NOT_RUN"}.items():
        same(report.get(key), expected, "Translation aggregate differs: " + key)
    identity = report["installed_identity"]
    same(identity["isolated"], True, "Translation installed parent lost isolation.")
    same(identity["wheel_sha256"], current_wheel["wheel_sha256"], "Translation wheel differs.")
    expected_python = {path: value for path, value in current_wheel["package_hashes"].items()
                       if path.endswith(".py")}
    same(identity["files"], expected_python, "Translation installed full Python inventory differs.")
    same(set(identity["module_origins"]), {"carr_library", "bootstrap"}, "Translation package roots differ.")
    for name, path in identity["module_origins"].items():
        module_path, raw = source.module(name)
        installed_origin({"path": path, "sha256": identity["files"][module_path]}, name,
                         source, prefix=identity["prefix"])
    for key in ("source_commit", "source_tree", "wheel_sha256", "project_name",
                "project_version", "payload_files", "required_runtime_files"):
        same(report["wheel_source"][key], current_wheel[key], "Translation wheel source differs.")
    expected_paths = [module.replace(".", "/") + ".py"
                      for module in (*old["functions"], *spec["local_functions"])]
    expected_paths += ["scripts/local_translation_oracles.json", "scripts/translation_scope_oracles.json",
        "scripts/local_translation_acceptance.py", "scripts/translation_scope_acceptance.py",
        "scripts/windows_translation_v3_smoke.py", "scripts/windows_translation_local_smoke.py",
        "scripts/windows_translation_scope_smoke.py", "tests/reader/test_ebook_reader.py",
        "scripts/ci_check.py", "scripts/ci_watchdog.py", "packaging/source_wheel.py"]
    source.check_hashes(report["fixture_and_harness_sha256"], exact=expected_paths)
    phases = {}
    for group, expected in (("existing", old["functions"]), ("local", spec["local_functions"])):
        for mode in ("source", "installed"):
            name = group + "-" + mode
            observed, failed, total = junit(evidence.raw(directory + group + "/" + mode + "-junit.xml"))
            require(not failed, "Translation JUnit failed: " + name + " " + repr(failed[:4]))
            compare_counter(observed, flatten(expected), "Translation exact function Counter differs: " + name)
            phase_receipt = report["phases"][name]
            same(phase_receipt["cases"], total, "Translation phase count differs.")
            same(phase_receipt["functions"], expected, "Translation phase functions differ.")
            origins = evidence.json(directory + group + "/" + mode + "-origins.json")
            same(phase_receipt["origins"], origins, "Translation raw origins differ.")
            same(origins["isolated"], mode == "installed", "Translation child isolation differs.")
            same(set(origins["module_origins"]), {"bootstrap", "carr_library"}, "Translation child roots differ.")
            for module, record in origins["module_origins"].items():
                if mode == "installed":
                    installed_origin(record, module, source, prefix=identity["prefix"])
                else:
                    relative_module, raw = source.module(module)
                    same(record["sha256"], digest(raw), "Translation source module differs.")
                    path = winpath(record["path"])
                    tail = tuple((("carr-library/src/" if module == "carr_library"
                                   else "carr-library/") + relative_module).split("/"))
                    require(tuple(path.parts[-len(tail):]) == tail, "Translation source origin differs.")
            phases[name] = {"cases": total, "counter_sha256": counter_digest(observed)}
    same(set(report["phases"]), set(phases), "Translation phase population differs.")
    native_modules = {"carr_library.application", "carr_library.providers",
        "carr_library.assistant.translation", "carr_library.assistant.translation_state",
        "carr_library.assistant.translation_selection", "carr_library.assistant.translation_scope_state",
        "carr_library.assistant.translation_scope", "carr_library.ui.translation_scope_dialog",
        "carr_library.library_backup"}
    native = {}
    for local in (False, True):
        for mode in (None, "document", "chapter", "batch"):
            label = ("local-" if local else "existing-") + (mode or ("selected" if local else "selected-advanced"))
            basename = "native-" + ("local-" if local else "") + (mode or ("selected" if local else "selected-advanced"))
            value = evidence.json(directory + basename + ".json")
            evidence.raw(directory + basename + ".txt")
            same(report["native"][label], value, "Translation retained native receipt differs.")
            expected = {"status": "PASS", "native_controls": True, "source_immutable": True,
                "quotation_authority": False, "actual_google_api": "NOT_RUN", "nvda": "NOT_RUN",
                "vault_cleanup": "PASS", "runtime_cleanup": "PASS", "native_event_loop": "owned-modal-unwound",
                "actual_credential_vault": "NOT_USED" if local else "PASS"}
            if local:
                expected.update(local_adapter="libretranslate-local", zero_remote_reservation=True,
                    credential_calls=0, actual_local_engine="NOT_RUN", translation_quality="NOT_QUALIFIED")
            for key, value_expected in expected.items():
                same(value.get(key), value_expected, "Translation native field differs: " + label + "/" + key)
            if mode:
                for key, value_expected in {"schema": "carr-native-scoped-translation/1", "mode": mode,
                        "windows": True, "isolated": True, "keyboard_focus": True,
                        "per_item_approval": True, "structure_preserved": True,
                        "documents": 2 if mode == "batch" else 1,
                        "translated_blocks": {"document": 2, "chapter": 1, "batch": 4}[mode],
                        "synthetic_provider_requests": {"document": 3, "chapter": 2, "batch": 5}[mode]}.items():
                    same(value.get(key), value_expected, "Scoped translation native field differs: " + key)
                same(value["fixture_helper_sha256"],
                     digest(source.raw("carr-library/tests/reader/test_ebook_reader.py")),
                     "Translation native fixture differs.")
                same(set(value["module_origins"]), native_modules, "Scoped translation modules differ.")
                for name, record in value["module_origins"].items():
                    installed_origin(record, name, source, prefix=identity["prefix"])
            else:
                for key, value_expected in {
                        "schema": "carr-native-local-translation/1" if local else "carr-native-translation-v3-smoke/1",
                        "synthetic_provider_requests": 2, "exact_reviewed_pair": "en->uk",
                        "directional_languages": True}.items():
                    same(value.get(key), value_expected, "Selected translation native field differs: " + key)
                if not local:
                    same(value.get("explicit_advanced_model"), True, "Advanced model selection missing.")
                    same(value.get("glossary_verified"), True, "Advanced glossary review missing.")
                installed_origin({"path": value["installed_module"],
                    "sha256": expected_python["carr_library/providers.py"]},
                    "carr_library.providers", source, prefix=identity["prefix"])
            native[label] = "PASS"
    same(set(report["native"]), set(native), "Translation native population differs.")
    firewall = evidence.json(directory + "firewall.json")
    bound_receipt(firewall, config)
    for key, expected in {"status": "PASS", "cleanup_status": "PASS", "owned_rule_absent": True}.items():
        same(firewall.get(key), expected, "Translation firewall cleanup differs.")
    for key in ("profiles_before", "profiles_restored", "profiles_enabled"):
        values = firewall[key]
        require(type(values) is list and len(values) == 3
                and {row["name"] for row in values} == {"Domain", "Private", "Public"},
                "Translation firewall profile inventory differs.")
    before = {row["name"]: row["enabled"] for row in firewall["profiles_before"]}
    after = {row["name"]: row["enabled"] for row in firewall["profiles_restored"]}
    same(before, after, "Translation firewall profiles were not restored.")
    require(all(row["enabled"] == "True" for row in firewall["profiles_enabled"]),
            "Translation firewall was not enabled.")
    for key, expected in {"direction": "Outbound", "action": "Block", "enabled": "True"}.items():
        same(firewall["actual_rule"][key], expected, "Translation firewall rule differs.")
    require(winpath(firewall["actual_rule"]["program"]) == winpath(identity["prefix"]) / "Scripts/python.exe",
            "Translation firewall guarded another interpreter.")
    return {"source": 324, "installed": 324, "phases": phases, "native_flows": 8,
            "installed_prefix": identity["prefix"], "owned_firewall_cleanup": "PASS"}


def require_flags(value, true_names=(), false_names=()):
    for key in true_names:
        same(value.get(key), True, "Required true evidence flag differs: " + key)
    for key in false_names:
        same(value.get(key), False, "Required false evidence flag differs: " + key)


def standard(evidence, source, config, current_wheel):
    installer_dir, update_dir = "standard-installer/", "standard-update/"
    inputs = evidence.json(update_dir + "inputs.json")
    same(inputs["status"], "verified-inputs", "Standard generation inputs were not verified.")
    for key, expected in {"candidate_source": config["source_commit"], "candidate_tree": config["source_tree"],
            "original_source": "91332206578b4d8dc779fec08c8d51a67962b0ff",
            "previous_source": "3cefda9cc3be02a96d4e40b291645f1bfc1fcc6f"}.items():
        same(inputs[key], expected, "Standard input source differs: " + key)
    same(inputs["dependency_lock_sha256"],
         digest(source.raw("carr-library/packaging/windows-integrated-cp311.lock")),
         "Standard dependency lock differs.")
    same(inputs["dependency_sbom_sha256"],
         digest(source.raw("carr-library/packaging/windows-integrated-sbom.cdx.json")),
         "Standard dependency SBOM differs.")
    require_flags(inputs, ["synthetic_publisher_and_anchor"],
                  ["production_authority_qualified", "authenticode_verified", "nvda_verified"])
    installer = evidence.json(installer_dir + "native.json")
    installer_checks = ["active-application-blocks-uninstall", "failed-bootstrap-preserves-previous",
                        "install", "repair", "uninstall", "failed-environment-preserved"]
    installer_true = ["old_process_survived_repair", "active_application_blocks_uninstall",
        "failed_bootstrap_preserves_previous_registration", "failed_environment_preserved",
        "research_and_unknown_files_preserved", "one_start_menu_entry",
        "native_install_repair_uninstall", "actual_installed_app_self_test"]
    installer_false = ["authenticode_verified", "nvda_verified"]
    same(set(installer), set(["schema", "status", "source_commit", "installer_sha256",
        "manifest_sha256", "failed_installer_sha256", "checks"] + installer_true + installer_false),
        "Ordinary installer exact receipt fields differ.")
    same(installer["schema"], 1, "Ordinary installer schema differs.")
    same(installer["status"], "passed", "Ordinary installer failed.")
    bound_receipt(installer, config, tree=False)
    require(type(installer["checks"]) is list and all(type(x) is str for x in installer["checks"]),
            "Malformed ordinary installer check inventory.")
    compare_counter(Counter(installer["checks"]), Counter(installer_checks), "Ordinary installer checks differ.")
    require_flags(installer, installer_true, installer_false)
    same(installer["installer_sha256"], inputs["candidate_setup_sha256"], "Candidate setup identities differ.")
    same(installer["manifest_sha256"], inputs["candidate_manifest_sha256"], "Candidate manifest identities differ.")
    same(digest(evidence.raw(installer_dir + "payload-manifest.json")), installer["manifest_sha256"],
         "Retained current standard manifest hash differs.")
    require(installer["installer_sha256"] != installer["failed_installer_sha256"],
            "Negative bootstrap setup is not distinct.")
    installer_inputs = evidence.json(installer_dir + "inputs.json")
    bound_receipt(installer_inputs, config)
    same(installer_inputs["application_wheel_sha256"], current_wheel["wheel_sha256"],
         "Ordinary installer application wheel differs.")
    same(installer_inputs["existing_full_manifest_sha256"], inputs["existing_full_manifest_sha256"],
         "Standard phases refer to different original full seals.")
    installer_proof = evidence.json(installer_dir + "proof.json")
    for key, value in installer.items():
        same(installer_proof.get(key), value, "Ordinary installer proof changed its native receipt.")
    bound_receipt(installer_proof, config)
    same(installer_proof["check_count"], 6, "Ordinary installer proof count differs.")
    require_flags(installer_proof, ["existing_full_sealed_payload_unchanged"])
    same(installer_proof["audio_transfer_guard"], "ENFORCED", "Standard audio transfer boundary missing.")
    source.check_hashes(installer_proof["source_files_sha256"], exact=[
        "scripts/ci_standard_installer.ps1", "packaging/build_standard_installer.py",
        "packaging/probe_standard_installer_windows.py", "packaging/standard_windows.iss.in",
        "tests/packaging/build_standard_failure_fixture.py"])

    native = evidence.json(update_dir + "native.json")
    checks = ["installed-module-origin", "two-distinct-source-bound-setups", "native-installed-a",
        "running-a-retains-shared-lease", "actual-tuf-verification",
        "native-b-health-rejection-rolls-back-a", "exact-activation-metadata-rollback",
        "rejected-b-retained", "verified-library-backup", "actual-standard-a-to-b",
        "old-process-and-sealed-a-retained", "protected-anchor-b", "actual-b-self-test",
        "actual-active-uninstall-refusal", "quiescent-uninstall-preserves-user-data"]
    true_flags = ["actual_two_compiled_generations", "actual_native_activation", "actual_guarded_rollback",
        "actual_old_process_preserved", "synthetic_publisher_and_anchor", "actual_windows_vault",
        "actual_restart_recovery"]
    false_flags = ["production_authority_qualified", "authenticode_verified", "nvda_verified"]
    fields = ["schema", "status", "checks", "previous_source", "candidate_source",
        "previous_setup_sha256", "candidate_setup_sha256", "previous_manifest_sha256",
        "candidate_manifest_sha256", "verified_target_sha256", "backup_sha256",
        "restart_recovery_proof_sha256", "system_helper", "maintenance"]
    same(set(native), set(fields + true_flags + false_flags), "Managed native receipt fields differ.")
    same(native["schema"], 1, "Managed native schema differs.")
    same(native["status"], "passed", "Managed native update failed.")
    require(type(native["checks"]) is list and all(type(x) is str for x in native["checks"]),
            "Malformed managed check inventory.")
    compare_counter(Counter(native["checks"]), Counter(checks), "Managed update checks differ.")
    require_flags(native, true_flags, false_flags)
    for key in ("previous_source", "candidate_source", "candidate_setup_sha256", "candidate_manifest_sha256"):
        same(native[key], inputs[key], "Managed generation binding differs: " + key)
    helper = native["system_helper"]
    helper_numbers = ["device", "inode", "bytes", "mtime_ns", "mode", "attributes", "links"]
    same(set(helper), set(["selection", "path"] + helper_numbers), "System helper metadata fields differ.")
    same(helper["selection"], "GetSystemDirectoryW/fixed-WindowsPowerShell-v1.0",
         "System helper selection differs.")
    winpath(helper["path"])
    require(all(type(helper[key]) is int and helper[key] >= 0 for key in helper_numbers),
            "System helper integer metadata invalid.")
    require(all(helper[key] > 0 for key in ("inode", "bytes", "mtime_ns", "links"))
            and stat.S_ISREG(helper["mode"]) and not stat.S_ISLNK(helper["mode"])
            and helper["attributes"] & 0x400 == 0, "System helper regular-file metadata invalid.")
    maintenance = evidence.json(update_dir + "original-maintenance.json")
    same(native["maintenance"], maintenance, "Independent maintenance receipt differs.")
    maintenance_checks = ["exact-original-source-installed",
        "ordinary-maintenance-installer-without-update-authority",
        "original-process-and-sealed-generation-preserved",
        "maintenance-native-self-test-and-user-data-preserved"]
    maintenance_true = ["actual_original_process_preserved",
                        "actual_original_sealed_generation_preserved", "user_data_preserved"]
    same(set(maintenance), set(["status", "original_source", "original_setup_sha256",
        "original_manifest_sha256", "previous_source", "checks"] + maintenance_true),
        "Maintenance exact receipt fields differ.")
    same(maintenance["status"], "passed", "Ordinary maintenance failed.")
    same(maintenance["checks"], maintenance_checks, "Maintenance four ordered checks differ.")
    require_flags(maintenance, maintenance_true)
    for key in ("original_source", "previous_source"):
        same(maintenance[key], inputs[key], "Maintenance source differs.")

    candidate = evidence.json(update_dir + "candidate-application-wheel.json")
    same(evidence.json(update_dir + "original-candidate-application-wheel.json"), candidate,
         "Original/maintenance builder candidate wheel receipts differ.")
    for key in ("source_commit", "source_tree", "wheel_sha256", "project_name",
                "project_version", "payload_files", "required_runtime_files"):
        same(candidate[key], current_wheel[key], "Standard B wheel receipt differs: " + key)
    historical = {}
    for label in ("original", "previous"):
        prefix = update_dir + label + "-application-wheel/"
        receipt = evidence.json(prefix + "manifest.json")
        same(receipt.get("schema"), 1, "Historical public wheel schema differs.")
        same(receipt.get("kind"), "tested-public-source-wheel", "Historical wheel purpose differs.")
        same(receipt["source_commit"], inputs[label + "_source"], "Historical wheel source differs.")
        same(receipt["candidate_source_commit"], config["source_commit"], "Historical wheel parent target differs.")
        same(receipt["candidate_wheel_sha256"], current_wheel["wheel_sha256"],
             "Historical wheel names another current B wheel.")
        filename = wheel_basename(receipt["wheel_file"])
        raw = evidence.raw(prefix + filename)
        same(len(raw), receipt["wheel_bytes"], "Historical retained wheel size differs.")
        same(digest(raw), receipt["wheel_sha256"], "Historical retained wheel digest differs.")
        previous_source = Source({"source_commit": inputs[label + "_source"],
                                  "source_tree": inputs[label + "_tree"]}, remote=True)
        verified = verify_wheel(raw, filename, previous_source)
        same(verified["expanded_bytes"], receipt["wheel_expanded_bytes"],
             "Historical wheel expanded byte count differs.")
        manifest_name = update_dir + label + "-manifest.json"
        manifest = evidence.json(manifest_name)
        same(manifest["source_commit"], inputs[label + "_source"], "Historical sealed source differs.")
        if "source_tree" in manifest:
            same(manifest["source_tree"], inputs[label + "_tree"], "Historical sealed tree differs.")
        same(manifest["files"]["wheelhouse/" + filename], digest(raw),
             "Historical sealed wheel binding differs.")
        require_flags(manifest, ["bundled_python", "bundled_model", "bundled_ocr",
                                 "bundled_native_runtime", "bundled_native_launcher"], ["bundled_audio_runtime"])
        build = evidence.json(update_dir + label + "-build.json")
        same(build["source_commit"], inputs[label + "_source"], "Historical compiled source differs.")
        same(build["status"], "compiled", "Historical setup was not compiled.")
        same(build["manifest_sha256"], digest(evidence.raw(manifest_name)), "Historical build manifest differs.")
        recorded = maintenance if label == "original" else native
        same(recorded[label + "_manifest_sha256"], digest(evidence.raw(manifest_name)),
             "Historical native manifest digest differs.")
        same(recorded[label + "_setup_sha256"], build["installer"]["sha256"],
             "Historical compiled/native setup identities differ.")
        previous_source.unchanged()
        historical[label] = {key: verified[key] for key in ("source_commit", "source_tree",
            "wheel_sha256", "payload_files", "required_runtime_files")}
        historical[label]["source_files_verified"] = len(previous_source.used)
    require(len({current_wheel["wheel_sha256"], *(v["wheel_sha256"] for v in historical.values())}) == 3,
            "O/A/B public wheel identities are not distinct.")

    crash = evidence.json(update_dir + "native-restart-crash-after-activation.json")
    for key, expected in {"status": "controlled-process-exit-after-activation", "exit_code": 73,
                          "candidate_source": config["source_commit"]}.items():
        same(crash[key], expected, "Controlled restart receipt differs.")
    recovery = evidence.json(update_dir + "native-restart-recover.json")
    same(recovery["status"], "rolled-back", "Fresh restart did not roll back.")
    same(recovery["candidate_source"], config["source_commit"], "Restart source differs.")
    require_flags(recovery, ["actual_fresh_process", "actual_windows_vault", "fresh_tuf_verification"])
    same(native["restart_recovery_proof_sha256"],
         digest(evidence.raw(update_dir + "native-restart-recover.json")), "Restart raw receipt hash differs.")

    library = evidence.json(update_dir + "library.json")
    for key, expected in {"schema": "carr-standard-library-qualification/1", "status": "PASS",
            "source_commit": config["source_commit"], "manifest_sha256": inputs["candidate_manifest_sha256"],
            "passed": 50, "failed": 0, "skipped": 0}.items():
        same(library[key], expected, "Installed-library outer field differs: " + key)
    require_flags(library["firewall"], ["profiles_restored", "owned_rules_removed"])
    functional = library["qualification"]
    bound_receipt(functional, config)
    for key, expected in {"schema": "carr-installed-library-qualification/1", "status": "PASS",
            "manifest_sha256": inputs["candidate_manifest_sha256"], "wheel_sha256": current_wheel["wheel_sha256"],
            "passed": 50, "failed": 0, "skipped": 0, "expected_failures": False,
            "audio_payload": False, "manual_nvda": "NOT_RUN", "real_corpus_quality": "NOT_RUN"}.items():
        same(functional[key], expected, "Installed-library inner field differs: " + key)
    require_flags(functional, ["installed_package", "sealed_files_unchanged", "production_distributions_unchanged"])
    qualifier = "carr-library/scripts/qualify_standard_library.py"
    expected = literal(source, qualifier, "EXPECTED")
    suites = literal(source, qualifier, "SUITES")
    modules = literal(source, qualifier, "MODULES")
    proof_files = list(literal(source, qualifier, "PROOF_FILES")) + [s.partition("::")[0] for s in suites]
    same(functional["case_counts"], expected, "Library50 function inventory differs.")
    same(functional["suites"], suites, "Library50 selectors differ.")
    source.check_hashes(functional["qualification_source_hashes"], exact=proof_files)
    same(set(functional["origins"]), set(modules), "Library installed origin set differs.")
    prefixes = {installed_origin(record, name, source) for name, record in functional["origins"].items()}
    require(len(prefixes) == 1, "Library modules use different generation prefixes.")
    generation = winpath(library["generation"])
    require(winpath(next(iter(prefixes))) == generation / ".venv", "Library origin generation differs.")
    folders = {name.split("/")[1] for name in evidence.entries
               if re.fullmatch(r"standard-update/standard-library-[0-9a-f]{32}/installed-library\.json", name)}
    require(len(folders) == 1, "Installed-library result directory is missing or ambiguous.")
    folder = update_dir + next(iter(folders)) + "/"
    same(evidence.json(folder + "installed-library.json"), functional, "Raw library50 inner proof differs.")
    baseline = evidence.json(folder + "before-test-dependencies.json")
    for field in ("source_commit", "source_tree", "manifest_sha256", "wheel_sha256"):
        same(baseline[field], functional[field], "Library baseline source/seal changed.")
    same(baseline["origins"], functional["origins"], "Library baseline module identities changed.")
    same(baseline["source_hashes"], functional["qualification_source_hashes"], "Library baseline test source changed.")
    same(baseline["package_hashes"], {name: value for name, value in current_wheel["package_hashes"].items()
                                    if name.startswith("carr_library/")}, "Library baseline package bytes differ.")
    observed = Counter()
    for number, selector in enumerate(suites, 1):
        prefix = folder + "library-" + str(number)
        counts, failures, total = junit(evidence.raw(prefix + ".xml"))
        require(not failures, "Library child has failed/skipped cases: " + repr(failures[:4]))
        module = selector.partition("::")[0][:-3].replace("/", ".")
        compare_counter(counts, flatten({module: expected[module]}), "Library child exact Counter differs.")
        observed.update(counts)
        origins = evidence.json(prefix + "-origins.json")
        same(origins["origins"], functional["origins"], "Library child source identities differ.")
        if number == 1:
            same(origins["private_dlls"], functional["private_native_dlls"],
                 "Library actual model/native DLL receipt differs.")
    compare_counter(observed, flatten(expected), "Complete library50 Counter differs.")
    same(set(functional["private_native_dlls"]), {"msvcp140.dll", "msvcp140_1.dll"},
         "Library private native DLL population differs.")
    payload = evidence.json(installer_dir + "payload-manifest.json")
    for name, record in functional["private_native_dlls"].items():
        require(winpath(record["path"]) == generation / "native-runtime" / "bin" / name,
                "Library native DLL loaded outside its selected generation.")
        same(record["sha256"], payload["files"]["native-runtime/bin/" + name],
             "Library native DLL differs from sealed metadata.")

    child = evidence.json(update_dir + "native-library-process.json")
    same(set(child), {"schema", "source_commit", "manifest_sha256", "returncode",
                      "output_file", "output_bytes", "output_sha256"}, "Library child receipt fields differ.")
    for key, expected_value in {"schema": 1, "source_commit": config["source_commit"],
            "manifest_sha256": inputs["candidate_manifest_sha256"], "returncode": 0,
            "output_file": "native-library-process.log"}.items():
        same(child[key], expected_value, "Retained library child field differs.")
    child_bytes = evidence.raw(update_dir + child["output_file"])
    require(len(child_bytes) <= 2 * 1024**2, "Retained child log exceeds its producer bound.")
    same(child["output_bytes"], len(child_bytes), "Retained child length differs.")
    same(child["output_sha256"], digest(child_bytes), "Retained child digest differs.")

    proof = evidence.json(update_dir + "proof.json")
    for key, value in native.items():
        same(proof.get(key), value, "Managed proof changed its native receipt.")
    for key, expected_value in {"original_source": inputs["original_source"], "original_tree": inputs["original_tree"],
            "previous_tree": inputs["previous_tree"], "candidate_tree": config["source_tree"],
            "original_wheel_sha256": historical["original"]["wheel_sha256"],
            "previous_wheel_sha256": historical["previous"]["wheel_sha256"],
            "candidate_wheel_sha256": current_wheel["wheel_sha256"], "check_count": 15,
            "maintenance_check_count": 4, "actual_three_compiled_generations": True,
            "historical_original_direct_managed_update": "NOT_QUALIFIED",
            "existing_full_sealed_payload_unchanged": True, "audio_transfer_guard": "ENFORCED",
            "audio_free_installed_library_cases": 50}.items():
        same(proof[key], expected_value, "Managed final proof differs: " + key)
    for field, member in (("ordinary_maintenance_receipt_sha256", "original-maintenance.json"),
                          ("disk_budget_sha256", "disk-budget.json"),
                          ("audio_free_library_proof_sha256", "library.json")):
        same(proof[field], digest(evidence.raw(update_dir + member)), "Managed retained proof hash differs.")
    for key in ("original_setup_sha256", "original_manifest_sha256"):
        same(proof[key], maintenance[key], "Final original-generation identity differs.")
    source.check_hashes(proof["source_files_sha256"], exact=[
        "scripts/ci_standard_update.ps1", "packaging/probe_standard_update_windows.py",
        "packaging/probe_standard_recovery_child.py", "src/carr_library/security/standard_recovery.py",
        "scripts/qualify_standard_library.py", "scripts/qualify_standard_library.ps1",
        "src/carr_library/security/standard_installation.py", "src/carr_library/security/standard_windows.py"])
    return {"ordinary_installer": 6, "ordinary_maintenance": 4, "managed_update": 15,
            "installed_library": 50, "library_counter_sha256": counter_digest(observed),
            "historical_public_wheels": historical, "restart": "EXACT_RETAINED_ROLLBACK_RECEIPT",
            "compiled_setups_and_native_model_payloads": "NOT_RETAINED_NOT_INDEPENDENTLY_REHASHED"}


def shell_classification(evidence, source, config, plan):
    directory = ""
    value = evidence.json("standard-qualification-shell.json")
    bound_receipt(value, config)
    spec = plan["post_f169_candidate_overlay"]["shell_classification"]
    same(value["schema"], spec["schema"], "Shell classification schema differs.")
    same(value["status"], "PASS", "Shell classification failed.")
    compare_counter(Counter(value["checks"]), Counter(spec["checks"]), "Shell control Counter differs.")
    for key, expected in spec["expected"].items():
        same(value.get(key), expected, "Shell control field differs: " + key)
    observations = value["observations"]
    same([row["mode"] for row in observations], ["absent", "fixed", "poisoned"],
         "Shell observation modes differ.")
    for row in observations:
        name = "standard-qualification-shell-" + row["mode"] + ".log"
        same(row["output_file"], name, "Shell output names another member.")
        raw = evidence.raw(name)
        require(len(raw) <= 2 * 1024**2, "Shell log exceeds its producer bound.")
        same(row["output_bytes"], len(raw), "Shell raw length differs.")
        same(row["output_sha256"], digest(raw), "Shell raw hash differs.")
        require(type(row["returncode"]) is int, "Shell return code is not an integer.")
        text = raw.decode("utf-8", errors="replace")
        shells = [json.loads(line[len("CARR_SHELL "):], object_pairs_hook=unique, parse_constant=nonfinite)
                  for line in text.splitlines() if line.startswith("CARR_SHELL ")]
        same(shells, [row["shell"]], "Shell observed metadata differs from the raw log.")
        require(type(row["shell"]["version"]) is str and row["shell"]["version"].startswith("5.1."),
                "Expected fixed Windows PowerShell5.1.")
        children = [json.loads(line[len("CARR_PIPELINE_CHILD "):], object_pairs_hook=unique,
                               parse_constant=nonfinite)
                    for line in text.splitlines() if line.startswith("CARR_PIPELINE_CHILD ")]
        if row["mode"] == "absent":
            require(row["returncode"] != 0 and b"CantActivateDocumentInPipeline" in raw,
                    "Old absent-PATHEXT failure was not observed.")
            same(row["python_started"], False, "Absent control started Python.")
            same(row["shell"]["pathext"], ".CPL", "Absent control shell classification differs.")
            same(children, [], "Absent control emitted an unexpected Python marker.")
        else:
            same(row["returncode"], 0, "Fixed shell control failed.")
            same(row["python_started"], True, "Fixed shell control did not start Python.")
            same(row["shell"]["pathext"], ".EXE;.CPL", "Actual fixed-shell classification differs.")
            same(children, [row["child"]], "Retained Python marker differs.")
            for key, expected in {"status": "PASS", "os": "nt", "isolated": True,
                                  "no_bytecode": True}.items():
                same(row["child"][key], expected, "Shell child field differs.")
            require(type(row["child"]["pid"]) is int and row["child"]["pid"] > 0,
                    "Shell child process ID is missing.")
            require(winpath(row["child"]["python"]) == winpath(value["python"]),
                    "Shell child executable identity differs.")
            sha_value(row["tee_sha256"], "Unretained tee digest is missing.")
    hashes = value["source_sha256"]
    expected = {"packaging/probe_standard_update_windows.py",
                "scripts/qualify_standard_library.ps1",
                "scripts/standard_qualification_shell_acceptance.py",
                "installed/security/standard_windows.py"}
    same(set(hashes), expected, "Shell source inventory differs.")
    for name in expected:
        relative_source = ("src/carr_library/security/standard_windows.py"
                           if name == "installed/security/standard_windows.py" else name)
        same(hashes[name], digest(source.raw("carr-library/" + relative_source)),
             "Shell helper/product source differs.")
    qualifier = source.raw("carr-library/scripts/qualify_standard_library.ps1").decode("utf-8")
    start, stop = "function Invoke-Logged {\n", "\nfunction Assert-OfflineRules {"
    require(qualifier.count(start) == qualifier.count(stop) == 1,
            "The selected PowerShell logging function is ambiguous.")
    invoke = qualifier[qualifier.index(start):qualifier.index(stop, qualifier.index(start))]
    same(value["invoke_logged_sha256"], digest(invoke.encode("utf-8")), "Shell exact logging function differs.")
    winpath(value["fixed_shell"])
    winpath(value["python"])
    sha_value(value["fixed_shell_sha256"], "Shell executable hash missing.")
    sha_value(value["python_sha256"], "Shell Python hash missing.")
    return {"controls": 3, "log_hashes": "VERIFIED", "source_files": 4,
            "selected_executable_hashes": "RECORDED_RUNTIME_IDENTITIES_NOT_REHASHED",
            "private_tee_files": "NOT_RETAINED_NOT_INDEPENDENTLY_READ"}


def closed_members(evidence, plan):
    selected = plan["closed_selection"]
    overlay = plan["post_f169_candidate_overlay"]["closed_selection_additions"]
    roots = selected["root_exact_names"] + overlay["root_exact_names"]
    require(len(roots) == len(set(roots)) == 151, "Closed root file population differs.")
    mandatory = set(roots)
    mandatory.update("standard-installer/" + name for name in selected["standard_installer"])
    mandatory.update("standard-update/" + name for name in selected["standard_update"])
    mandatory.update("standard-update/" + name for name in overlay["standard_update_exact_names"])
    mandatory.update(selected["standard_additional"]["application_wheel_receipts"])
    translation = selected["translation"]
    mandatory.update(translation["directory"] + "/" + name for name in translation["root"])
    for group in translation["population_directories"]:
        mandatory.update(translation["directory"] + "/" + group + "/" + name
                         for name in translation["population_names"])
    mandatory.update(translation["directory"] + "/" + base + extension
                     for base in translation["native"]["basenames"]
                     for extension in translation["native"]["extensions"])
    readers = selected["reader_interactive"]
    mandatory.update(readers["directory"] + "/" + name for name in readers["names"])
    found = mandatory & evidence.entries.keys()
    missing = sorted(mandatory - evidence.entries.keys())
    selected_names = set(found)
    library_names = {"before-test-dependencies.json", "installed-library.json", "qualification.log",
                     "snapshot.log", "test-dependencies.log"}
    for number in range(1, 7):
        library_names.update("library-" + str(number) + suffix
                             for suffix in (".xml", "-origins.json", ".log"))
    for name in evidence.entries:
        parts = name.split("/")
        if len(parts) == 3 and parts[0] == "standard-update":
            if re.fullmatch(r"standard-library-[0-9a-f]{32}", parts[1]) and parts[2] in library_names:
                selected_names.add(name)
        if len(parts) == 2 and parts[0] == "standard-update":
            if re.fullmatch(r"native-managed-[A-Za-z0-9_-]+\.log", parts[1]):
                selected_names.add(name)
    SUMMARY["inventory"] = {"archive_members": len(evidence.entries),
        "closed_mandatory_members": len(mandatory), "missing_mandatory_members": missing,
        "selected_existing_before_manifest_wheels": len(selected_names),
        "unselected_count": len(evidence.entries) - len(selected_names),
        "oversized_selected": {name: evidence.entries[name].file_size for name in selected_names
                              if evidence.entries[name].file_size > evidence.limits["member_bytes"]}}
    # Hash every selected complete member before summarizing it. Never print
    # entire source/installed logs or replace a Counter check with a log suffix.
    for name in sorted(selected_names):
        raw = evidence.raw(name)
        if name.endswith(".json"):
            parsed = json_bytes(raw)
            SUMMARY["members"][name]["json_kind"] = type(parsed).__name__
        elif name.endswith(".xml"):
            counts, failed, total = junit(raw)
            SUMMARY["members"][name].update(cases=total, function_counter_sha256=counter_digest(counts),
                                            failed_or_skipped_cases=len(failed))
            if failed:
                SUMMARY["members"][name]["failure_examples"] = failed[:4]
        else:
            SUMMARY["members"][name]["text_lines"] = len(evidence.text(name).splitlines())
    require(not missing, "Mandatory main evidence is incomplete: " + repr(missing[:24]))
    return {"root_files": 151, "closed_mandatory_members": len(mandatory),
            "selected_complete_members_hashed": len(selected_names),
            "unselected_members_are_not_acceptance_evidence": True}


def self_check():
    """Four negative controls and one exact log-parser control, in memory only."""
    checks = []
    def refuses(label, action):
        try:
            action()
        except ValueError:
            checks.append(label)
        else:
            raise AssertionError("Reader negative control did not refuse: " + label)

    limits = {"members": 10000, "expanded_bytes": 512 * 1024**2, "member_bytes": 4 * 1024**2}
    parser_raw = (
        b".PASS: tests/ui/test_audio_studio_keyboard.py: junit_passed=4, failed=0, skipped=0, pytest_exit=0\r\n"
        b"Required suites complete: 451; passed=451; failed=0\r")
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("known.json", b'{"status":"PASS"}\n')
        archive.writestr("parser.txt", parser_raw)
    evidence = Evidence(memory.getvalue(), {}, limits)
    try:
        same(evidence.json("known.json"), {"status": "PASS"}, "Positive reader control failed.")
        refuses("missing-required-member", lambda: evidence.raw("absent.json"))
        same(evidence.raw("parser.txt"), parser_raw, "Log parsing changed retained raw bytes.")
        parsed = evidence.text("parser.txt")
        same(parsed, parser_raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"),
             "Universal-newline log interpretation differs.")
        same(SOURCE_SUITE_RESULT.findall(parsed),
             [("PASS", "tests/ui/test_audio_studio_keyboard.py", "4", "0", "0", "0")],
             "Complete appended verdict was not preserved.")
        same(SOURCE_SUITE_COMPLETE.findall(parsed), [("451", "451", "0")],
             "CRLF aggregate was not preserved.")
        same(SOURCE_SUITE_RESULT.findall(parsed.replace(".PASS:", "BYPASS:")), [],
             "Partial verdict word was incorrectly admitted.")
    finally:
        evidence.close()
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ci-results/known.json", b"{}")
        archive.writestr("known.json", b"{}")
    def duplicate():
        with zipfile.ZipFile(io.BytesIO(memory.getvalue())) as archive:
            zip_inventory(archive, limits, outer=True)
    refuses("duplicate-normalized-member", duplicate)
    compare_counter(Counter({"one": 2}), Counter({"one": 2}), "Positive Counter control failed.")
    refuses("wrong-function-multiplicity",
            lambda: compare_counter(Counter({"one": 1}), Counter({"one": 2}), "Counter control"))
    identity = {"source_commit": "a" * 40, "source_tree": "b" * 40}
    bound_receipt(dict(identity), identity)
    refuses("wrong-source-identity",
            lambda: bound_receipt({"source_commit": "c" * 40, "source_tree": "b" * 40}, identity))
    SUMMARY["members"].clear()
    print(json.dumps({"schema": "carr-full-main-reader-self-check/1", "status": "PASS",
                      "negative_controls": checks,
                      "positive_controls": ["appended-verdict-and-universal-newlines"],
                      "actual_application_execution": "NOT_RUN"},
                     sort_keys=True), flush=True)


def inspect(config):
    require(config["schema"] == "carr-full-main-reader-input/1", "Reader input schema differs.")
    for key in ("source_commit", "source_tree"):
        require(type(config.get(key)) is str and HEX40.fullmatch(config[key]) is not None,
                "An exact known candidate source identity is required.")
    for key in ("run_id", "job_id", "run_attempt"):
        require(type(config.get(key)) is int and config[key] > 0, "An actual run/job identity is required.")
    pin = config.get("artifact")
    require(type(pin) is dict and set(pin) == {"id", "name", "bytes", "sha256"},
            "Exact completed artifact fields are required.")
    require(type(pin["id"]) is int and pin["id"] > 0 and type(pin["bytes"]) is int and pin["bytes"] > 0
            and type(pin["name"]) is str and pin["name"]
            and type(pin["sha256"]) is str and HEX64.fullmatch(pin["sha256"]) is not None,
            "NOT_READY: actual completed artifact metadata is not pinned.")
    run = api("/actions/runs/" + str(config["run_id"]))
    for key, expected in (("id", config["run_id"]), ("head_sha", config["source_commit"]),
                          ("run_attempt", config["run_attempt"]), ("event", "pull_request"),
                          ("path", ".github/workflows/carr-windows.yml"), ("status", "completed")):
        same(run[key], expected, "Actual workflow run identity/state differs: " + key)
    jobs = api("/actions/runs/" + str(config["run_id"]) + "/attempts/"
               + str(config["run_attempt"]) + "/jobs?per_page=100")
    require(type(jobs["total_count"]) is int and jobs["total_count"] == len(jobs["jobs"]) <= 100,
            "Run job inventory is incomplete.")
    matching = [row for row in jobs["jobs"] if row["id"] == config["job_id"]]
    require(len(matching) == 1, "The exact main job is absent or ambiguous.")
    job = matching[0]
    same(job["head_sha"], config["source_commit"], "Main job source differs.")
    same(job["status"], "completed", "Main job is not complete.")
    SUMMARY["actual_run"] = {key: run.get(key) for key in ("id", "head_sha", "run_attempt", "event", "conclusion")}
    SUMMARY["actual_job"] = {key: job.get(key) for key in ("id", "name", "head_sha", "status", "conclusion")}
    source = Source(config)
    for path, blob in config["source_blobs"].items():
        same(source.entries.get(path, {}).get("sha"), blob, "Reviewed source binding differs: " + path)
        source.raw(path)
    plan = source.json(PLAN_PATH)
    for row in plan["post_f169_candidate_overlay"]["expected_source_bindings"]:
        same(source.entries[row["path"]]["sha"], row["blob"], "Plan source blob differs.")
        same(digest(source.raw(row["path"])), row["sha256"], "Plan source bytes differ.")
    extension_raw = ROOT.joinpath(*config["wheel_extension"]["path"].split("/")).read_bytes()
    same(digest(extension_raw), config["wheel_extension"]["sha256"], "Wheel-selection extension changed.")
    extension = json_bytes(extension_raw)
    same(extension["base_plan_blob"], config["source_blobs"][PLAN_PATH], "Wheel extension has another base plan.")
    same(extension["source_commit"], config["source_commit"], "Wheel extension has another source candidate.")
    same(extension["allowed_prefixes"], [
        "standard-update/original-application-wheel/",
        "standard-update/previous-application-wheel/"], "Wheel extension expands its exact prefixes.")
    limits = plan["existing_bounds"]
    same(config["bounds"], {key: limits[key] for key in ("archive_bytes", "members", "expanded_bytes", "member_bytes")},
         "Reader silently changed its inherited archive limits.")
    require(pin["bytes"] <= limits["archive_bytes"], "Actual artifact exceeds the inherited archive cap; review its measured size.")
    base = "/actions/artifacts/" + str(pin["id"])
    metadata = api(base, 65536)
    for key, expected in (("id", pin["id"]), ("name", pin["name"]), ("size_in_bytes", pin["bytes"]),
                          ("digest", "sha256:" + pin["sha256"]), ("expired", False)):
        same(metadata[key], expected, "Actual artifact metadata differs: " + key)
    same(metadata["workflow_run"]["id"], config["run_id"], "Artifact belongs to another run.")
    same(metadata["workflow_run"]["head_sha"], config["source_commit"], "Artifact belongs to another source.")
    raw = read("https://api.github.com/repos/" + REPOSITORY + base + "/zip", limits["archive_bytes"])
    same(len(raw), pin["bytes"], "Artifact bytes differ from completed metadata.")
    same(digest(raw), pin["sha256"], "Artifact digest differs from completed metadata.")
    SUMMARY["artifact"] = dict(pin)
    evidence = Evidence(raw, plan, limits)
    SUMMARY["archive_read"] = "PASS"
    try:
        phase("closed-selection-and-complete-member-hashes", lambda: closed_members(evidence, plan))
        state = {}
        def wheel_phase():
            state["wheel"] = source_and_public_wheel(evidence, source, config)
            return {key: value for key, value in state["wheel"].items() if key != "package_hashes"}
        phase("exact-public-B-wheel", wheel_phase)
        def runtime_phase():
            state["ffmpeg"] = runtime_boundaries(evidence, source)
            return SUMMARY["runtime"]
        phase("runtime-and-sealed-source-metadata", runtime_phase)
        phase("protected-source-population", lambda: source_population(evidence, source, config))
        phase("protected-installed-population", lambda: installed_population(evidence, source, config))
        native = source.json("carr-library/scripts/r7_native_oracles.json")
        same(set(native), {row["kind"] for row in plan["native_receipts"]}, "Native oracle family set changed.")
        same(len(native), 21, "Native family count changed.")
        for row in plan["native_receipts"]:
            phase("native-" + row["kind"], lambda row=row: native_kind(
                evidence, source, config, native, row, state["ffmpeg"]))
        phase("reader-interactive-native", lambda: reader_interactive(evidence, source, config))
        phase("numeric-loopback-planning", lambda: loopback(evidence, source, config))
        phase("local-and-existing-translation", lambda: translation(evidence, source, config, state["wheel"]))
        phase("ordinary-maintenance-managed-and-library", lambda: standard(evidence, source, config, state["wheel"]))
        phase("early-shell-classification", lambda: shell_classification(evidence, source, config, plan))
        def same_full_bundle():
            prefixes = {row["installed_prefix"] for row in SUMMARY["phases"].values()
                        if row["status"] == "PASS" and "installed_prefix" in row}
            require(len(prefixes) == 1, "Native/reader/planning/translation receipts name different full-bundle interpreters.")
            return {"one_installed_prefix": True}
        phase("same-full-bundle-native-origins", same_full_bundle)
        source.unchanged()
        SUMMARY["source_files_verified"] = source.used
        SUMMARY["selected_member_count"] = len(evidence.selected)
        all_pass = all(row["status"] == "PASS" for row in SUMMARY["phases"].values())
        SUMMARY["application_evidence"] = "PASS" if all_pass and job["conclusion"] == "success" else "FAIL"
        SUMMARY["status"] = SUMMARY["application_evidence"]
        SUMMARY["limits"] = [
            "This is an inert cross-check of one completed job's public receipts, not new application execution.",
            "Native executables, compiled setups, model/runtime bytes and private fixtures are not retained here and were not rehashed.",
            "Public wheel bytes and RECORD/source closure were checked without importing or installing a wheel.",
            "Unselected artifact members and absent receipts never qualify an omitted workflow.",
            "Private-runtime quality, manual NVDA, physical-audio devices, signing and redistribution clearance are not claimed.",
            "Source selector overlaps are reported separately from the5813/5485 protected populations.",
        ]
    finally:
        evidence.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("main-reader-results"))
    args = parser.parse_args()
    require(sys.flags.isolated and sys.flags.dont_write_bytecode, "Use isolated stdlib Python with -I -B.")
    if args.self_check:
        self_check()
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=False)
    config_raw = CONFIG_PATH.read_bytes()
    SUMMARY["reader_source_sha256"] = digest(Path(__file__).read_bytes())
    SUMMARY["reader_config_sha256"] = digest(config_raw)
    SUMMARY["reader_commit"] = os.environ.get("GITHUB_SHA")
    try:
        inspect(json_bytes(config_raw))
    except Exception as error:
        SUMMARY["status"] = "FAIL"
        SUMMARY["fatal"] = type(error).__name__ + ": " + str(error)[:2400]
    encoded = (json.dumps(SUMMARY, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    require(len(encoded) <= 2 * 1024**2, "Structured reader result exceeded its explicit output bound.")
    with (args.output_dir / "main-artifact-reader-result.json").open("xb") as stream:
        stream.write(encoded)
    compact = {key: SUMMARY[key] for key in
               ("schema", "status", "archive_read", "application_evidence", "no_archive_execution")}
    compact.update(result_sha256=digest(encoded), phases=len(SUMMARY["phases"]),
                   failed_phases=[key for key, value in SUMMARY["phases"].items() if value["status"] != "PASS"],
                   members_hashed=len(SUMMARY["members"]))
    if "fatal" in SUMMARY:
        compact["fatal"] = SUMMARY["fatal"]
    print("FINAL_INERT_READ", json.dumps(compact, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if SUMMARY["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
