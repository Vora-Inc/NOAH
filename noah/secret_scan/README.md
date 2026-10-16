# noah.secret_scan

The shared secret scanner for plaintext release and backup inputs. Standard library only.
Run the agreed acceptance battery with `python -m noah.secret_scan.tests.battery`
(scope and reasons in `ACCEPTANCE.md`, cases in `acceptance_cases.json`).

    python3 -m noah.secret_scan [--release] [--json] [--strict] [--config FILE]
        [--excluded PATH ...] [--expected-refused NAME ...] <file-or-dir> ...

Exit 0 clean, 3 a hit or an unexpected refusal, 2 an error or nothing scanned. It prints
`path:line: kind` with secret-looking path parts masked, plus a one-line summary
(scanned, refused, excluded, findings); with `--json` the same as a JSON object. It never
prints matched text.

## What it scans, what it refuses

Scanned: `.json .txt .conf .service .timer .mount .socket .path .sql .md`, the names `SHA256SUMS` and
`software-commit`; with `--release` also `.py .ts .tsx .js .sh .toml .yaml .yml .lock
.example .cfg .ini .css .html .sha256`, `LICENSE`, `.gitignore`, `Makefile`.

Every other file (archives, `.gpg`, `.tar.gpg`, sqlite, unknown binaries) is refused by name
and never opened. List the ones that are binary by design with `--expected-refused`; any
other refusal fails the run. Ping-URL files are skipped only when named in `--excluded`.

## What it can no longer catch

* Anything inside an archive, a compressed or encrypted file, a sqlite file or any other
  refused file. Someone must look at those another way.
* A phrase or key split across two files.
* Encoded or obfuscated secrets: rot13, hex-encoded text, URL-encoded text, reversed text,
  base64 of an archive.
* Phrases with two typos, 11-word runs, filler words between words, phrases whose
  separators are not uniform.
* Dockerfile `ENV TOKEN <hex>` lines (no Dockerfile is in the extension list), a 40-character
  provider secret with no keyword next to it, and seeds written as integer lists.
* A secret in a shape the scanner does not list.

It is pattern matching: it reduces leaks, it does not prove their absence. The first real
bundle is always read by a human before it leaves.
