# MSVC native launcher: exact 2dfc5d32

[Run 46111569613](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46111569613), [job 140876044245](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46111569613/job/140876044245), completed successfully at 2026-11-28T06:08:17Z. Actual checkout: `2dfc5d32558c722ba05068dfff9ebb19eeae52f7`, tree `14a6dc1ce55f5c9d885fbb88b7a50701187acb80`.

The real Windows MSVC build passed all nine native launcher checks using the synthetic child. All seven reported source hashes independently match the selected commit. The private executable is 15360 bytes with SHA-256 `d02013f8ab6b1de4eddb851dac83846454fe037fe858d0f2a03a22be9f8e364b`.

The later probe binds that same executable hash and reports native execution. Its actual-Python-installation, NVDA and Authenticode fields remain false. The static build record precedes execution; its windows_execution_verified=false does not replace the later probe result.

The complete decoded log is retained with its UTF-8 byte count and SHA-256. Build and probe records are extracted from the log, where each is printed twice identically. They are not original artifact-member files or duplicate trials. The workflow uploads no artifact or executable. The separate Zig receipt retains its own passed trial; GitHub now reports the whole two-job workflow as successful.
