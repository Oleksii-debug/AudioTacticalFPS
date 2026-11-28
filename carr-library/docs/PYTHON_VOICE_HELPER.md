# Reviewed Python voice helpers

The existing **Audio Studio → Scene voice and music engines → Register a reviewed local voice helper**
action accepts a Python entry script together with its explicitly selected interpreter.
The helper still implements the existing `carr.local-voice.v1` request/reply protocol.
This is an adapter for reviewed local code, not a voice engine installation or a
claim that a particular private provider is compatible.

## Declaration and explicit review

The selected registration JSON contains these fields:

| Field | Meaning |
| --- | --- |
| executable | Absolute local interpreter path. |
| sha256 | Actual lowercase SHA-256 of that interpreter file. |
| script | Absolute local path to the reviewed entry script. |
| script_sha256 | Actual lowercase SHA-256 of that script. |
| engine_name | Bounded visible engine name. |
| enabled | Explicit activation, accepted only as a boolean. |
| max_voices | Optional bounded catalog size, from 1 through 10,000. |

The two script fields must be supplied together. Missing files, mismatched hashes,
network references, alternate data streams, relative script paths and linked files
are rejected. A disabled declaration may retain an unavailable installation path
without opening or running it.

The existing confirmation shows the interpreter path/hash and the script path/hash.
Review runs on the existing background worker and enumerates the real helper before
saving. Merely reading or restoring the declaration does not activate it. A failed
review keeps the previously saved configuration.

The fixed command is:

```text
<reviewed-python> -I -B -S <reviewed-script>
```

The host sends a bounded UTF-8 JSON request on stdin and reads a bounded JSON reply
from stdout; it supplies no user-defined Python arguments. The isolated invocation
excludes the script directory, user site and environment-injected Python search
paths, disables site startup and avoids bytecode writes. These are Python startup
controls, not an OS sandbox or a network-denial policy. The interpreter-file hash
and entry-script hash do not seal every library that trusted code might explicitly
load. See the [official Python command-line contract](https://docs.python.org/3.12/using/cmdline.html).

A registration without the two script fields keeps the historical single-executable
command. Owner-specific Studio voice registrations and their reviewed private
protocol remain separate and unchanged.

## Identity, output and cancellation

Both files are checked before every helper catalog or render request. Cache
fingerprints also include the script digest and fixed launch mode. Changing the
script requires explicit review of its new digest; discovery does not replace an
invalid configured helper with a default voice.

The existing process-family owner handles generic helper execution, including
Windows interpreter launchers and POSIX children. Timeout and Cancel keep their
existing semantics. If cleanup cannot yet prove quiescence, temporary input,
diagnostic streams and the private utterance directory stay retained by the
cleanup owner. No final WAV is published from that failed request.

Successful synthesis still validates actual PCM format, frames and byte limits
before the existing atomic no-overwrite WAV publication. Text remains literal
JSON data and never becomes a shell command.

## Backup and qualification

Complete native backups and supported foreign-flat backups keep the declarations
as metadata. Restore preserves paths and hashes and sets the existing
`restore_requires_revalidation` barrier. It does not run an external interpreter
or grant permission to a restored helper.

Prepared source/installed selectors:

```text
tests/speech/test_python_helper.py
tests/storage/test_python_helper_backup.py
tests/speech/test_runtime_settings_restore.py
tests/ui/test_engine_settings_review.py
```

The new process fixtures use actual Python children, a synthetic 320-frame PCM
recording, hostile startup-path controls, and retained descendant handles.
The backup fixtures use real schema-6 libraries, native format 5 and supported
flat format 4. They test inactive restoration and rehashed malformed declarations.
These additions are source-reviewed and NOT_RUN at this checkpoint; the next
exact-candidate Windows and Linux gates provide execution evidence. They do not
qualify a neural voice, acoustic quality, human listening, or NVDA behavior.
