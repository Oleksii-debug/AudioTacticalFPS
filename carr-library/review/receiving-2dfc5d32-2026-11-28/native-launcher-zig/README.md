# Exact 2df pinned-Zig native launcher receiving

Job 140876044090 in run 46111569613 completed successfully on checkout `2dfc5d32558c722ba05068dfff9ebb19eeae52f7`. The pinned official Zig 0.14.1 archive was verified, the private launcher was built, and the matching executable passed all nine named synthetic-child checks on actual Windows.

Tested launcher SHA-256: `58e3de03d9d09520833841bbc72f698f5b924631e0490f41d8b73dc73da53cf7`. All five reported source hashes independently match the selected Git blobs. Both repeated build and probe records in the log are byte-equivalent as parsed JSON.

This receipt does not qualify the queued MSVC job. The probe explicitly leaves actual Python installation, NVDA and Authenticode unverified. The build is an unsigned private candidate, and the workflow uploads no executable or artifact. The complete connector-decoded log is retained; extracted JSON files are labeled as log-derived rather than downloaded runner-file bytes.
