"""Original synthetic JSONL actor, never an implementation of speech synthesis.

This executable fixture models the externally observed worker framing only.
Every PCM value and identity is artificial. No private source or resource is used.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from queue import Queue
import os
import struct
import sys
from threading import Event, Thread


OUTPUT_RATE = 24000
CLASSMATE_LOCALES = frozenset({"sk"})


def main():
    root = Path(__file__).resolve().parent
    scenario = json.loads((root / "scenario.json").read_text())["mode"]
    (root / "worker-ready.json").write_text(json.dumps({"pid": os.getpid()}))
    if scenario == "stall_reader":
        Event().wait(15)
        return
    queue, cancelled = Queue(maxsize=8), Event()

    def read():
        for line in sys.stdin:
            request = json.loads(line)
            logged = {k: v for k, v in request.items() if k != "text"}
            if "text" in request:
                logged["text_sha256"] = hashlib.sha256(request["text"].encode()).hexdigest()
            logged["pid"] = os.getpid()
            with (root / "requests.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(logged) + "\n")
            if request["mode"] == "cancel":
                cancelled.set()
            else:
                if request["mode"] == "reference":
                    cancelled.clear()
                queue.put(request)
        cancelled.set()
        queue.put(None)

    def reply(value):
        print(json.dumps(value), flush=True)

    def chunk(number):
        data = struct.pack("<3h", number, number + 1, number + 2)
        reply({"chunk": base64.b64encode(data).decode(), "engine": "synthetic-acceptance"})

    Thread(target=read, daemon=True).start()
    serial, remaining = 0, 0
    while (request := queue.get()) is not None:
        mode = request["mode"]
        if mode == "reference":
            serial += 1
            remaining = 1
            if scenario == "late_cancel_chunk":
                reply({"loadingStage": "language-source"})
                (root / "awaiting-cancel").write_text("ready")
                if not cancelled.wait(3):
                    reply({"error": "FixtureDeadline"})
                    continue
                reply({"loadingStage": "selected-voice"})
                chunk(serial * 100)
            else:
                chunk(serial * 100)
        elif mode == "next":
            if cancelled.is_set() or not remaining:
                reply({"finished": True})
            else:
                remaining -= 1
                chunk(serial * 100 + 10)
        elif mode == "drop":
            remaining = 0
            if scenario == "bad_drop":
                reply({"finished": True, "unexpected": True})
            else:
                reply({"finished": True})
        else:
            reply({"error": "UnknownFixtureCommand"})


if __name__ == "__main__":
    main()

