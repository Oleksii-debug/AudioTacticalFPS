# Exact public CHM normalization diagnosis

The read-only Ubuntu diagnostic at `6b3125c33efe03566419055e31d39a90d9359929` completed successfully; the current product still refuses both selected publisher members. This result is diagnostic evidence and does not qualify a Windows conversion or a release.

Run **46053526155**, job **140690435657**, artifact **15224489353** (6598 bytes; SHA-256 `9be94ddcacea231351d79487c5d42f021ccf8199faef17d89de6bd77215c7d71`) binds the exact pinned 7-Zip 26.06 installer, both original HTML hashes, unchanged parser source hashes and lxml 6.1.3. All temporary public payloads were removed.

| Member | Actual boundary |
| --- | --- |
| `cmdline/switches/index.htm` | Both streams contain 555 words. At token 4 the inert reader produces `<switch;>::=`, while libxml produces `<switch>::=`. Original syntax starts `&ltswitch>::=`; appending a semicolon to the HTMLParser callback name introduces the extra character. |
| `fm/plugins/7-zip/add.htm` | The original pass has 1827 words. The final strict pass stops after 1076 at serialized line 178, column 2, with stack `html/body/dl`. Consecutive `dd` openings at 165/166 are serialized with two closes 176/178; the inert reader already implicitly closes same-tag definitions, making the second close unmatched. |

The first defect needs exact raw entity semantics in the CHM reading copy. The second needs the admitted optional closing semantics preserved through normalization. Neither result justifies ignoring ordered-word differences or making the final strict reader accept arbitrary malformed markup. General HTML behavior remains unchanged at this diagnostic head.

The paired JSON retains exact hashes, counts, positions and bounded structural evidence. Proposed repairs remain source recommendations until the product owner runs their regressions and the complete Windows publisher corpus.
