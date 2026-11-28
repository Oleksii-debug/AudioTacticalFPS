Local audio runtime for Universal Research Library

The optional Windows x64 audio engine is the exact Gyan FFmpeg 9.1 full build
identified in source-lock.json. The official FFmpeg download page links to this
publisher. Full builds contain libflite; the essentials package does not.

Setup is explicit. Supply an already downloaded archive with --archive, or opt
into the pinned public download with --download. The running library never
downloads this runtime automatically. No account, API key, document, voice,
recording, model or private path is sent by the setup request.

The archive size and SHA256 are checked before opening it. Safe extraction
rejects links, duplicate paths, traversal, special devices, alternate streams,
encrypted entries and excessive file counts/sizes. Original publisher license
and documentation files remain unchanged. The prepared directory has a checked
manifest binding all files, executable SHA256s and the pinned source lock.

Use scope: private personal installation. Do not publish runtime archives or
private recordings from this project. Gyan describes these builds as GPLv3.
Their compiled dependency set is broader than FFmpeg's default LGPL build.
Public redistribution is not covered by this preparation step: the exact
corresponding source, dependency licenses and other obligations must also be
satisfied. Preserve the publisher's original LICENSE and docs in every private
copy. No MusicGen weights, SoundFont or SAPI voice is included here.

Native Windows execution is checked separately; preparation on another OS is
reported as NOT_RUN for Windows. The actual filters, codecs and voices are
discovered from the installed runtime. No unsupported feature is assumed from
a download alone. The audio suite tests real PCM duration, channels, ducking,
effects, cancellation, no-overwrite publication and long streaming assembly.
