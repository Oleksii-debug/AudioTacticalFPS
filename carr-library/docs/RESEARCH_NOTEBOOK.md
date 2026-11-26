# Search your research notebook

Use **Research, Search saved research** to find material you have already saved
to research topics in the open library. This searches source quotations and your
own notes across topics, including nested topics. Book-text search remains in
the Search section.

## Find material across topics

1. Open a library or one of its research topics.
2. Choose **Research, Search saved research**.
3. Enter a word or phrase. Choose quotations, research notes, or both.
4. Optionally enter an exact tag to restrict the search to tagged evidence.
   A tag alone can be used without a text query.
5. Run the search and choose a result with the keyboard.

The result identifies its topic, source, matching field and archive state.
Source text and the researcher's note remain separate. A match in a note never
becomes a quotation attributed to an author.

Text uses Unicode word-phrase matching, with case-insensitive matching and
preserved accents. Punctuation between words is not an exact-byte constraint.
Boolean operators and wildcards are not executed as search instructions.
Search text cannot create a phrase across two source pages. The displayed
quotation retains its original source text, regardless of query normalization.

Tag matching is exact, including case. The search stays within the currently
open library. Identically named topics or tags in another library are excluded.

## Archived material and navigation

The default search includes active topics. A topic below an archived parent is
also excluded. Select **Include archived topics** to search those saved records
explicitly. Their archive status is displayed; finding a record does not
restore its topic.

Open a result's topic to continue working with the saved material. Existing
unsaved research notes must be preserved or handled by the normal draft
confirmation before changing topics. Clipboard and navigation actions reload
the selected record and validate its source again.

## Integrity and limits

The service uses the existing saved-evidence codec, historical metadata
attestation and retained originals. It does not generate source passages or
grant extraction/page-review approval. Changes to a source, evidence payload,
review or authorization can make a result unavailable; a failed validation
does not authorize use of an older cached quotation.

Each operation runs in one authorized database snapshot, with a shared timeout
and cancellation budget. The search reports the number of matching records and
whether the displayed result limit was reached. It does not silently report a
partial, interrupted scan as complete. A bounded candidate population prevents
unlimited memory use; use a tag or the existing individual-topic search when a
library exceeds that bound.

Searches do not change the database schema, original documents, quotations,
notes, tags, archive state or saved topic queries. Existing backup and restore
include the underlying records without a separate search database.

## Design sources

The workflow was informed by these primary product documents, reviewed on
2026-11-26:

- [Zotero: Searching](https://www.zotero.org/support/searching) documents library
  searches that include notes and tags, explicit field criteria and a selected
  library scope.
- [Zotero: Collections and Tags](https://www.zotero.org/support/collections_and_tags)
  describes topic/project organization and tag-based filtering.

Those documents establish useful research workflows. They do not certify this
implementation or supply its code. Our source-integrity rules, archived-topic
behavior and bounded execution are specific implementation decisions.

Native keyboard automation and manual NVDA acceptance are recorded separately.
Passing native control tests does not establish that every NVDA announcement
has been manually checked on Windows 11.
