"""Measure synthetic legacy CSL profiles without changing application budgets."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.source / "src"))
import quickjs
from carr_library.academic_integration import available_styles, render_citations, CitationRequest

original = quickjs.Context
measurements = []
current = {}
memory_override = None


class ObservedContext:
    def __init__(self):
        self.inner = original()
        self.limits = {}

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def set_memory_limit(self, amount):
        actual = memory_override if memory_override is not None else amount
        self.limits["requested_memory"] = amount
        self.limits["actual_memory"] = actual
        return self.inner.set_memory_limit(actual)

    def set_time_limit(self, amount):
        self.limits["cpu_seconds"] = amount
        return self.inner.set_time_limit(amount)

    def eval(self, code):
        started = time.monotonic()
        cpu = time.process_time()
        row = dict(current, limits=dict(self.limits), javascript_chars=len(code))
        try:
            value = self.inner.eval(code)
            row["status"] = "RETURNED"
            return value
        except BaseException as error:
            row.update(status="RAISED", error=repr(error))
            raise
        finally:
            row.update(wall_seconds=time.monotonic() - started, process_cpu_seconds=time.process_time() - cpu)
            try:
                row["runtime_memory"] = self.inner.memory()
            except Exception as error:
                row["memory_diagnostic_error"] = repr(error)
            measurements.append(row)
            print(json.dumps(row, sort_keys=True), flush=True)


references = [dict(id="book-doe", type="book", title="The Book of Science",
                   author=[{"family": "Doe", "given": "Jane"}],
                   issued={"date-parts": [[2020]]}, publisher="Example Press", page="1-400")]
results = []
quickjs.Context = ObservedContext
try:
    for style in available_styles():
        for candidate_memory in (None, 512 * 1024 * 1024):
            memory_override = candidate_memory
            current = {"style": style.id, "diagnostic_memory_override": candidate_memory}
            row = dict(current)
            started = time.monotonic()
            try:
                result = render_citations(references, style=style.id,
                    citations=[CitationRequest("book-doe", "12", locator_verified=True)])
                row.update(status="RETURNED", bibliography=[x.text for x in result.bibliography],
                           citations=[x.text for x in result.citations])
            except Exception as error:
                causes = []
                while error is not None:
                    causes.append(repr(error))
                    error = error.__cause__
                row.update(status="RAISED", causes=causes)
            row["wall_seconds"] = time.monotonic() - started
            results.append(row)
            print(json.dumps(row, sort_keys=True), flush=True)
            if row["status"] == "RETURNED":
                break
finally:
    quickjs.Context = original
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "status": "DIAGNOSTIC_ONLY",
        "scope": "fixed synthetic citation profiles; optional memory counterfactual is not a production change",
        "results": results, "context_measurements": measurements,
    }, indent=2) + "\n", encoding="utf-8")
