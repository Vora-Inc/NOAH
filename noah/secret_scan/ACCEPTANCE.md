# Secret scanner: acceptance tests and scope

The acceptance tests cover: the uniform-separator rule (a real
phrase with one odd separator among uniform ones still hits; a code line with mixed
separators stays clean); the first and last word of each line are scanned as their own
streams (consecutive lines, nearly all distinct); a machine-readable run summary; an
expected-refused list (refusing a listed name is normal; any other refusal fails; a listed
name that is absent is a note, a failure only in strict mode). Test cases are in
`acceptance_cases.json` (templates; every secret-looking value is generated at test
time, none is stored). Run the battery as a test; its result table
(each shape, in scope: caught or missed) is reported.

The scanner is a safety net for PLAINTEXT inputs only. It is not a gate after
encryption, and a human always reads the first real bundle before it leaves.

## 1. What the real inputs are (so what can be produced)

| Input | Format |
|---|---|
| Release tree | `.py .ts .tsx .js .sh .toml .yaml .yml .json .md .txt .cfg .ini .lock .css .html`, `.example` files (`.env.example`), `.sha256` manifests; bare names `LICENSE`, `.gitignore`, `Makefile`. Survey of the extractor's output tree: nothing else belongs in a release (venvs, `node_modules`, `.pyc`, `.so`, `.whl`, `.pem` are not release content). |
| Day bundles | `fixture.json score.json settings.json chain-refs.json MANIFEST.json README.txt SHA256SUMS` |
| Addresses document | `WALLET_ADDRESSES.md` |
| Backup inputs | `loop.json`, `commit-ledger.json`, `scores/day-N.json`, dials JSON, systemd `.service`/`.timer`/`.mount`/`.socket`/`.path`/`.conf` files, install manifests, the one-line `software-commit` file, `env-allowlisted.txt`. SQLite `history.db` / `daily-deposit-store.db`: today they are BINARY snapshots (refused, not opened). A text `.dump` does not exist yet; its `INSERT ... VALUES(...)` rows stay IN scope because a text dump is the likely future form. |

Allowed extensions (anything else is refused by name, never opened):
base (bundles, backup): `.json .txt .conf .service .timer .mount .socket .path .sql .md`, bare names
`SHA256SUMS`, `software-commit`;
release adds: `.py .ts .tsx .js .sh .toml .yaml .yml .lock .example .cfg .ini .css .html .sha256`,
bare names `LICENSE`, `.gitignore`, `Makefile`.

## 2. How skipped files are reported (condition 1 and 2)

* Refused by name, NOT opened, never counted as clean, exit 3 and a distinct count:
  archives (`.zip .tar .tgz .gz .bz2 .xz .7z`), encrypted bundles (`.gpg .tar.gpg .age`),
  sqlite (`.db .sqlite .sqlite3`), unknown binaries and every other extension. The
  decoding code for archives is removed (no decompression, no caps for it).
* Wallet and keystore file names (`.env`, `*.pem`, `id_rsa*`, `keystore*`, `*mnemonic*`,
  `coldkey`, `hotkey`, `wallet.dat`, `*.gpg`, `*.kdbx`, `*.ppk`, `seed.*`, ...) are
  refused by name, not opened.
* Ping-URL files (the Healthchecks URLs are secrets by design) are excluded ONLY by an
  explicit named list in the caller's configuration; never by filename
  pattern. The CLI/library prints the count of excluded files.
* Every run prints one summary: files scanned, refused, excluded, findings. Zero files
  scanned is an error (exit 2), never clean.
* `.git` directories, symlinks (also dangling), FIFOs/sockets, unreadable files and
  invalid UTF-8 in a text-extension file are findings.
* Text files over 32 MiB are a finding (`file-too-large`).
* Output never contains matched text. Paths are printed with secret-looking components
  masked (token shapes, mixed case plus digits, a secret word with a random suffix,
  over 24 characters). Nothing derived from content is printed as a name.

## 3. Shapes IN scope (must be caught; test case ids in the JSON)

Recovery phrases (BIP39 English, 12/15/18/21/24 words), all of these layouts:
space, newline, comma, comma without space, CRLF; numbered `1. a`, `1) a`, `(1) a`,
`#1 a`, `001 a`; labelled `word1: a`; Markdown bullets, `**word**`, table rows and
table cells; JSON array of strings, JSON string with spaces or `\n`; SQL
`VALUES (0,'a'),(1,'b')`; Python/TS quoted lists including `b'x'`, `u'x'`, `r'x'`;
HTML cells; a phrase inside a longer source line; one typo in 12 words (best effort);
`abandon x11 about` and phrases with repeated words; base64 of a phrase inside text
(single, wrapped at 64/76 columns, base64 of base64, depth at most 3; deeper gives
`decoded-too-deep`).

Per-line prefixes of non-list words are covered by scanning each line's last
token as its own stream. The test corpus includes this form.

Env value forms with punctuation: `Pa(55)w0rd!xyz` on a `NAME=value` or
`key: value` line, and quoted systemd `Environment="NAME=value with (punctuation)!"`; placeholders and code in the
same forms stay clean.

Other secrets: `NAME=value`, `NAME: value` and systemd `Environment=` lines, also with
trailing text on the line (first token of an unquoted value); camelCase, snake_case and
kebab names; passphrases with spaces; trailing `# comment`; raw 64 or 128 hex after
a secret keyword (a benign label such as "hash" does not defeat it); raw 64/128 hex
under `hotkey`/`coldkey`/`address`; 32-byte base64 and base58 keys after a keyword;
PEM/OpenSSH/PGP/PuTTY headers; token shapes (GitHub, provider access keys, JWT, Slack/Discord webhooks,
npm, Stripe, Google, Hugging Face, Telegram, SendGrid, Healthchecks ping and API
keys, `user:pw@` URLs, Basic/Bearer); the placeholder rule hides only short words
(`your-token`, `changeme`), never `your-<40hex>` or `your-actual-Pa55w0rd`.

Must stay CLEAN (negative corpus): public SS58 addresses, 0x block hashes and sha256 sums
(including `sha256sum` lines), history `INSERT` rows with addresses and hashes, score
numbers, empty/placeholder env values, paths, prose that merely mentions the words,
code that uses secret-like names, repeated list words in code, "private testnet".

Performance (a hang on the backup path is a denial of service): 100 KB on one line under
1 s; 1 MB on one line under 10 s; a 50,000-object one-line JSON array under 10 s; a deeply
nested JSON document never crashes the scan.

## 4. Shapes OUT of scope, each with its reason

| Shape | Reason |
|---|---|
| URL-encoded words (`a%20b`, `a+b`) | No real input is URL-encoded text; ping-URL files are excluded by an explicit list |
| Pipe-joined words outside a Markdown table | No input writes it |
| JSON Lines with one word per object | No input writes it |
| Separators `\ -> = & ~ * @ ^ $ { (` between words outside a code-like context | No input writes them; counting them as phrases would flag source code |
| Archives, encrypted bundles, sqlite, unknown binaries | Refused by name, never opened (see section 2): the scanner cannot look inside them |
| Base64 of an archive/gzip inside text | Archives are not decoded; documented residual risk |
| A phrase split across two files | Per-file scan cannot see it |
| Dockerfile `ENV TOKEN <hex>` | No Dockerfile is in the extension list, so such a file is refused by name |
| A 40-character provider secret without a secret-assignment keyword | Keyword-less: nothing marks it as a secret |
| Seeds written as integer lists (`[12, 7, 301, ...]`) | Not a shape any input produces; no word or key pattern applies |
| rot13, hex-encoded or reversed text, other encodings | Not decoded |
| Two typos in one 12-word phrase, 11-word runs, filler words (`and`, `then`) between words | Documented gaps |
| URL-style, `hcr_` etc. beyond the listed token shapes, secrets in a format with no shape | Pattern matching cannot know them |

## 5. Residual risk (to be repeated in the README)

The scanner cannot catch: secrets inside archives, compressed or encrypted files (these
are refused by name and the human must open them another way); phrases or keys split
across two files; encoded or obfuscated secrets (rot13, hex text, URL encoding, reversed
text, base64 of an archive); a secret in a shape not listed here. It reduces leaks; it
does not prove their absence. Condition: the first real bundle is always read by a human
before publication. Scan actual plaintext inputs as well as the synthetic test
corpus, and review every reported finding.

## 6. Run the tests

From the source root, run `python -m noah.secret_scan.tests.battery`.
The result records which specified cases pass. A passing synthetic corpus
establishes only those cases; it does not certify a repository or backup.
