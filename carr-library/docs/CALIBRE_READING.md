# Selected Calibre reading conversion

The Convert menu offers an explicit installed Calibre `ebook-convert` selector for
AZW, AZW3, CHM, DjVu, LIT, MOBI, PRC and TCR. This is the existing document
conversion route: it produces a new UTF-8 reading file and the existing
`carr-reading-derivative/1` provenance receipt. Original files and catalog quotation
authority stay unchanged. These extensions are not added as raw catalog imports.

No converter is discovered, downloaded or enabled merely because a book has one
of these extensions. A selection captures the local self-contained installation;
the queued worker verifies the exact core files before execution. It uses the
existing isolated conversion worker and native process owner. Its selected core
and retained source readers remain with that owner while shutdown is uncertain.
The current no-overwrite, source recheck and exact output-pair publication remain
in force.

The native converter receives a new private configuration/cache/home/temporary
directory, without user plugins or inherited Python/proxy/key-log variables.
Only the selected executable is admitted by the Python worker. Calibre is a
native tool, and these restrictions are **not an operating-system sandbox** or a
complete seal of its system dependencies. The application does not provide a DRM
removal or decryption path.

## Supported reading scope

| Family | Reading scope |
| --- | --- |
| AZW / MOBI | Unprotected MOBI-family books; text conversion is reviewed separately from layout. |
| AZW3 | Unprotected KF8 text. |
| PRC | Mobipocket or PalmDoc header families; other PRC databases are refused. |
| CHM | HTML text extracted by the selected native parser. Active content is not executed by the application. |
| DjVu | Existing embedded text only. Image-only documents fail; this route does not infer OCR. |
| LIT | Unprotected Microsoft Reader book content accepted by the selected native parser. |
| TCR | The supported eight-bit compressed book family. |

Every result records the selected converter version and core digest. Reading
order, printed pagination, tables, pictures, formatting and language quality need
source review. The application does not promote any converted text into verified
quotation material.

## Reproducible native gate

`packaging/calibre.json` pins upstream Calibre 9.19.0 release assets by size and
SHA-256 published by the official GitHub release API. CI prepares this external
tool before blocking egress. It is not added to the product bundle or uploaded
with evidence. Calibre source/license remain upstream GPLv3:
https://github.com/kovidgoyal/calibre/tree/v9.19.0 .

Run with the selected installed product interpreter from outside its source path:

```text
python -I -B scripts/create_calibre_fixtures.py --calibre ABSOLUTE_EBOOK_CONVERT --output NEW_FIXTURE_DIRECTORY
python -I -B scripts/calibre_conversion_acceptance.py --calibre ABSOLUTE_EBOOK_CONVERT --fixtures NEW_FIXTURE_DIRECTORY/fixtures.json
```

The fixture generator uses two source-authored, CC0 English paragraphs. It runs
the selected tool to create genuine MOBI, KF8, LIT, PalmDoc and TCR books. AZW
retains an unencrypted MOBI-family container with the AZW suffix. It also authors
a complete uncompressed CHM directory with two HTML chapters and a DjVu page with
a real JPEG background and optional embedded text. The generated inventory binds
all eight byte hashes and expected phrases; generation evidence separately binds
the exact checked-in generator, selected runtime and authored input.

The acceptance gate must convert **all eight actual inputs** through the installed
`ConversionService`. It verifies text, saved provenance, unchanged originals,
no overwrite, no implicit tool activation, refusal of image-only DjVu and the
unselected configuration sentinel. Selected core and installed source identities
are rechecked after the work. Synthetic header/admission tests and ordinary
process ownership tests are separate evidence and never count as native format
qualification.

The initial source composition is **NOT_RUN**. Only an actual successful gate
qualifies these finite fixtures on its reported operating system and selected
tool version. It does not establish arbitrary-document fidelity, visual rendering
of every format, private assets, OCR quality or legal rights in other documents.

Public format references used by the authored fixtures:

- CHMLib exact parser blob:
  https://api.github.com/repos/jedwing/CHMLib/git/blobs/ffd213c4f66d4319b7bf95ef31eac78b65dad6b9
- CHM internal metadata:
  https://www.nongnu.org/chmspec/latest/Internal.html#SYSTEM
- DjVu reference and alternative JPEG/text chunks:
  https://djvu.sourceforge.net/doc/djvu3spec.djvu
- Calibre selected plugin source:
  https://github.com/kovidgoyal/calibre/tree/v9.19.0/src/calibre/ebooks/conversion/plugins
