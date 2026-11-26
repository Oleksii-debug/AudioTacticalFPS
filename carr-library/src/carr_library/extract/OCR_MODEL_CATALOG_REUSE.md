# Official OCR model catalogue reuse

Reviewed: 2026-11-26.

## Purpose and scope

`ocr-model-catalog.json` is a small, immutable inventory of the official
Tesseract model repositories. It contains metadata and the exact upstream
license texts, not traineddata weights, a download client, a new installer or an
OCR implementation. The existing offline-pack installer remains the installation
boundary.

Catalogue schema: `carr.ocr-model-catalog.v1`. Its `sources` object has `fast`
and `best` entries. Each records the profile, upstream repository, immutable
commit, actual Git tree, recursive tree API URL, pinned raw-file base URL,
license provenance, README provenance, compatibility and inventory counts.
The upstream recursive tree responses were complete (`truncated: false`).

## Immutable sources

| Profile | Repository | Commit | Actual Git tree |
| --- | --- | --- | --- |
| fast | [tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) | `87416418657359cb625c412a48b6e1d6d41c29bd` | `c4f8a9213d56a853074161b93fe39422d399fe52` |
| best | [tesseract-ocr/tessdata_best](https://github.com/tesseract-ocr/tessdata_best) | `e12c65a915945e4c28e237a9b52bc4a8f39a0cec` | `27af2938bbbe8fe12bc5aa024e05722d1cf72807` |

The fast revision is the V3 scope pin. The best revision was resolved from the
official repository and then fixed to the full commit above; consumers must not
substitute a moving branch. Commit and tree IDs are separate values.

| Profile | Regular model files | Language-model codes | Script-model codes | Special models | Regular model bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| fast | 162 | 123 | 37 | 2 | 700,353,173 |
| best | 162 | 123 | 37 | 2 | 1,774,547,803 |

These are file/code counts, not a claim of 123 distinct natural languages:
historical, script and vertical variants have separate codes. The byte totals
exclude licenses, READMEs, aliases and unrelated repository content. No complete
model collection was downloaded.

## Model and alias representation

Each `sources.PROFILE.models[CODE]` entry has:

- `path`: exact, case-sensitive upstream path, including `script/` where present;
- `git_blob_sha1`: Git blob identity reported in the pinned tree;
- `size`: exact file-byte count from that tree;
- `kind`: `language`, `script` or `special`;
- `role` for the two special files;
- `name`, `labels.en` and `name_source` for searchable official English names.

The pinned [official Tesseract command-line manual](https://github.com/tesseract-ocr/tesseract/blob/4292f6890aa86ef64eba32090358f40f69dd6d28/doc/tesseract.1.asc)
provides explicit names for 122 language/special codes and all 37 scripts.
The top-level `name_sources` records this source revision. The three remaining
codes (`chi_sim_vert`, `chi_tra_vert`, `jpn_vert`) retain their code as `name`, an
empty `labels` object and `name_source: code`; no native-language label was
invented. A UI may separately search its reviewed localized aliases.

Examples of codes are `eng`, `ukr`, `slk`, `chi_sim_vert` and
`script/Arabic`. Keep the script prefix and case; do not silently lowercase or
flatten upstream paths. A catalogue entry does not by itself certify that every
consumer UI or installer already accepts that code.

Only regular Git blob files with mode `100644` are model payloads.
Fast also contains deprecated `frk.traineddata` as a mode-`120000` symbolic
link, stored separately in `aliases.frk`: `target_text` and `target_path` retain
the exact target text,
`deu_latf.traineddata`, size 20, Git blob
`d473893bcabcef536ed36480f976fcce482bd87c`. The target model is `deu_latf`.
Best has no `frk` alias. Do not fetch/install the 20-byte link as model weights,
or materialize upstream symlinks into an offline pack. Other non-model symlinks,
submodules, configuration files and fonts are outside this catalogue.

## License and model compatibility

Both pinned READMEs state that repository data uses Apache-2.0:
[fast README](https://github.com/tesseract-ocr/tessdata_fast/blob/87416418657359cb625c412a48b6e1d6d41c29bd/README.md),
[best README](https://github.com/tesseract-ocr/tessdata_best/blob/e12c65a915945e4c28e237a9b52bc4a8f39a0cec/README.md).
The exact license files differ; preserve the selected source's license text.

| Profile | License bytes | License Git blob | SHA-256 of retrieved license bytes |
| --- | ---: | --- | --- |
| fast | 11,358 | `d645695673349e3947e8e5ae42332d0ac3164cd7` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| best | 10,173 | `d9a10c0d8e868ebf8da0b3dc95bb0be634c34bfe` | `a6cba85bc92e0cff7a450b1d873c0eaa2e9fc96bf472df0247a26bec77bf3ff9` |

Each `license` object includes its SPDX identifier, immutable source URL, path,
byte size, Git blob and complete unmodified text. License sizes and Git blob
hashes were independently recomputed from those UTF-8 bytes.

The official [Tesseract user manual](https://tesseract-ocr.github.io/tessdoc/Home.html)
confirms that the version-4 language models from these repositories work with
Tesseract 5.x. The [data-file reference](https://tesseract-ocr.github.io/tessdoc/Data-Files.html)
identifies fast as integerized LSTM and best as floating-point LSTM. Their
language/script recognition uses OEM 1; OEM 0 and OEM 2 are unsupported.
This is upstream compatibility evidence, not per-language runtime acceptance or
a guarantee of higher accuracy on every document. The pinned fast README also
notes secondary `jpn_vert` lookup by `jpn`; selected-pack preparation must not
infer that all language models are independent merely from the file inventory.

### OSD and equation-model provenance

The complete pinned trees contain these identical special files in both profiles:

| Code | Purpose | Bytes | Git blob |
| --- | --- | ---: | --- |
| `osd` | Orientation and script detection | 10,562,727 | `527457ca8f8fe1fda7c2f88bce3c0e4be12be9d0` |
| `equ` | Equation detection | 2,251,950 | `27b470378dc8718f1dc5a93931fefa3340336e4d` |

The official data-file reference treats these separately and records historical
compatibility beginning with Tesseract 3.01 for OSD and 3.02 for equation
detection. They are not ordinary language models. The catalogue's OEM 1
restriction is explicitly limited to language/script recognition. OSD remains
the existing orientation workflow's special input; do not substitute a language
model or claim that `equ` enables general mathematical OCR.

## Offline-pack handoff and verification

Model Git blob SHA-1 is not a raw-file SHA-1 or a SHA-256. To authenticate selected
bytes against the recorded Git object, calculate:

```text
SHA1(ASCII("blob " + decimal_file_size) + NUL + exact_file_bytes)
```

A pack preparer must bound each download, use the exact pinned source/path,
require the recorded byte size and Git blob identity, then calculate SHA-256
from those same verified bytes. Only that actual SHA-256 belongs in the existing
offline-pack manifest's `models[code] = {size, sha256}` entry. Do not invent
SHA-256 values from Git IDs, set an unchecked value to zero, or promote tree
metadata into a claim that model bytes were inspected.

The catalogue is not itself the installer's `{schema, upstream, license, models}`
manifest. A selected pack preserves the applicable exact license and provenance,
uses regular files, and passes through the established offline-pack verifier.

Verification performed for this asset: complete pinned tree coverage; 162
regular model entries per profile; exact matching code sets; unique paths;
category counts and total bytes; exact retrieved license size/Git hashes;
exact alias text size/Git hash; identical OSD/equ metadata across profiles.

### Bounded real-model smoke, 2026-11-26

Only the English model from each pinned profile was downloaded from its exact
official raw URL. Redirects were refused; Content-Length, bounded byte count and
Git blob identity were verified before the file was accepted. No other weights
were downloaded.

| Profile | English model bytes | Verified Git blob | Actual file SHA-256 |
| --- | ---: | --- | --- |
| fast | 4,113,088 | `bbef4675053b5b468cdb477053e28b1c698ba08e` | `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2` |
| best | 15,400,601 | `176dc3220de7db34d3b3aecbfa42043a6038348b` | `8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba` |

Actual Linux Tesseract 5.5.0 ran with the selected profile directory, `-l eng
--oem 1 --psm 6`, a 30-second process deadline and one OpenMP thread. Both
profiles returned exit 0, empty stderr and the exact expected text after
whitespace normalization from the same generated two-line image:

```text
Research libraries preserve knowledge.
English OCR model verification 1234567890.
```

The fixture used DejaVu Sans at 48 pixels on a white 1450-by-230 RGB image.
Both complete actual-model requests passed; this small synthetic fixture does
not establish comparative OCR accuracy, real-book quality or general language
coverage. OSD/equ inference, other language/script payload bytes, native Windows
and NVDA were not tested by this smoke.

The application's new `prepare_offline_pack` was then called separately for
each real English model. Both packs passed exact model size/SHA-256, license
text and provenance checks. The existing-schema manifest embeds `Apache-2.0`
followed by a newline and the full exact source license; the separate
`LICENSE.txt` is also preserved in the prepared pack. Final `models.json`
SHA-256 values:

- fast: `583b3f9b09d56d10cbf012b4634c07c2a32f2c601bf19eb84917766530c7d91f`;
- best: `865e26ee33772d1ee3b8388849dfdff73377793ab08b351b779c7d90d7405ca4`.

Both prepared packs were passed to the actual existing
`language_manager.install_offline_pack` and installed into separate new local
directories. The consumer and its `durable_state` dependency were imported from
the independent owner worktree without editing those modules. Checks confirmed
exact installed model bytes, byte-identical installed manifests and the full
embedded upstream license. Tesseract then read each installed directory using
the same synthetic image and OEM/PSM settings: both returned exit 0, empty stderr
and exact normalized expected text. This validates the real preparation,
installation and recognition path for these two English models only.
