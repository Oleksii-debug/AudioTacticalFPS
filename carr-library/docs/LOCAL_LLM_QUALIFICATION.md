# Actual local model proposal qualification

This isolated Linux CPU probe exercises the existing product's
`propose_local_plan`, `ProviderGateway`, `Plan`, `Scope`, and
`AssistantService.validate` with real model inference. It adds no product
planner, engine, gateway, downloader, or automatic execution path.

## Pinned inputs

- Official Qwen `Qwen3-1.7B-GGUF` revision
  `90862c4b9d2787eaed51d12237eafdfe7c5f6077`, Q8_0, 1,834,426,016 bytes.
  The exact [model repository](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/tree/90862c4b9d2787eaed51d12237eafdfe7c5f6077)
  publishes Apache-2.0 terms in its LICENSE file.
- Official llama.cpp `b12774`, commit
  `171527522610996f5f0acf12334e185156ca8f44`, Ubuntu x64 CPU archive,
  23,454,685 bytes. The [pinned release](https://github.com/ggml-org/llama.cpp/releases/tag/b12774)
  and exact MIT LICENSE are retained in the input record.
- `packaging/local-llm-proof-inputs.json` records publisher hashes, immutable
  download URLs, exact license bytes and the unchanged product-module hashes.
  Acquisition has explicit byte/time limits and no automatic retries.

The previously selected official Qwen3-0.6B Q8_0 candidate remains unqualified.
With the neutral complete tool descriptions at commit 9685d79, actual run
45946002017 validated the English library-list proposal but returned the wrong
tool and omitted the requested count for the Ukrainian book request. The
unchanged semantic assertion failed; the model proposal was never executed.

The 1.7B candidate changes only the pinned model and service alias. Actual
metadata-only run 45946912142 verified the immutable publisher pointer, exact
Apache-2.0 license bytes and model-card bytes before model acquisition. Model
weights plus the unchanged archive/expanded runtime occupy about 1.79 GiB.
The native 3 GiB address-space limit, CPU/time limits, fixed seed, product
source, grammar and all four multilingual/permission cases are unchanged.
No shared local workspace is used. A metadata PASS is not an inference PASS.

The first actual 1.7B run, 45947558728 at 0e3ec0d, selected the correct English
and Ukrainian tools within the unchanged native memory/time caps. It still
omitted the explicitly requested Ukrainian catalog count while mentioning it
in the summary. The unchanged semantic assertion rejected that proposal;
artifact 15175282800 preserves the original response and failure.
The product SYSTEM now makes a general constraint explicit: every supported
limit, filter, path or option requested by the user must be in step arguments,
not merely the summary. Unspecified optional fields remain omitted. No example
number, tool choice or expected answer is supplied by that correction.

Actual run 45949212390 at 232f8c4 passed the nine source contracts but failed
before model readiness. That hosted CPU selected an additional AMX weight
buffer of 2,043,035,648 bytes. The unchanged 3 GiB address-space ceiling refused
it; artifact 15176502676 records the allocation error and zero model responses.
The pinned runtime's supported `--no-repack` option now disables those extra
weight buffers. Its exact `common/arg.cpp` maps this option to
`no_extra_bufts`, and `common/common.cpp` passes that to
`llama_model_params.use_extra_bufts`. CPU, memory, time, source, model and
semantic acceptance bounds are unchanged. This is a portable memory-layout
selection, not an increased resource allowance or an inference result.

The selected runtime uses two CPU threads, one slot, a 2,048-token context and a
256-token prediction cap. Thinking is disabled with the pinned runtime's
`--reasoning off` option. The exact upstream generic JSON grammar is passed
through `--grammar-file`, byte-for-byte, with no chat-prefix adaptation.
It constrains only JSON syntax: no plan fields, tool names, arguments or
expected answers are forced. The unchanged product JSON, argument and scope
validators decide acceptance. The service binds only to loopback and uses the
runtime's offline option. This does not establish an operating-system network
sandbox.

The failed attempts remain visible in the isolated workflow history:

- The initial `--json-schema` configuration loaded the model but failed before
  generation because the OUTPUT_FORMAT grammar could not consume the Qwen chat
  generation prefix.
- Unconstrained generation returned Markdown fences. The product's strict parser
  rejected them, including after the real product SYSTEM format clarification.
- An attempted prefix-aware generic grammar was supplied with `--grammar-file`.
  That USER grammar forced the prefix to be generated again; the strict parser
  rejected that actual response. Its model-selected tool also did not satisfy
  the list request.

Pinned upstream source explains the distinction: `common/arg.cpp` classifies
`--json-schema` as OUTPUT_FORMAT and `--grammar-file` as USER. In
`common/sampling.cpp`, `common_grammar_needs_prefill` excludes USER grammars.
The current candidate therefore reuses unmodified upstream
[json.gbnf](https://github.com/ggml-org/llama.cpp/blob/171527522610996f5f0acf12334e185156ca8f44/grammars/json.gbnf),
without response rewriting, parser relaxation, seed shopping or answer
constraints. Exact source/grammar identities are in the input manifest.
All proposal acceptance, permission refusal, cancellation and no-send
assertions are retained. Actual run 45945159039 confirmed that this configuration
returns strict JSON, but the small model chose an unrequested library-creation
step for a list request. The existing caller Scope refused it. This semantic
failure remains evidence of an unqualified candidate, not a positive result.

## Acceptance

The product's existing `assistant.llm.SYSTEM` now explicitly describes the
strict JSON shape, the purpose of every allowed tool, and every argument name
and type accepted by the real `AssistantService._check_arguments`. It requires
only explicitly requested operations and the fewest necessary steps. Tool
descriptions are complete and neutral; no expected test answer is injected.
The actual schema source is
[service.py at bbd7](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/bbd7f662162c139bf4d4cf06f32257c141b9884a/carr-library/src/carr_library/assistant/service.py).

This addresses the observed semantic failure as a real product correction.
The parser, tool validator, caller scope, gateway and approval logic remain
unchanged. The pinned source hash binds actual inference to this product prompt;
synthetic user instructions contain only their requested operations. The fixed
sampling seed, generic JSON grammar, all four semantic/boundary cases and
no-execution controls are unchanged. The generically corrected product prompt is exercised against the same pinned 1.7B candidate; a complete actual result is still required.


The probe requires actual English and Ukrainian model proposals to pass the
existing argument and scope validation. Separate actual model responses must be
refused when they request unapproved library creation or an input outside the
selected roots. Duplicate-operation and pre-send cancellation controls must
prevent further HTTP requests.

No model proposal is submitted or executed. The probe replaces those two service
entry points only with forbidden-call assertions, delegates every actual request
to the existing HTTP transport, and verifies that jobs and the catalog remain
unchanged. It records the original model responses, source and artifact hashes,
actual command, selected outcomes and native-process cleanup.

Only synthetic JSON evidence is uploaded. Model weights, native binaries,
credential data and library originals are never included in artifacts. A passed
report is required before claiming actual model inference. The source workflow's
presence alone is not an acceptance result.

Native Windows packaging, installed offline behavior, general language coverage,
research accuracy, accessibility and broad model quality remain separate gates.
