# Encrypted transfer dependency provenance

The optional sync extra uses PyCA cryptography 50.0.2. The Windows desktop and
full offline inventories include its three exact dependencies so an explicitly
selected encrypted transfer does not require a network installation at runtime.
Ordinary reading imports no cryptographic runtime. A selected encrypted operation
must fail if its required runtime is missing; it must never fall back to plaintext.

The following publisher wheel hashes were independently rechecked on the official
PyPI release file pages on 2026-11-27. The donor lock came from PR99 commit
5f18481654277425ab8af170575de978e7a2a69c, blob
cd9c18a24d7a7ac203d81cf6a8d82d8bcb8f8ab3. This source check is not a claim
that this run has downloaded or executed the wheels.

## cryptography-50.0.2-cp311-abi3-win_amd64.whl

- Release: https://pypi.org/project/cryptography/50.0.2/#files
- SHA-256: `7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c`
- Declared license: Apache-2.0 OR BSD-3-Clause

## cffi-2.1.1-cp311-cp311-win_amd64.whl

- Release: https://pypi.org/project/cffi/2.1.1/#files
- SHA-256: `42f6930c31dc7f50732c9ae793c2786c7b6b044195967bbdde40bb9be81c4cc0`
- Declared license: MIT-0

## pycparser-3.12-py3-none-any.whl

- Release: https://pypi.org/project/pycparser/3.12/#files
- SHA-256: `a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e`
- Declared license: BSD-3-Clause

Cryptography's wheel has a PyPI trusted-publishing attestation for upstream
commit fe12d283180ea89ceed622021242fddb31d27f84. The other two file records do
not claim trusted-publishing provenance. All 3 wheels still require actual
publisher hash matching during the existing no-build installation. The current
bundle builder checks transitive Windows dependency closure and preserves the
original wheel archives and their license material. The installed inventory must
match the complete lock and SBOM on the exact candidate.

The SBOM retains the prior uv output and identifies these 3 additions explicitly
as supplemental publisher-reviewed entries. Its old uv timestamp is not evidence
of a new uv resolution. Real AES-GCM/scrypt tamper, cancellation, archive and
fresh-destination restore checks are required by the sync suites. Native UI and
complete-bundle checks do not establish cloud-provider or multi-device conflict
policy beyond the tested immutable local exchange directory.
