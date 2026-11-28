# Native acceptance repairs after r7

Frozen r6/r7 execution is retained; its commits and results are not rewritten.

The native bibliography probe still expected the old UI worker string result.
The UI now returns an ExportReceipt so it can report completed publication even
when private staging cleanup needs attention. The repaired probe checks the
receipt's exact destination and cleanup state, then compares published bytes
with its existing independent text/HTML render. All 108 native checks remain.

The standard-installer driver compared the console loader's PE file version
with the compiler engine's minimum version. Inno's console source loads
ISCmplr.dll and obtains the engine version through ISDllGetVersion. The repaired
driver compiles one fixed synthetic script with output disabled; the script
requires preprocessor Ver >= 0x06040000, and the actual engine banner must
identify exactly one version >= 6.4. Executable and engine hashes must remain
unchanged. A 30-second owned-process timeout remains fail-closed. The exact
engine digest is carried through the B handoff and rechecked for A-to-B proof.
No compiler is downloaded, the minimum is not reduced, and no setup executes
during version discovery.

Primary implementation reference:
https://github.com/jrsoftware/issrc/blob/is-6_7_1/Projects/ISCC.dpr
Primary preprocessor contract:
https://jrsoftware.org/inno-setup/documentation/topic_predefinedvars.htm

The new exact-module native wrapper also rejects omission of a probe's required
internal source-binding record. Current audio probes already emit that record.

Actual frozen r6 capacity now passed all 1,000/10,000/50,000 synthetic catalogue
cases with 10/100/500 result pages. The 50,000 case measured P50 2.9613811 s and
P95 3.1525123 s per page, and peak process RSS 116,559,872 bytes. It does not
measure large-book import throughput, real user corpus quality, neural
inference or manual accessibility.

Actual frozen r6 audio passed 913 cases and 22 subtests, with seven explicitly
reported optional skips, native Studio 16 and native effects 109. These results
do not qualify the later r7 clip/master/publication additions.

This repair source requires fresh Windows execution. Full platform, physical
audio, manual NVDA, production authority/signing and rights remain separate.
