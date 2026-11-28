# Native bibliography probe: canonical Windows paths

The frozen f4d2bbbf71da11369833ae3825f7841fed6ad0b8 run failed its combined receipt/output/status assertion after an actual export future completed. That failure is retained, including its run and artifact identity. Its log did not identify which predicate failed; the successor must establish this through execution.

The probe now resolves its owned temporary root before constructing any destination. Windows TEMP can expose a short alias such as RUNNER~1, whereas export receipts intentionally contain a resolved path. All existing byte equality, UTF-8 content, cleanup, native status/focus and overwrite conditions remain, with the same 108 expected native labels. Additional boolean diagnostics identify any remaining predicate failure without publishing user content. No product behavior is changed by this probe correction.

Independent publication evidence in this commit is an actual PASS on frozen 523aa40f9215569a276f0625d09fcc0fa84bb520: 17 source and 17 installed boundary cases, three synthetic EPUBs checked with EPUBCheck 5.3.0 and three synthetic DAISY books checked against pinned local DTDs, plus deliberately invalid controls. Reader interoperability, all DAISY rules, manual NVDA and speech quality remain NOT_RUN.
