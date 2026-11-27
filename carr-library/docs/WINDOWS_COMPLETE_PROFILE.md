# Complete private Windows profile

The complete development profile composes the existing Windows desktop/semantic
lock with the optional encrypted-snapshot lock. Ordinary source installations can
still omit the encryption extra. The private complete bundle includes its three
additional packages so encrypted backup needs no separate user installation.

`packaging/compose_windows_profile.py` reads only already acquired, hash-pinned
Windows wheels. It checks every active CPython 3.11/Windows dependency, rejects
conflicting locks or extra/missing wheels, and produces one exact runtime lock and
a CycloneDX inventory of the actual wheel bytes, versions, dependency edges and
publisher license expressions when available. It does not run a resolver or
download packages. The original locks remain the inputs; there is no separately
editable third list. The current complete profile has 50 dependency wheels.

Example on the authorized build host, with a dedicated wheelhouse and fresh output:

```powershell
python packaging/compose_windows_profile.py --lock packaging/windows-semantic-cp311.lock --lock packaging/windows-sync-cp311.lock --wheelhouse C:\PrivateBuild\Wheels --output C:\PrivateBuild\Profile
```

Pass the generated `runtime.lock` and `runtime-sbom.cdx.json` to the existing bundle
builder. The builder verifies and seals both the application source wheel and
dependency inventory. Wheel-contained notices are preserved; a metadata license
expression is not a review of all bundled native dependencies or distribution rights.

## Native launch entry

The existing source-only native launcher is reused. Build it with
`packaging/native-launcher/build_msvc.ps1`, then pass the fresh private build
directory through `build_windows_bundle.py --launcher-dir`. The admission helper
checks the bounded PE, its exact build report and current launcher source hashes.
Only `ResearchLibrary.exe` and its build report are included; the synthetic child,
compiler objects and test executable remain outside the product.

The bundle remains an **unsigned private development candidate**. These checks
bind the build inputs and inventory; they do not replace an owner-controlled
Authenticode signature, signed updates or final distribution approval. Start-menu
installation, registered uninstall/repair and signed upgrade acceptance remain
separate finalization requirements.

## Installed acceptance

The integrated Windows workflow compiles this source, builds and installs the
complete package with explicit outbound firewall rules, and runs the existing
installed native-entry probe. The installation diagnostic recognizes typed audio,
encryption and launcher flags. It verifies the sealed audio runtime, executes
FFmpeg/ffprobe version checks, performs an actual AES-256-GCM roundtrip and rejects
changed ciphertext. Full snapshot regressions additionally run from the installed
package. Neither a launcher probe nor a ciphertext test establishes NVDA usability,
voice quality, the complete multilingual matrix or successful signed delivery.

Public CI retains textual synthetic verification evidence only. Executables,
models, voice data and personal research files must not be published as artifacts.
