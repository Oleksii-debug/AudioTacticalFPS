# Actual standard update artifact diagnostic

The inert diagnostic [run46057927777](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46057927777) completed at `b3206b08c6b0dde95e1de40a7fd0c958339dadfb`. It read the exact failed Windows artifact15226302196 after validating its run, head,1176055-byte size and SHA-256 `3a3661a91f7bb49d3869e6f6f6f4018af947558266e61f6cc6a37cb200a54ee7`. No archive content was extracted or executed.

The complete archive has71 members. Fifteen of22 fixed text/JSON paths were present and read. The clean installation, repair and uninstall proof records six passed native checks at source `480f8ecee5e7eee108245391fb617cf8eaa7bfa5`. The A and B compiled setup inputs are independently bound to different source commits and setup hashes.

The update log is1148 bytes, SHA-256 `5e7dd62e445215aa814237237a3a76b8ae60be6f2c8763bc56a1c09a8b2804ee`. It contains only the fixed-system-helper identity and the observed failure from the first controlled upgrade: `The setup did not activate the reviewed new generation.` The call is probe line241 into standard installation line476.

**The actual archive contains no A-to-B Inno setup log or transaction snapshot.** The update native/proof/library/restart/verdict reports are absent. Therefore this artifact cannot establish why B was rejected. The successful clean-install logs belong to a separate operation and do not answer that question.

At the failed source head, the standard host does not request an Inno `/LOG`, discards the owned setup process output, and the wrapper uploads only fixed files under `ci-results`; the failed native proof directory remains private to the runner. A later exact-head run needs bounded owned setup and activation diagnostics while preserving the existing activation/health assertions.

The first reader attempt stopped at the separate clean-uninstall log,5399251 bytes, because the4MiB per-member limit correctly refused it. The successful successor narrowed the fixed selection rather than enlarging archive or member limits. Its full inventory, selected member hashes, original receipts and observed update trace are retained in the accompanying JSON.

This is a completed evidence read. The original standard update run remains failed, with no production-authority, Authenticode or NVDA qualification.
