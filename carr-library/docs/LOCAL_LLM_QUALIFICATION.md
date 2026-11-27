# Actual local model proposal qualification

This isolated Linux CPU probe exercises the existing product's
`propose_local_plan`, `ProviderGateway`, `Plan`, `Scope`, and
`AssistantService.validate` with real model inference. It adds no product
planner, engine, gateway, downloader, or automatic execution path.

## Pinned inputs

- Official Qwen `Qwen3-0.6B-GGUF` revision
  `23749fefcc72300e3a2ad315e1317431b06b590a`, Q8_0, 639,446,688 bytes.
  The exact [model repository](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/tree/23749fefcc72300e3a2ad315e1317431b06b590a)
  publishes Apache-2.0 terms in its LICENSE file.
- Official llama.cpp `b12774`, commit
  `171527522610996f5f0acf12334e185156ca8f44`, Ubuntu x64 CPU archive,
  23,454,685 bytes. The [pinned release](https://github.com/ggml-org/llama.cpp/releases/tag/b12774)
  and exact MIT LICENSE are retained in the input record.
- `packaging/local-llm-proof-inputs.json` records publisher hashes, immutable
  download URLs, exact license bytes and the unchanged product-module hashes.
  Acquisition has explicit byte/time limits and no automatic retries.

The selected runtime uses two CPU threads, one slot, a 2,048-token context and a
256-token prediction cap. Thinking is disabled with the pinned runtime's
`--reasoning off` option. The exact upstream generic JSON grammar constrains
JSON syntax. Its root consumes the pinned Qwen chat generation prefix using the
runtime's documented special-token rules. All generic JSON productions remain
unchanged; no plan fields, tool names, arguments or expected answers are forced.
The unchanged product JSON, argument and scope validators decide acceptance. The service binds only to loopback and uses the runtime's
offline option. This does not establish an operating-system network sandbox.

An initial actual run loaded the model but failed before generation: this
runtime's global `--json-schema` grammar rejected the Qwen chat generation
prefix. A second run without a grammar returned Markdown-fenced output, which
the strict product parser correctly refused. The reviewed generic grammar now
consumes that exact native prefix before JSON generation, without rewriting
responses or weakening parsing. These failed runs remain in the isolated
workflow history; proposal acceptance, permission refusals, cancellation and
no-send assertions are unchanged.
The primary [generic grammar](https://github.com/ggml-org/llama.cpp/blob/171527522610996f5f0acf12334e185156ca8f44/grammars/json.gbnf)
and [token-rule documentation](https://github.com/ggml-org/llama.cpp/blob/171527522610996f5f0acf12334e185156ca8f44/grammars/README.md)
are pinned with the configured grammar hash.

## Acceptance

The product's existing `assistant.llm.SYSTEM` instruction now explicitly asks
for raw JSON without Markdown or reasoning, a string schema identifier, a string
summary and an array of steps containing a string tool and object arguments.
It supplies no expected tool or argument. The parser, tool validator, caller
scope, gateway and approval logic are unchanged. The pinned source hash binds
the actual inference to this product prompt; synthetic user instructions contain
only their requested operations. A previous actual run returned fenced JSON with
an incorrectly nested schema and was correctly rejected by the product parser.
This narrow correction benefits the real proposal workflow as well as the probe.


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
