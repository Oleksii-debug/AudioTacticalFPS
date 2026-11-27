# Independent synchronized-book acceptance

These scripts adapt canonical PR152's independent-validator gates to the single current audio.production/audio.publishing renderer and the version 2 clip plan. They do not import the alternate studio or speech stacks. New adapter execution is NOT_RUN until the exact integrated gate finishes.

## Exact tools

packaging/epubcheck.json retains EPUBCheck 5.3.0, archive size 33,071,108 bytes and SHA256 6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5. The public upstream release API independently reports this same size/digest. The tagged release states EPUB 3.3 conformance checking; its exact source commit is 029831b8f477e4519e9734c984ee24357547a698. Its BSD-3-Clause notice and third-party notices are required inside the verified archive. Newer EPUBCheck releases have separate terms and are not silently substituted.

Sources: https://github.com/w3c/epubcheck/releases/tag/v5.3.0 and https://api.github.com/repos/w3c/epubcheck/releases/tags/v5.3.0 ; exact license: https://github.com/w3c/epubcheck/blob/029831b8f477e4519e9734c984ee24357547a698/LICENSE.md . Specification: https://www.w3.org/TR/epub-33/ .

packaging/daisy-validation-schemas.json retains the canonical five fixed DTD/entity pins. The DAISY Consortium SMIL DTD is tied to commit 88d39392e9a3e10c1f55f1a82e5e7ea12b0b28b6; the W3C XHTML files each have exact size/SHA checks. The lxml parser uses no_network, no general entity substitution and a resolver that admits only the five selected local files. lxml is already in the reviewed application dependency lock; this work adds no runtime wheel or model.

DAISY 2.02 specification: https://daisy.org/activities/standards/daisy/daisy-2/daisy-format-2-02-specification/ . DTD conformance alone does not cover every DAISY rule or reader behavior.

## Run

An external build step may obtain the publicly pinned inputs and must check their sizes and hashes. Neither acceptance script downloads or installs anything. No validator is added to the delivered runtime by these scripts.

    python scripts/epubcheck_acceptance.py --archive /explicit/epubcheck-5.3.0.zip --java /explicit/java --report /new/epubcheck-report.json
    python scripts/daisy_schema_acceptance.py --schemas /explicit/pinned-dtds --report /new/daisy-dtd-report.json

For an installed wheel use that interpreter with -I and --require-installed. This requires all tested application modules to reside inside its own site-packages prefix. For a sealed Windows bundle also pass --bundle-root to verify the existing audio-runtime inventory and exact selected ffmpeg.exe; the application's reviewed runtime resolver remains authoritative. The reports are new files and are never overwritten.

Both gates create three real short synthetic productions: Unicode/multiple chapters with pauses, an MP3-selected production whose checked PCM master is reused for publication, and retained clip-only audio after the selected original is removed. The existing renderer and exporter generate every artifact. The external checker binds its results to the exact exported byte hash and records the application module/audio-runtime identities.

EPUBCheck must report version 5.3.0 and no fatal errors, errors or warnings; a deliberately invalid mimetype must be rejected. DAISY must pass the actual local XHTML/SMIL DTDs; a well-formed document containing an unsupported element must fail. A changed publication or schema during validation also fails the gate. Validator reports include reader_interoperability and nvda as NOT_RUN, and DAISY full_daisy_rule_conformance as NOT_RUN.

The portable test_publication_acceptance suite checks boundary refusal and report handling, including real child-process deadline and diagnostic-budget failures. Java runs through the existing owned-process supervisor with a 180-second deadline, a 1 MiB diagnostic limit and an 8 MiB report limit; retained native cleanup is settled before temporary files can be removed. Its mocked report cases are harness tests only. They cannot produce conformance acceptance; only the real Java/lxml runs above can do that. No synthetic audio test establishes speech quality or microphone/playback-device qualification.

The separate carr-publication workflow installs the source-bound application wheel and checks the unchanged 53-pin production inventory before adding test tools. It requires source and isolated installed copies of the boundary suite to finish with zero skips, then runs both independent validators. Every downloaded validator/archive/schema has an exact byte count and SHA256. The runner-provided Java 17 version and executable hash are recorded. Only textual reports are retained; validator payloads, synthetic books and the test audio runtime are ephemeral. A new source SHA does not cancel a different source SHA's qualification run.

R7 adapter note: the checker uses current audio.recording_process and audio.cleanup; the alternate capture_process family is not imported. The standalone workflow adds four current cleanup-owner cases to the thirteen checker-boundary cases, requiring seventeen source and seventeen isolated installed cases. R7 execution status and retained guards are recorded in AUDIO_R7_PORT_20261127.md.
