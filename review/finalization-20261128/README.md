# Finalization evidence

The source snapshot on this evidence branch is PR143 head `71ca92be691455d0ff51ed163b1d91749e5ae62e`. Its required main, V3 and Linux 2 GiB acceptance failed; it must not merge.

[Current status](status.json) and [continuation checkpoint 19](checkpoints/correction-cycle-19.json) identify the exact prepared source `57bd81ebabc0df2ffc4106c44b94805ee2da7588`, reviewed corrections, completed focused acceptance and the active full native preflight. That source has not yet replaced the PR head. [PR description](pull-request.md) records implementation scope and qualification boundaries.

## Evidence groups

- `components/`: actual source, installed and native component contracts on their recorded heads.
- `gates/`: maintained workflow results, including completed failures and independent successes on 71ca.
- `preflights/`: actual syntax/native outcomes and bounded decoding of the failed preparation artifact.
- `reviews/`: finite source and full composition reviews, separate from runtime acceptance.
- `corrections/`: qualified importer, inventory and fixture corrections, with predecessor failures retained.
- `capacity/`: actual metrics for eight passed tiers, Linux 2 GiB failure and the bounded saved-report reader checkpoint.

Every prior record remains. No isolated component or predecessor PASS qualifies a changed final head. Final integration requires all seven maintained gates and all twelve actual capacity tiers on the exact selected source, including four largest-profile backup/verify/fresh-restore outcomes, then an expected-head merge.
