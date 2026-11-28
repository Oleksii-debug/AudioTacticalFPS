# Canonical domain-source disposition — 27 November 2026

## Review boundary

This document accounts for the **87 canonical product-source paths** assigned to the domain review. The companion [per-path JSON](CANONICAL_DOMAIN_SOURCE_PATH_DISPOSITION_20261127.json) binds every old path, its exact canonical blob, the same-path current blob or absence, the concrete current owners, current regression modules, supporting ledgers and limitations. The other **52 paths** belong to the root's separate general-source review.

| Input | Exact identity |
| --- | --- |
| Canonical commit | `e5c64a5245583f8f8b404a0f042f6ca438c24d7f` |
| Canonical tree | `344567db5c0d99f5d6d7ad24c105645ea75b65e8` |
| Audited current commit | `4e733a4f081c7f7bf0464983e94711604fc93fa1` |
| Audited current tree | `e1ec6da12495beea19aa3dfcce38340342130c8b` |
| Root input inventory commit | `30269d7c207d6fecad39e383b27afee713c21f89` |
| Input inventory blob | `c5dac71d82509d95946204a6b0ce302f18c1f69f` |

Both recursive trees were complete. All 87 old/current blob pairs match the root input inventory. Every owner, test and evidence reference resolves to an exact blob in the audited tree. Targeted direct comparisons supplement the earlier domain reviews; the JSON records whether each same-path source was reopened in this audit and whether the old blob is explicit in the linked domain ledger.

This is **source review**, with no new compilation or execution claim. It does not equate old and current Python imports, saved schemas, output waveforms, or individual test cases merely because their names or totals are similar.

## Real gaps found by this review

### Dordiai: three current-owner repairs

The current application has a real `DordiaiVoicesEngine` for the selected owner's `nvda_worker.py` protocol. The separately implemented generic local voice helper is a different adapter; it does not establish Dordiai preservation.

Direct comparison with canonical `integrations/dordiai_speech.py`, `dordiai_voices.py` and `_dordiai_probe.py` found three concrete gaps:

| Gap at the audited base | Reviewed repair in the existing owner |
| --- | --- |
| Returned PCM could publish without a post-render complete-runtime recheck. | Recheck the exact loaded manifest and complete asset inventory after streaming and cleanup, before the existing no-replace publisher. |
| A failed native stop could unwind a streaming operation, and the old one-second I/O joins could leave threads alive. | The existing pending cleanup owner now retains the native family, both I/O threads, pipes, staged PCM and caller resource lease until confirmed settlement. Interrupts are delivered after that boundary. |
| Routes were inferred from a hardcoded native-locale list and regional codes were reduced to their base. Missing/partial language records were lost. | Existing isolated discovery now uses the exact selected bootstrap, installed coverage, wide registration, worker native locales and 24 kHz declaration. Typed v2 records preserve full runtime codes and passive statuses. Request flags and the cache discriminator bind the verified route. |

The clean repair chain ends at **`7fde5edd8416621b7287b55684058844728fde7a`**, tree `d767104e8fc49b619c98e05be307bab79c7cdfc9`, on exact root `1ea95e02acf514078142501040836b57f780a4da`. All five files were read back exactly. Independent source review found two concrete predecessor issues—code-or-None predicate compatibility and I/O-thread settlement—and the successor fixes both.

The repair contains **34 new declared cases** and retains **22 existing discovery cases**. Real synthetic child I/O, runtime mutation, selected route flags and actual resource/I/O-thread barriers are authored tests, not observed successes in this document. Root gate integration adds both complete modules, protecting **56 cases per mode**; this differs from the net 34 newly authored cases. The per-function maps and exact changed blobs are in [the repair evidence](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/7fde5edd8416621b7287b55684058844728fde7a/carr-library/docs/evidence/canonical-dordiai-route-and-lifetime-repair-20261127.json) at that component.

The new discovery contract does not silently alias old canonical voice IDs or old caches. Warm persistent-worker reuse, private neural assets, multilingual/acoustic quality, physical output and NVDA remain unqualified.

### Studio: native role and export forms remain an explicit action

Canonical `ui/studio_dialog.py` blob `418da4c9bf29098ebfc7fae933c43582beb7b953` provides an **Edit roles and voices** form for installed voice, pan, gain, synthesis rate and volume, plus a final-format Choice and stems checkbox.

At the audited current base, the underlying semantics remain available through the current structured production script, parser, models and renderer. Current Studio also has default voice, effect-chain, clip, preview and export controls. It does **not** have equivalent native role/export forms. Script access is not credited as preservation of that visible workflow.

The root assigned a thin draft-bound restoration to the current native UI owner. It must preserve current source/draft identity, trust/resource admission, progress and late-completion guards, recording behavior and SaveCoordinator. **This row remains open until its actual source component is reviewed and separately qualified.** This ledger is not authorization to merge away the canonical form without that repair.

## Disposition of the remaining source paths

| Disposition | Paths | Meaning |
| --- | ---: | --- |
| Bounded current-owner mapping | 69 | A concrete current service/UI owner and relevant regression modules are recorded, with the stated semantic limits. |
| Explicit inert migration | 9 | Historical declarations and source identities remain recoverable without adopting old execution or external-path authority. |
| Retired import surface with current workflow | 3 | Old package-level Python convenience exports are not aliases; the current user workflow has explicit owners. |
| Docstring-only namespace retired | 1 | The old integration package initializer has no independent executable behavior. |
| Retention with a different validation boundary | 1 | Generated backup is passive; current active consumers enforce the separately documented completion checks. |
| Dordiai post-base repair | 3 | The three canonical owner-specific adapter paths point to the exact reviewed successor, with execution pending. |
| Open native Studio form action | 1 | The missing visible controls above require their assigned repair. |
| **Total** | **87** | No root-owned general-source row is included. |

The assistant mapping includes current deterministic plans, explicit local model proposals, topic create/list/export, templates, typed interactive dispatch and the restored `audio.transcribe` bridge. Source/profile/output approval is reviewed again on execution; unfinished dispatch intent never silently replays. An interactive receipt proves the observed action or reading start, not whole-book completion.

The research/bibliography mapping binds exact current source, style and export-approval owners. Direct comparison of the small shared changes confirms that catalogue export callers only move to the current typed approval helper, `topic_tree.list_topics` adds an optional bounded limit without changing the default, and `export_file` retains stale-approved-file checks while adding current durable-publication hooks. The obsolete `NativeDialogs.approve_export` wrapper is retired; current callers use `approve_export_destination` directly.

Current speech/readers retain immutable source, separate narration edits, typed old reading-history/session import and passive queue restoration. Current COM SAPI, selected 32-bit compatibility, eSpeak fallback, punctuation and local helper contracts have their own source/qualification evidence. Historical voice identifiers and device handles do not become current authority automatically.

## Limits that must survive the final merge ledger

- **ASR size semantics:** canonical historical retention permits 32 GiB per source and 64 GiB of aggregate retained inputs. Current library-wide new execution separately permits 32 GiB aggregate. Generic state and metadata have their own bounds. Declaration/accounting tests are not an actual 64 GiB throughput measurement. Current receipts-v1, V5 and library-wide V43 contracts remain distinct; a filename or worker version does not prove private-runtime compatibility.
- **Generated audio:** current passive Studio backup checks its retained byte/envelope contracts and does not reproduce the complete canonical generated-graph validator. Current completed-output consumers refuse hash-consistent invalid PCM/encoded metadata before fresh cache/admission/publication. These are separate boundaries, not blanket validator equivalence.
- **Audio output:** no byte-identical DSP waveform, old 24 kHz stereo, cross-duration cache-prefix reuse or same completed run after changing the disk budget is claimed. The optional named door/click/chime/noise generator is absent; required sound design uses reviewed Sound/Clip assets.
- **Formats:** explicit TXT encodings, EML subject and PPTX order were restored through current owners. HTML media-alt and spreadsheet formula-placeholder policies are intentionally different. Optional converter native qualification remains separate from source mapping.
- **Runtime provenance:** current Gyan acquisition is not the canonical BtbN binary/source workflow. The comtypes worker dependency was not copied or used; current SAPI uses the pinned pywin32 dependency with its own notices. No source/license clearance is inferred from a functional mapping.
- **Recovery authority:** raw history retention does not restore credential-vault contents, active network approval, device playback, foreign path permission, quotation approval or unfinished side effects.

The per-path JSON deliberately keeps the exact historical 4e source boundary. Later root CHM capability changes, the Dordiai successor, the generated-backup fixture correction and the Office bytecode-environment correction are separately identified. None is silently substituted for an audited-base blob.
