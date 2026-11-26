# Academic library expansion

User direction, 2026-11-26: extend the product beyond the original search and
quotation scope into a professional, multifunction academic library. The first
implementation increment is version 0.2; future items below are not claims of
implemented functionality.

## Implemented increment

| User task | Implementation | Integrity requirement |
| --- | --- | --- |
| Keep references before obtaining documents | Workspace-scoped book/article/thesis/report/web reference records | No dummy source document or quotation authority |
| Plan a literature review | Reading state, priority, progress, rating, tags, abstract and notes | Personal notes remain editable and separate from sources |
| Find material in a large reading list | Metadata search, status/tag filters, ordered pages and counts | No implicit global search or silent first-page-only export |
| Reuse existing bibliographies | RIS and CSL JSON preview with explicit selection and warnings | One transaction; unsupported data disclosed; no attachment reads |
| Check repetitive imports | DOI, ISBN and conservative title/author/year duplicate groups | Suggestions only; never automatic deletion or merging |
| Move references into a writing workflow | RIS, BibTeX, CSL JSON, CSV and plain bibliography export | Notes opt-in; format escaping; no named-style conformity claim |
| Read an attached document | Link an existing local edition and open a verified viewing copy | Original bytes and historical evidence stay unchanged |
| Read retained books | Full-source reader, EPUB sections, named bookmarks and last-page resume | Source identities and unverified text remain explicit; bookmark state has a separate export |
| Inspect the source catalog | Completeness reports, retained-file duplicate groups, bibliography and review-matrix exports | Reports do not infer source verification or silently merge records |
| Find prior research | Search quotations and personal notes across topics, with exact tags and explicit archive scope | Source-backed quotations remain separately authenticated; unsaved drafts are protected |
| Upgrade and recover | Exact V3-to-V4 migration and V3/V4 backup validation | Transactional migration; schema identity; archived data retained |

These are implemented using the existing SQLite, native wx controls and pinned
RIS components. No additional required runtime dependency or hosted service is
introduced. Existing exact quotations, OCR review, source retention and local
semantic search continue to use their original authority boundaries. The composed
EPUB increment adds the separately pinned defusedxml runtime for XML parsing.

## Research basis

Official documentation reviewed on 2026-11-26:

- [Zotero duplicate detection](https://www.zotero.org/support/duplicate_detection):
  library-scoped DOI/ISBN/title comparisons and deliberate duplicate resolution.
- [JabRef reading and ranking fields](https://docs.jabref.org/finding-sorting-and-cleaning-entries/specialfields):
  reading state, ranking and priority as useful research tasks.
- [Zotero bibliographic formats](https://www.zotero.org/support/dev/data_formats):
  RIS, BibTeX and CSL JSON as interchange formats.
- [JabRef import inspection](https://docs.jabref.org/collect/import/importinspectiondialog):
  preview and select references before adding them.
- [Zotero related items](https://www.zotero.org/support/related):
  explicit relationships between versions, chapters, reviews and associated work.
- [JabRef groups](https://docs.jabref.org/finding-sorting-and-cleaning-entries/groups):
  saved criteria can form dynamically maintained reading groups.
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/):
  an optional future metadata source; remote bibliographic data is not source
  verification and does not grant access to full text.

This is behavioral research, not source-code reuse from those applications.
Existing pinned vendor notices remain authoritative for code actually bundled.

## Subsequent development priorities

1. Saved academic filters and explicit relationships between references, with
   the same workspace isolation, backup and keyboard requirements.
2. Structured personal/institutional author names and a pinned CSL processor for
   named citation styles. Validate representative bibliography and in-text
   outputs against official processor fixtures before claiming conformity.
3. Optional identifier lookup with a visible preview and explicit network action,
   provenance and timestamps, rate-limit handling and an offline fallback.
4. BibTeX/BibLaTeX import through a maintained parser with license/pin review,
   macros and name parsing tested against real interchange cases.
5. Include the reading desk bookmark/resume sidecar in a single checked library
   backup; the present reader exposes its own explicit export/import workflow.
6. User-reviewed duplicate consolidation with relationship and reading-note
   preservation, reversible history and no historical citation retargeting.

Full Word/LibreOffice integration, shared institutional access, automatic
retraction screening, cloud synchronization and automated literature conclusions
are separate design and acceptance tasks. They are not exposed as unfinished
buttons in the current product.
