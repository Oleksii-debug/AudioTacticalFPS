# Installed OCR language selection

The desktop import command now lists the text models in the selected local
Tesseract installation. The native multiple-choice dialog supports choosing
one or several languages and official script model IDs such as `script/Latin`.
Use the arrow keys to focus an item and Space to select or clear it. English,
Ukrainian and Slovak are no longer the only choices. A model without a friendly
language name is shown using its exact installed ID.

Import Center uses the same discovered catalogue for its language selector.
Choose **Choose multiple OCR languages** to create a combination for the next
file or folder plan. A saved queue keeps the languages chosen when that plan was
created; changing the selector does not rewrite an existing queue. Selection is
bounded to 32 simultaneous models to avoid accidentally loading an entire model
catalogue for every page. Installed models are not automatically selected en masse.

English is preferred initially when available. A local installation containing
other languages does not need an English model merely to list or choose them.
The explicit English installation self-check still needs and tests English.
OSD data is excluded from text-language choices because it is used for orientation,
not language identification.

## Integrity and availability

The discovery operation does not download models, scan source documents, or run
OCR. It fully verifies a selected portable bundle's manifest and every inventoried
file before showing its model catalogue. Missing, additional, changed, linked or
corrupt bundle files retain the existing failure behavior; a broken explicitly
selected bundle does not fall back to a different engine. Script models retain
their path-relative identity in both that inventory and extraction fingerprints.

For a system Tesseract installation, discovery runs only the local executable's
`--list-langs` command. The query has a five-second deadline, a 128 KiB output
bound, strict model-name and inventory validation, and no shell command string.
The deadline also applies to portable-file verification. A missing engine or
empty text-model catalogue is shown explicitly. Existing native text extraction
and unavailable-OCR error/review handling remain authoritative.

Installed model availability does not establish recognition quality or approve
source text. OCR results still require the existing source/page review, and
printed-page confirmation remains a separate action. Arbitrary paths, duplicate
model IDs, OSD-as-text, and option injection are rejected. The existing pinned
English/Ukrainian/Slovak profile and its exact model hashes are unchanged.

## Verification

The focused regression set is:

```sh
python -m pytest -q tests/extract/test_ocr_runtime.py \
  tests/extract/test_runtime_identity.py tests/extract/test_page_ocr.py \
  tests/ui/test_ocr_options.py tests/ui/test_import_center.py \
  tests/ui/test_desktop_handlers.py
```

The local implementation run used CPython 3.12.14, Tesseract 5.5.0 and the four
existing locally available language/orientation files, each checked against
`pinned_tessdata.py` before execution. It passed 162 tests without failures or
skips. This includes actual Tesseract OCR, metadata enumeration and fingerprints,
real subprocess timeout/output-limit checks, sealed model tampering, absent
English and custom/script IDs, multiselection/cancellation, queue-config capture,
and the existing exact-source review boundary. Synthetic script-name metadata
fixtures do not demonstrate recognition quality for that script.

This increment does not implement automatic language detection, a downloadable
model manager, per-region language inference, or native-speaker OCR evaluation.
Native Windows/NVDA acceptance is separate from these Linux regressions.
