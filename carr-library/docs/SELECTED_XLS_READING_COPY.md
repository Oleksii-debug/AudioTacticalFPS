# Selected XLS reading copies and inactive page decorations

The explicit LibreOffice XLS conversion route creates an ODS reading intermediate. It can omit narrowly recognized page header/footer templates whose own style:display is exactly false. This is limited conversion support: the resulting library source is the reading text, and the exact original XLS plus its provenance report stay in the selected reading-copy folder. It grants no native XLS citation, original pagination, quotation approval, or independent extraction-fidelity guarantee.

## Why this explicit option exists

The actual pinned LibreOffice 26.8.3.1 qualification run produced 12 disabled header/footer nodes in styles.xml. Eight were empty. Four contained paragraph/span sheet-name or page-number fields. The earlier strict extraction correctly rejected the populated disabled structures. The captured report retained only bounded structural names, attribute shapes, booleans and hashes; no original XML or source values were published.

OASIS OpenDocument 1.3, Part 3, section 19.475 defines style:display="false" on page headers/footers as not displayed. Sections 16.10–16.15 place the six header/footer elements under a master page. These page decorations already lie outside this reader's content.xml/office:body extraction.

Primary specification:
https://docs.oasis-open.org/office/OpenDocument/v1.3/os/part3-schema/OpenDocument-v1.3-os-part3-schema.html

Actual predecessor evidence:
Git blob b45aa906123bd3a5654cd9175fbf5cfc93886ccf, exact head 89ba80ffc2cf7e21e30e5be0cd09f8c8060d3b59, run 46093750313. This records failure under the old strict policy; it is not qualification of the new option.

## Exact boundary

Only the fixed selected .xls → .ods adapter supplies allow_inactive_page_decorations=True. Ordinary ODF ingestion and other selected formats keep their previous behavior. The option requires an actual boolean and is rejected for every other format. Source-controlled request fields cannot select it.

A populated exemption requires the exact hierarchy office:document-styles / office:master-styles / style:master-page / style:{header,header-first,header-left,footer,footer-first,footer-left}.

The decoration has only style:display="false", with no non-whitespace root text or tail. The closed descendant profile allows paragraphs without attributes, spans with at most a text-style name, and leaf sheet-name/page-number fields without attributes. It admits at most 256 descendants and three descendant levels. Unknown attributes, elements, placement and ambiguous display values are refused.

The complete existing XML active-content, hidden-content and inherited spreadsheet visibility checks still visit all nodes. There is no subtree skip. Body, cell and style rejection rules, archive/XML/text budgets, cancellation and absolute deadlines remain active.

The fixed warning inactive_page_decorations_not_read is attached to the conversion's normal fidelity report whenever a populated inactive template is omitted. The exact original and intermediate hashes, reading text, logical locations and unverified status remain bound by the existing three-file commit. The actual native XLS gate requires this warning in the persisted report.

## Verification scope

The portable tests parse real synthetic ODS ZIPs, reproduce the observed 12-node structure, compare exact cell bytes and locations, and preserve the strict default refusal. Adversarial controls cover closed structure and placement, hidden or active content elsewhere, inherited spreadsheet visibility, independent node limits, true-return cancellation and invalid option types/formats.

The Office adapter tests stub only the external program while retaining actual reviewed component readers and the real intermediate parser. They establish that only the XLS route selects the option; they are not native-format qualification.

The separate existing Windows gate still requires all 14 actual selected formats, the entire old 34 conversion suite and 30 native GUI checks in both source and installed modes. New execution and actual format-quality claims remain pending until those runs complete. No physical-device, screen-reader or model claim follows from these checks.
