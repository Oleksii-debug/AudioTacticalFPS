"""Bounded progress markers for the unchanged required audio pytest selection.

The wrapper forwards every argument and pytest's actual exit status. It exposes
the last active test without printing potentially very long parameter IDs.
"""
import sys
import time

import pytest


class CaseProgress:
    def pytest_sessionstart(self, session):
        self.reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        self.number = 0

    def pytest_runtest_logstart(self, nodeid, location):
        self.number += 1
        self.started = time.monotonic()
        label = nodeid.partition("[")[0][:240]
        self.reporter.write_line(f"CASE_START {self.number:03d} {label}")

    def pytest_runtest_logfinish(self, nodeid, location):
        elapsed = time.monotonic() - self.started
        self.reporter.write_line("")
        self.reporter.write_line(f"CASE_END {self.number:03d} {elapsed:.3f}s")


if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:], plugins=[CaseProgress()]))
