# Private delivery guide templates

`Research_Library_V3_Academic_Quick_Start_UK.template.txt` is a build template, not a completed release receipt. Its final section deliberately contains `SOURCE_COMMIT`, `APP_ARCHIVE`, `APP_SHA256`, and `APP_SIZE` exactly once. Resolve them against the exact final source and verified archive before delivering an external guide.

For a guide embedded inside the application ZIP, fill only the source commit and application archive name and omit the two lines containing `APP_SHA256` and `APP_SIZE`. A ZIP must not contain its own final checksum or byte count. Produce the external guide with all four values only after archive verification.

The template describes the canonical Resonance V3 plus Academic UI, keeps the existing installation and user-data folder names, and records the actual backup boundaries. It contains no private credentials, recordings, user documents, storage identifiers, or fabricated archive identity. The mandatory OCR corresponding-source companion remains a separate ZIP that must be extracted into the same installation folder.
