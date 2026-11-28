"""Opt-in current-thread SQLite C-call observation for the library pilot only.

Native connections/cursors, SQL, bindings, transactions, progress handlers and
validation are never replaced. This module cannot execute a query itself.
CPython 3.11 positions identify exact reviewed AST call spans, not just lines.
"""
import ast
import dis
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import threading
from time import perf_counter_ns
from types import CodeType

SCHEMA = "carr-library-native-sqlite-observation/1"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SITES = 64
MAX_ROWS = 128
MAX_EVENTS = 4096
MAX_RECORD_BYTES = 256 * 1024
OPERATIONS = frozenset(("execute", "fetchone", "fetchmany", "fetchall"))


def _require(value, message):
    if not value:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _codes(code):
    yield code
    for item in code.co_consts:
        if isinstance(item, CodeType):
            yield from _codes(item)


def _span(node):
    return (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset)


def _position(instruction):
    value = instruction.positions
    return (value.lineno, value.end_lineno, value.col_offset, value.end_col_offset)


def _native_operation(argument):
    owner = getattr(argument, "__self__", None)
    name = getattr(argument, "__name__", None)
    if type(owner) is sqlite3.Connection and name == "execute":
        return name
    if type(owner) is sqlite3.Cursor and name in ("fetchone", "fetchmany", "fetchall"):
        return name
    return None


class NativeSQLiteObserver:
    """Finite observer of supplied source-bound functions; no query execution.

    Source hashes are explicit inputs, never inferred expectations. Ordinary
    diagnostic exceptions record INVALID without replacing the operation;
    process/user interruptions propagate. Calling code must refuse INVALID
    evidence even when the operation returns.
    """

    def __init__(self, functions, expected_sources, *, context_functions=(),
                 max_events=MAX_EVENTS, max_rows=MAX_ROWS):
        _require(sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 11)
                 and sys.flags.optimize == 0,
                 "Native SQLite observation requires ordinary CPython 3.11.")
        _require(type(max_events) is int and 1 <= max_events <= MAX_EVENTS
                 and type(max_rows) is int and 1 <= max_rows <= MAX_ROWS,
                 "Invalid finite SQLite observation bounds.")
        _require(type(expected_sources) is dict and 1 <= len(expected_sources) <= 4,
                 "An explicit bounded source-hash inventory is required.")
        functions, context_functions = tuple(functions), tuple(context_functions)
        all_functions = functions + context_functions
        _require(1 <= len(functions) <= 8 and len(context_functions) <= 8
                 and all(isinstance(getattr(f, "__code__", None), CodeType) for f in all_functions)
                 and len({id(f.__code__) for f in all_functions}) == len(all_functions),
                 "Invalid or repeated observed/context functions.")
        targets = {id(f.__code__) for f in functions}
        self._source_bytes, self._compiled, self._source_trees = {}, {}, {}
        self._known_codes, self._code_identity, self._sites, self._definitions = {}, {}, {}, {}
        self._code_objects = {}
        self._invocations = {}
        for supplied_path, digest in expected_sources.items():
            path = Path(supplied_path).resolve()
            _require(str(path) not in self._source_bytes
                     and type(digest) is str and len(digest) == 64
                     and all(c in "0123456789abcdef" for c in digest),
                     "Invalid or repeated source identity.")
            with path.open("rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            _require(len(raw) <= MAX_SOURCE_BYTES and sha256(raw).hexdigest() == digest,
                     "Observed source bytes differ from their explicit hash.")
            source = raw.decode("utf-8")
            self._source_bytes[str(path)] = (raw, digest)
            self._source_trees[str(path)] = ast.parse(source, filename=str(path))
            # The actual code filename is verified and used below when compiling.
            self._compiled[str(path)] = {}
        for function in all_functions:
            code = function.__code__
            filename = str(Path(code.co_filename).resolve())
            _require(filename in self._source_bytes,
                     "Observed function is outside the explicit source inventory.")
            raw, digest = self._source_bytes[filename]
            cache = self._compiled[filename]
            if code.co_filename not in cache:
                root = compile(raw, code.co_filename, "exec", dont_inherit=True, optimize=0)
                values = tuple(_codes(root))
                _require(len(values) <= 4096, "Source code-object inventory exceeds its bound.")
                cache[code.co_filename] = values
            matches = [candidate for candidate in cache[code.co_filename]
                       if candidate.co_qualname == code.co_qualname
                       and candidate.co_firstlineno == code.co_firstlineno]
            _require(len(matches) == 1 and code == matches[0],
                     "Observed function code does not match its source bytes.")
            identity = dict(filename=filename, source_sha256=digest,
                            function=code.co_qualname, first_line=code.co_firstlineno)
            self._known_codes[id(code)] = identity
            self._code_objects[id(code)] = code
            if id(code) not in targets:
                continue
            nodes = [node for node in ast.walk(self._source_trees[filename])
                     if isinstance(node, ast.FunctionDef) and node.name == code.co_name
                     and node.lineno == code.co_firstlineno and not node.decorator_list]
            _require(len(nodes) == 1, "Observed function has no unique ordinary AST definition.")
            self._code_identity[id(code)] = identity
            self._invocations[id(code)] = 0
            calls = {}
            for node in ast.walk(nodes[0]):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in OPERATIONS):
                    key = _span(node)
                    _require(key not in calls, "Ambiguous native-call source span.")
                    expression = ast.get_source_segment(raw.decode("utf-8"), node)
                    _require(expression is not None, "Native-call source span is unavailable.")
                    descriptor = dict(function=identity, operation=node.func.attr,
                        span=dict(line=key[0], end_line=key[1],
                                  column=key[2], end_column=key[3]),
                        expression_sha256=sha256(expression.encode("utf-8")).hexdigest(),
                        query_identity="source-expression-only; no expanded SQL or bindings")
                    if (node.func.attr == "execute" and node.args
                            and isinstance(node.args[0], ast.Constant)
                            and type(node.args[0].value) is str):
                        descriptor["literal_sql_sha256"] = sha256(
                            node.args[0].value.encode("utf-8")).hexdigest()
                        descriptor["query_identity"] = "static source SQL literal hash"
                    site_id = sha256(_canonical(descriptor)).hexdigest()
                    descriptor["site_id"] = site_id
                    descriptor["allowed_instruction_offsets"] = []
                    _require(site_id not in self._definitions, "Repeated native-call source identity.")
                    self._definitions[site_id] = descriptor
                    calls[key] = site_id
            by_offset, active_span = {}, None
            # PRECALL specialization and CALL caches share the same exact AST
            # span. No line-only or nearest-offset fallback is permitted.
            for instruction in dis.get_instructions(code, show_caches=True, adaptive=False):
                position = _position(instruction)
                if instruction.opname in ("PRECALL", "CALL", "CALL_FUNCTION_EX"):
                    active_span = position if position in calls else None
                elif instruction.opname != "CACHE" or position != active_span:
                    active_span = None
                if active_span is not None:
                    site_id = calls[active_span]
                    by_offset[instruction.offset] = site_id
                    self._definitions[site_id]["allowed_instruction_offsets"].append(instruction.offset)
            _require(calls and all(self._definitions[key]["allowed_instruction_offsets"]
                                   for key in calls.values()),
                     "A native-call AST span lacks an exact CPython instruction position.")
            self._sites[id(code)] = by_offset
        _require(len(self._definitions) <= MAX_SITES, "Native-call inventory exceeds its bound.")
        self.max_events, self.max_rows = max_events, max_rows
        self._rows, self._pending, self._errors = {}, [], []
        self._events = 0
        self._started = self._stopped = False
        self._owner_thread = None
        self._hook = self._profile

    def _invalidate(self, reason):
        if not self._errors:
            self._errors.append(reason)
        self._pending.clear()  # Do not retain frames, connections or cursor owners.

    def _profile(self, frame, event, argument):
        try:
            if self._errors or threading.get_ident() != self._owner_thread:
                return
            code_id = id(frame.f_code)
            sites = self._sites.get(code_id)
            if sites is None:
                return
            if event == "call":
                self._invocations[code_id] += 1
                if self._invocations[code_id] > self.max_events:
                    self._invalidate("observed-function-entry-bound")
                return
            if event not in ("c_call", "c_return", "c_exception"):
                return
            operation = _native_operation(argument)
            if operation is None:
                return
            self._events += 1
            if self._events > self.max_events:
                self._invalidate("selected-native-event-bound")
                return
            site_id = sites.get(frame.f_lasti)
            if site_id is None or self._definitions[site_id]["operation"] != operation:
                self._invalidate("unmapped-native-call-position")
                return
            if event == "c_call":
                if len(self._pending) >= 8:
                    self._invalidate("native-call-stack-bound")
                    return
                # Only exact source-reviewed code identities are emitted;
                # inspect at most four caller frames, never their local values.
                callers, current = [], frame.f_back
                for _ in range(4):
                    known = self._known_codes.get(id(current.f_code)) if current else None
                    if known is None:
                        break
                    callers.append(known)
                    current = current.f_back
                parent = callers[0] if callers else {"function": "outside-reviewed-source"}
                parent_key = sha256(_canonical(callers)).hexdigest()
                key = (site_id, parent_key)
                if key not in self._rows:
                    if len(self._rows) >= self.max_rows:
                        self._invalidate("native-row-bound")
                        return
                    self._rows[key] = dict(site_id=site_id, caller=parent, caller_chain=callers,
                        calls=0, returns=0, exceptions=0, elapsed_ns=0, maximum_ns=0,
                        start_offsets=set(), end_offsets=set())
                row = self._rows[key]
                row["calls"] += 1
                row["start_offsets"].add(frame.f_lasti)
                self._pending.append((frame, argument, site_id, key, perf_counter_ns()))
            else:
                ended = perf_counter_ns()
                if not self._pending:
                    self._invalidate("unpaired-native-return")
                    return
                parent_frame, parent_argument, expected_site, key, started = self._pending.pop()
                if (parent_frame is not frame or parent_argument is not argument
                        or expected_site != site_id or ended < started):
                    self._invalidate("native-call-pair-changed")
                    return
                row = self._rows[key]
                row["returns" if event == "c_return" else "exceptions"] += 1
                elapsed = ended - started
                row["elapsed_ns"] += elapsed
                row["maximum_ns"] = max(row["maximum_ns"], elapsed)
                row["end_offsets"].add(frame.f_lasti)
        except BaseException as error:
            # Ordinary diagnostic failures do not replace production failures.
            # Process/user interruptions must still propagate after invalidation.
            self._invalidate("observer-callback-" + type(error).__name__)
            if not isinstance(error, Exception):
                # Preserve process/user interruption; CPython clears a failed
                # profile hook and the normal owner finally still runs.
                raise

    def start(self):
        _require(not self._started and not self._stopped, "Observer cannot be reused.")
        _require(sys.getprofile() is None,
                 "Refuse nested profiling; the existing profile hook belongs to another owner.")
        self._owner_thread = threading.get_ident()
        self._started = True
        sys.setprofile(self._hook)

    def stop(self):
        if not self._started or self._stopped:
            self._invalidate("observer-lifetime-order")
            return
        if threading.get_ident() != self._owner_thread:
            self._invalidate("observer-owner-thread-changed")
            return
        if sys.getprofile() is self._hook:
            sys.setprofile(None)
        else:
            self._invalidate("observer-hook-replaced")
        self._stopped = True
        if self._pending:
            self._invalidate("unfinished-native-call")
        self._pending.clear()

    def run(self, function, *args, **kwargs):
        self.start()
        try:
            return function(*args, **kwargs)
        finally:
            self.stop()

    def record(self):
        _require(self._started and self._stopped, "Observer has not completed its lifetime.")
        for filename, (raw, digest) in self._source_bytes.items():
            with Path(filename).open("rb") as stream:
                current = stream.read(MAX_SOURCE_BYTES + 1)
            if current != raw or sha256(current).hexdigest() != digest:
                self._invalidate("observed-source-changed")
        rows = []
        for key in sorted(self._rows):
            original = self._rows[key]
            row = dict(original, start_offsets=sorted(original["start_offsets"]),
                       end_offsets=sorted(original["end_offsets"]))
            if row["calls"] != row["returns"] + row["exceptions"]:
                self._invalidate("incomplete-native-call-count")
            rows.append(row)
        if not rows:
            self._invalidate("no-observed-native-calls")
        result = dict(schema=SCHEMA,
            status="INVALID" if self._errors else "COMPLETE",
            errors=list(self._errors), thread=self._owner_thread,
            runtime=dict(implementation=sys.implementation.name, python=sys.version.split()[0],
                         sqlite=sqlite3.sqlite_version),
            sources=[dict(filename=path, sha256=value[1])
                     for path, value in sorted(self._source_bytes.items())],
            limits=dict(sites=MAX_SITES, rows=self.max_rows, selected_events=self.max_events,
                        pending_native_calls=8, context_frames=4, serialized_bytes=MAX_RECORD_BYTES),
            selected_native_events=self._events,
            functions=[dict(identity=self._code_identity[code], invocations=count)
                       for code, count in self._invocations.items()],
            sites=sorted(self._definitions.values(), key=lambda row: row["site_id"]),
            rows=rows,
            scope="Current-thread paired native C-call wall intervals, including observation overhead "
                  "and descheduling. execute ends when the native method returns its cursor; explicit "
                  "fetchone/fetchmany/fetchall intervals are separate. Implicit cursor iteration is "
                  "excluded, so these are not complete query costs. Nested native intervals can overlap. "
                  "No connection/cursor replacement, SQL parameters, expanded SQL, locals, row values "
                  "or user-data observation.")
        _require(len(_canonical(result)) <= MAX_RECORD_BYTES,
                 "Native SQLite observation exceeds its serialized bound.")
        return result
