# New V3 contracts and managed update observations — 2026-11-27

These observations bind the named source hashes only. Later source corrections
are NOT_RUN until their own jobs complete.

## e43 fast source and installed contracts

Run46010948402 / Linux job140545996989 completed seven of11 strict source
suites. Named templates43/43, template handlers21/21, book CLI9/9, large-import
CLI8/8, large-import handlers9/9, backup capacity UI6/6 and the20 strict-runner
cases passed with zero skips. The four failing suites had two causes:

- Measured Book dialog used the same already-corrected shared Effect fixture.
- The bounded PDF cursor rejected pypdf NumberObject stream lengths because it
  required type(value) is int instead of the genuine integer indexing protocol.

The corrected PDF reader accepts operator.index values, refuses bool, float,
text, negative/read-all and oversized reads, and retains the16MiB per-read guard.
Real parser/import/recovery cases remain required; the added direct regression
uses pypdf.NumberObject, not a replacement parser.

Installed and real-loopback steps were correctly refused before pytest because
the initial checker required bootstrap/semantic_compat/probe.py. That directory
contains developer probes and is not a declared package in pyproject.toml.
The correction derives the exact expected Python inventory from the explicit
setuptools package list, then requires equality with every actual installed
Python file plus exact byte hashes. Missing, extra, changed or source-shadowed
code remains a failure. Native/data package seals are still separately required.

The old162 audio run's loopback refusal was the previously fixed runner
allowlist; the current runner permits only the two exact planning/translation
HTTP fixture paths. No general network permission has been added.

## Actual managed Windows update at819

Run46005184289 / job140526455617 passed on Windows x64.
All eight strict contract suites passed:251 cases, zero skips.
The installed standard setup proof then executed real A→B update, full-wrapper
rollback, Repair, Uninstall, native registration, Credential Manager,
live-application exclusion and complete backup/restore with original user data.

SourceA:665acf2c1fdb5a30a05ef31c482eb02dcaf1e141.
SourceB:819d6bd910a92171eacc487806a8968ad25ad485.
SetupA SHA256:b772d1bb09993cfa9c92bd09c97d63c6117f73afe2a99c76fdd19ff44f553b48.
SetupB SHA256:1a9b20bf53678e847164975d4d1804d1cf789e137fcd77623c07a3bc3026f20d.
Target SHA256:b0947eb459d7252a618658cdbf382947ee0ead2ba31ce389e1d47eaeee6351ad.
Final PASS timestamp:2026-11-27T14:22:36.7457462Z.

The proof explicitly records Authenticode NOT_CLAIMED, production publisher
NOT_CONFIGURED and NVDA NOT_RUN. It does not certify a later composition.

## Independent publication conformance at162

Run46007562215 / job140534501156 passed the source-bound installed wheel,
unchanged53-pin runtime, hash-pinned FULL FFmpeg9.1 and independent validators.
Actual EPUBCheck5.3 and offline DAISY DTD checks include invalid controls.
Artifact15204696125 SHA256:
794990b7a308a457d735b2a0befb695981ebe7895220a00a54d88395a616aaae.
This is format-validation evidence, not acoustic quality or reader/NVDA
interoperability. The later measured-PCM and publication-cleanup composition
requires its own run.
