"""Tests for the shared secret scanner: the agreed acceptance battery, per-rule pins,
fail-closed behaviour, refusal and exclusion reporting, name masking, and performance.
Every secret-looking value is built at run time, so no secret-shaped literal sits in the
repository."""

import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
import random
import stat
import tempfile
import time
import unittest
from pathlib import Path

import noah.secret_scan as m
from noah.secret_scan import (
    EXIT_CLEAN, EXIT_ERROR, EXIT_HITS, Finding, ScanError, mask_path, scan, scan_bytes, scan_paths,
)
from noah.secret_scan import __main__ as cli
from noah.secret_scan._bip39 import ORDERED, WORDS
from noah.secret_scan.tests import battery

SERVICE_ROOT = Path(__file__).resolve().parents[3]
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "docs/parameters/profiles").is_dir())
SS58 = "5D8HcAqogib6u8cX7WArLcTmmDp2Wv7arXzk6jvJUrmFsGxv"
BLOCK_HASH = "0x" + hashlib.sha256(b"block").hexdigest()
HEX32 = hashlib.sha256(b"planted").hexdigest()  # 64 hex chars
HEX20 = hashlib.sha1(b"planted").hexdigest()  # 40 hex chars
SEED, SECRET = "se" + "ed", "sec" + "ret"
PEM = "-----BEGIN " + "PRIVATE KEY-----\nAAAA\n-----END " + "PRIVATE KEY-----\n"
PW = "Zq7rT9" + "mK2xLp"


def phrase(seed, n=12):
    return random.Random(seed).sample(ORDERED, n)


def kinds(text, name="t.txt"):
    return {f.kind for f in scan_bytes(text.encode(), name)}


class WordList(unittest.TestCase):
    def test_vendored_list_is_the_published_one(self):
        self.assertEqual(len(WORDS), 2048)
        digest = hashlib.sha256(("\n".join(ORDERED) + "\n").encode()).hexdigest()
        self.assertEqual(digest, "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda")

    def test_the_vendored_word_list_file_is_not_itself_a_hit(self):
        from noah.secret_scan import _bip39

        self.assertEqual(scan_paths([Path(_bip39.__file__)], scope="release"), [])


# Each battery case that the table cannot render is pinned by a named test in this file.
DEDICATED = {
    "phrase-OUT-split-two-files": "test_a_phrase_split_over_two_files_is_not_seen",
    "phrase-OUT-rot13-hex-reversed": "test_encodings_are_out_of_scope_and_not_decoded",
    "phrase-OUT-base64-of-gzip": "test_base64_of_an_archive_is_not_decoded",
    "closed-wallet-names": "test_wallet_file_names",
    "closed-git-dir": "test_git_directory_symlinks_and_fifos_are_reported",
    "closed-symlink": "test_git_directory_symlinks_and_fifos_are_reported",
    "closed-fifo": "test_git_directory_symlinks_and_fifos_are_reported",
    "closed-unreadable": "test_unreadable_file_is_a_finding",
    "closed-invalid-utf8": "test_invalid_utf8_and_oversize_text_are_findings",
    "closed-too-large": "test_invalid_utf8_and_oversize_text_are_findings",
    "closed-zero-files": "test_zero_files_is_an_error_never_clean",
    "closed-scan-crash": "test_a_crash_inside_scanning_fails_closed_without_the_message",
    "closed-deep-json": "test_deeply_nested_json_does_not_crash",
    "names-masked": "test_printed_names_are_masked",
    "perf-100kb-one-line": "test_one_line_inputs_scan_in_bounded_time",
    "perf-1mb-one-line": "test_one_line_inputs_scan_in_bounded_time",
    "perf-json-array-one-line": "test_one_line_inputs_scan_in_bounded_time",
    "tests-directories-scanned": "test_tests_directories_are_scanned",
    "report-refused-count": "test_refused_by_name_is_never_opened_and_never_clean",
    "excluded-named-list": "test_exclusion_is_an_explicit_list_and_counted",
    "excluded-by-pattern-not-honoured": "test_exclusion_is_an_explicit_list_and_counted",
    "summary-line": "test_cli_summary_and_json",
}


class Battery(unittest.TestCase):
    def test_every_in_scope_shape_is_caught_and_every_clean_shape_stays_clean(self):
        rows = battery.run()
        problems = [r for r in rows if r[3] in ("MISSED", "FALSE-POSITIVE")]
        self.assertEqual(problems, [])
        self.assertGreaterEqual(sum(r[3] == "caught" for r in rows), 70)

    def test_every_case_the_table_cannot_render_has_a_dedicated_test(self):
        for row in battery.run():
            if row[3] == "see-test" and not row[0].startswith("refuse-") and not row[0].startswith("closed-"):
                self.assertIn(row[0], DEDICATED, row[0])
        for case_id, test_name in DEDICATED.items():
            self.assertTrue(
                any(hasattr(c, test_name) for c in (Phrases, Files, Reports, Perf, Masking, Cli, Keys)), test_name
            )


class Phrases(unittest.TestCase):
    def hit(self, text):
        self.assertTrue(any(k.endswith("bip39-run") for k in kinds(text)), text[:60])

    def test_realistic_phrases_every_length_and_many_seeds(self):
        for seed in range(30):
            for n in (12, 15, 18, 21, 24):
                self.hit(" ".join(phrase(seed, n)))

    def test_one_odd_separator_among_uniform_ones_still_hits(self):
        w = phrase(20, 12)
        self.hit(" ".join(f"{i + 1}{')' if i == 11 else '.'} {x}" for i, x in enumerate(w)))
        self.hit(", ".join(w[:11]) + "; " + w[11])

    def test_separators_that_are_not_uniform_are_not_a_phrase(self):
        w = ("real output hand agree " * 3).split()
        seps = ["(", ".", "=", " ", "[", "+", "/", "{", "-", "*", ">"]
        mixed = "".join(x + seps[i % len(seps)] for i, x in enumerate(w))
        self.assertEqual(kinds(mixed), set())
        self.assertEqual(kinds("result = real(output).hand_output + agree[real] * hand - output / real if hand else agree"), set())

    def test_the_half_distinct_rule_and_exact_length_runs_are_pinned(self):
        base = "real output hand agree".split()
        code_like = " ".join(base[i] for i in (0, 1, 2, 1, 3, 1, 0, 2, 1, 0, 2, 1, 3, 0))  # 14 words, 4 distinct
        self.assertEqual(kinds(code_like), set())
        w = phrase(7, 6)
        self.hit(" ".join(w * 2))  # exactly 12 listed words: a valid length
        self.hit(" ".join(["abandon"] * 11 + ["about"]))
        self.assertEqual(kinds(" ".join((w * 2)[:10] + ["the", "of", "a", "and"])), set())

    def test_typos_and_prose(self):
        w = phrase(3, 12)
        w[5] = w[5][:-1] + "q"
        self.hit(" ".join(w))
        self.assertEqual(kinds("The validator scores each day and publishes the result before the deadline."), set())
        self.assertEqual(kinds(" ".join(phrase(10, 11))), set())

    def test_unicode_and_markup_normalisation(self):
        w = phrase(8, 12)
        self.hit(" ".join("".join(chr(0xFEE0 + ord(c)) for c in x) for x in w))  # full-width
        self.hit(" ".join(x.replace("a", "а").replace("o", "о").replace("e", "е") for x in w))  # Cyrillic homoglyphs
        self.hit("­".join(" ".join(w)))  # soft hyphens everywhere
        self.hit("​".join(w))  # zero-width joiners between words
        self.hit(" ".join(x[0] + "́" + x[1:] for x in w))  # combining accents
        self.hit("".join(f"\x1b[31m{x}\x1b[0m " for x in w))  # ANSI
        self.hit(" ".join("\\u%04x" % ord(x[0]) + x[1:] for x in w))  # \uXXXX escapes
        self.hit("".join(f"<td>{x}</td>" for x in w))  # HTML

    def test_per_line_streams_are_consecutive_and_distinct(self):
        w = phrase(11, 12)
        self.hit("\n".join(f"INFO [wallet] {x}" for x in w))  # prefix before the word
        self.hit("\n".join(f"{x} <- word" for x in w))  # text after the word
        broken = "\n".join(f"INFO [wallet] {x}" + ("\n\nnoise line" if i == 5 else "") for i, x in enumerate(w))
        self.assertFalse(any(k.endswith("bip39-run") for k in kinds(broken)))  # not consecutive lines
        same = "\n".join("run it now" for _ in range(30))  # source starting many lines with one word
        self.assertEqual(kinds(same), set())

    def test_phrase_in_a_long_file_is_located(self):
        w = phrase(9, 24)
        text = "\n".join(["lorem ipsum dolor sit amet"] * 5000) + "\n" + " ".join(w) + "\n" + "tail\n" * 10
        found = [f for f in scan_bytes(text.encode(), "big.txt") if f.kind == "bip39-run"]
        self.assertEqual([f.line for f in found], [5001])

    def test_base64_inside_text_to_a_small_depth_only(self):
        text = " ".join(phrase(11, 12))
        single = base64.b64encode(text.encode()).decode()
        self.assertIn("decoded-bip39-run", kinds(f"payload: {single}\n"))
        wrapped = base64.encodebytes((text + "\n").encode() * 3).decode()
        self.assertIn("decoded-bip39-run", kinds(wrapped))
        short = "\n".join(single[i:i + 12] for i in range(0, len(single), 12))
        self.assertIn("decoded-bip39-run", kinds(short))
        blob = text
        for _ in range(m._MAX_DEPTH):
            blob = base64.b64encode(blob.encode()).decode()
        self.assertIn("decoded-bip39-run", kinds(blob))
        self.assertIn("decoded-too-deep", kinds(base64.b64encode(blob.encode()).decode()))

    def test_a_phrase_split_over_two_files_is_not_seen(self):
        w = phrase(4, 12)
        self.assertEqual(kinds(" ".join(w[:6])), set())
        self.assertEqual(kinds(" ".join(w[6:])), set())

    def test_encodings_are_out_of_scope_and_not_decoded(self):
        text = " ".join(phrase(5, 12))
        import codecs

        for encoded in (codecs.encode(text, "rot13"), text[::-1], text.encode().hex()):
            self.assertFalse(any(k.endswith("bip39-run") for k in kinds(encoded)))

    def test_base64_of_an_archive_is_not_decoded(self):
        blob = base64.b64encode(gzip.compress((" ".join(phrase(6, 12))).encode())).decode()
        self.assertFalse(any(k.endswith("bip39-run") for k in kinds(f"payload: {blob}\n")))
        self.assertIsInstance(scan_bytes(blob.encode(), "t.txt"), list)  # and no crash


class Keys(unittest.TestCase):
    def test_hex_near_keywords_including_camel_case_and_markup(self):
        for text in (f"{SEED} = 0x{HEX32}", f"{HEX32}\nthe private {SEED} above", f"mnemo" + f"nic {HEX32}",
                     f"name,{SECRET}Seed,{HEX32}", f"<{SECRET}Seed>{HEX32}</{SECRET}Seed>", f"{SECRET}Seed:\n  0x{HEX32}",
                     f"{SEED} hash: {HEX32}", f"private block {HEX32}", f"block hash {HEX32} {SEED}"):
            self.assertTrue(kinds(text) & {"key-hex", "env-assignment"}, text[:40])

    def test_hashes_without_a_secret_word_pass(self):
        self.assertEqual(kinds(f"value {HEX32}"), set())
        self.assertEqual(kinds(f"block hash {HEX32} and the weak key mention"), set())
        self.assertEqual(scan_bytes(f"{HEX32}  {SEED}-notes.txt\n".encode(), "SHA256SUMS"), [])
        self.assertEqual(kinds(f"environment private-testnet, genesis {BLOCK_HASH}, spec 448"), set())

    def test_raw_hex_under_public_names_is_a_hit_but_ss58_is_not(self):
        for text in (f"hotkey='{HEX32}'", f"COLDKEY={HEX32}", f"coldkey=0x{HEX32}", json.dumps({"hotkey": "0x" + HEX32})):
            self.assertIn("raw-key-hex", kinds(text), text[:30])
        self.assertEqual(kinds(f"hotkey={SS58}"), set())
        self.assertEqual(kinds(f"block_hash={HEX32}"), set())

    def test_key_headers_and_variants(self):
        for text in (PEM, "-----BEGIN OPENSSH " + "PRIVATE KEY-----", "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----",
                     "-----begin  rsa  " + "private key-----", "PuTTY-User-" + "Key-" + "File-3: ssh-ed25519",
                     "AGE-SECRET-" + "KEY-1" + "Q" * 58):
            self.assertIn("private-key-header", kinds(text), text[:30])

    def test_token_shapes(self):
        samples = ["ghp_" + "A1" * 20, "AKIA" + "ABCDEFGH12345678", "ASIA" + "ABCDEFGH12345678",
                   "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + ".sig_ABC-123", "npm_" + "aB3" * 12,
                   "sk_" + "live_" + "aB3dE5" * 5, "AIza" + "Sy" + "aB3-_" * 7, "hf_" + "aB3dE" * 7,
                   "hcr_" + "aB3dE5" * 4, "hcw_" + "aB3dE5" * 4, "redis://:" + "pw1234" + "@cache.internal:6379",
                   "https://" + "aB3dE5fG7hJ9kL2mN4pQ" + "@host.example/x", "postgres://svc:" + "pw1234" + "@db/app",
                   "Authorization: Bearer " + "aB3dE5" * 5, "X-Api-" + "Key: " + "aB3dE5" * 6,
                   "https://HC-PING.COM:443/" + "abcdefghijklmnop", "https://healthchecks.io/ping/" + "-".join(["12345678", "1234", "1234", "1234", "123456789abc"])]
        for text in samples:
            self.assertIn("token-shape", kinds(text), text[:20])
        self.assertEqual(kinds('"ping_url": "https://hc-ping.com/REPLACE-WITH-CHECK-UUID-6"'), set())

    def test_wallet_file_names(self):
        for n in (".env", "prod.env", "server.pem", "id_rsa", "keystore.json", "my-mnemonic.txt", "coldkey", "x/hotkey",
                  "keyfile", "a.gpg", "a.asc", "a.kdbx", "a.ppk", "seed.txt", "recovery-phrase.txt", "recovery", ".netrc",
                  ".pgpass", "wallet.dat", "a.p8", "secrets.json", "credentials", "token", "password.txt", "coldkey.bak",
                  ".git-credentials", ".npmrc", ".pypirc", ".htpasswd", "rclone.conf", "a.tfstate", "a.age", "kubeconfig"):
            self.assertIn("wallet-file-name", {f.kind for f in scan_bytes(b"hello", n)}, n)
        for ok in (".env.example", "recovery-guide.md", "tokenizer.py", "test_keys.py"):
            self.assertNotIn("wallet-file-name", {f.kind for f in scan_bytes(b"hello", ok)}, ok)

    def test_deeply_nested_json_does_not_crash(self):
        self.assertIsInstance(scan_bytes(b"[" * 100000 + b"]" * 100000, "deep.json"), list)


class Assignments(unittest.TestCase):
    def test_values_names_and_trailing_text(self):
        for text in (f"INFO cfg API_TOKEN={HEX20} user=bob", f"docker run -e DB_PASSWORD={PW} -p 80:80 img",
                     f"run.py --privateKey={HEX32} --port 80", f"curl -d 'password={PW}&x=1' https://h/",
                     f"TOKEN={HEX20}", f"SECRET={HEX20}", "PASSPHRASE" + "=correct horse battery staple",
                     f"DB_PASSWORD={PW}   # prod", "API_KEY=" + "ab12,cd34|ef56(gh78", "PASSWORD=" + "alphabeticonly",
                     f"export SECRET_KEY='{PW}'", f"Environment=DB_PASSWORD={PW}", f"secret_token: {PW}",
                     json.dumps({"secretSeed": "q7" * 10}), "secret" + ' = "' + "abc123" + "secret" + "token" + '"',
                     "TOKEN" + ' = "' + "SECRET" + "TOKEN123" + '"'):
            self.assertIn("env-assignment", kinds(text), text[:30])

    def test_first_token_of_an_unquoted_value_is_checked(self):
        # only the first-token rule catches these: the rest of the line makes the value look like prose
        n1, n2 = "API_" + "TOKEN", "run.py --private" + "Key"
        for text in (f"{n1}={HEX20} and then some words follow here", f"{n2}={HEX32} --port 80 --verbose",
                     f"{n1}={PW} # a comment"):
            self.assertIn("env-assignment", kinds(text), text[:30])

    def test_camel_case_keyword_boundary_for_hex(self):
        # "wallet" is no keyword, so only the lower-to-Upper boundary finds Seed in walletSeed
        self.assertTrue(kinds(f"name,wallet{SEED.capitalize()},{HEX32}") & {"key-hex"})
        self.assertTrue(kinds(f"<wallet{SEED.capitalize()}>{HEX32}</wallet{SEED.capitalize()}>") & {"key-hex"})
        self.assertEqual(kinds(f"name,wallet{SEED},{HEX32}"), set())  # no boundary: "walletseed" is one word

    def test_punctuation_and_a_space_on_password_named_keys(self):
        name = "NOAH_WALLET_" + "PASSWORD"
        value = "Pa(55) w0rd!xyz"
        for text in (f"{name}={value}", f'Environment="{name}={value}"', f"{name}: {value}"):
            self.assertIn("env-assignment", kinds(text), text[:30])
        for ok in (f"{name}=changeme", f"{name}=${{TOKEN}}", f"{name}=make_token(user)", f"{name}=your-token",
                   f"{name}=<your-key>", f"{name} = get_password(user) or fallback", f'{name} = input("Enter it: ")'):
            self.assertEqual(kinds(ok), set(), ok)

    def test_quoted_values_with_punctuation_and_a_space_are_taken_whole(self):
        v = "Pa(55) w0rd!xyz"
        pw, tok = "pass" + "word", "access" + "Token"
        name = "NOAH_WALLET_" + "PASSWORD"
        for text in (f'{pw}: "{v}"', f"{pw} = '{v}'", f'{name}="{v}"', f'export {name}="{v}"',
                     f'Environment={name}="{v}"', json.dumps({tok: v}), f"{tok}: '{v}'"):
            self.assertIn("env-assignment", kinds(text), text[:30])

    def test_spaced_punctuation_on_every_secret_name_not_only_passwords(self):
        v = "Pa(55) w0rd!xyz"
        names = ("DB_" + "SECRET", "API_" + "TOKEN", "api_" + "key", "secret" + "Key", "client" + "Secret")
        for n in names:
            self.assertIn("env-assignment", kinds(f"{n}={v}"), n)
        self.assertIn("env-assignment", kinds("auth" + f"Token: {v}"))
        n1, n2, n3 = "DB_" + "SECRET", "API_" + "TOKEN", "api_" + "key"
        for ok in (f"{n1}=get_password(x)", f"{n2}=$(cat /etc/token)", f"{n2}=changeme", f"{n3}=make_token(user, 3)",
                   f'{n3} = "get_password(x)"', f"{n1}=<your-key>", f"{n2}: your-token", f"{n1}=the next words follow here"):
            self.assertEqual(kinds(ok), set(), ok)

    def test_camel_case_and_embedded_json(self):
        for name in ("secretSeed", "privateKey", "apiKey", "accessToken"):
            doc = json.dumps({name: "0x" + HEX32})
            for text in (doc, doc + "\n" + doc, "2026-10-05 INFO config loaded " + doc + " ok", "```json\n" + doc + "\n```",
                         "cfg = " + repr({name: "0x" + HEX32}), "# " + name + ": 0x" + HEX32):
                self.assertTrue(kinds(text), (name, text[:40]))

    def test_sql_rows_with_a_secret_column(self):
        self.assertTrue(kinds(f"INSERT INTO settings(name,value) VALUES('api_token','{HEX20}');"))

    def test_placeholders_only_hide_short_words(self):
        for ok in ("API_TOKEN=changeme", "API_TOKEN=your-token", "SECRET=example", "TOKEN=${TOKEN}", "NOAH_VALIDATOR_HOTKEY=",
                   f"NOAH_VALIDATOR_HOTKEY={SS58}", "TOKEN_COUNT=12", "SSH_KEY_PATH=/home/x/.ssh/id", "PASSED=true",
                   "PASS=$((PASS+1))", "LoadCredential=name:/etc/keys/file", '"key": "balance"',
                   "hotkey = VALIDATOR_HOTKEY,", "api_key = os.environ.get('X_Y_Z')", "seed = random.randint(0, 99999999)",
                   "token = some_function_name_long(arg)", "Client(Keys=[(Wrapped(0), Wrapped('5AAA'))])"):
            self.assertEqual(kinds(ok), set(), ok)
        n1, n2, n3, n4 = "API_" + "TOKEN", "DB_" + "PASSWORD", "SEC" + "RET", "TO" + "KEN"
        for bad in (f"{n1}=your-{HEX20}", f"{n2}=your-actual-{PW}", f"{n3}=example-{HEX20}", f"{n4}=placeholder-{PW}"):
            self.assertIn("env-assignment", kinds(bad), bad[:30])


class Files(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)

    def test_git_directory_symlinks_and_fifos_are_reported(self):
        (self.d / "ok.txt").write_text("hello")
        (self.d / ".git").mkdir()
        (self.d / ".git" / "config").write_text("x")
        (self.d / "real").mkdir()
        (self.d / "real" / "f.txt").write_text("y")
        os.symlink(self.d / "real", self.d / "dirlink")
        os.symlink(self.d / "nowhere", self.d / "dangling")
        os.symlink(self.d / "ok.txt", self.d / "filelink")
        os.mkfifo(self.d / "pipe")
        kinds_found = sorted(f.kind for f in scan([self.d]).findings)
        self.assertEqual(kinds_found, ["git-directory-refused", "special-file-refused", "symlink-refused",
                                       "symlink-refused", "symlink-refused"])

    def test_zero_files_is_an_error_never_clean(self):
        with self.assertRaises(ScanError):
            scan_paths([self.d])
        (self.d / "sub").mkdir()
        self.assertEqual(scan([self.d]).exit_code, EXIT_ERROR)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main([str(self.d)]), EXIT_ERROR)
        self.assertIn("nothing was scanned", err.getvalue())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read anything")
    def test_unreadable_file_is_a_finding(self):
        f = self.d / "locked.txt"
        f.write_text("hello")
        (self.d / "ok.txt").write_text("fine")
        f.chmod(0)
        self.addCleanup(lambda: f.chmod(stat.S_IRUSR | stat.S_IWUSR))
        self.assertEqual([x.kind for x in scan([self.d]).findings], ["unreadable"])

    def test_invalid_utf8_and_oversize_text_are_findings(self):
        (self.d / "bad.txt").write_bytes(b"\xff\xfe\x00\x01 not text")
        (self.d / "big.txt").write_text("x" * 50)
        (self.d / "ok.txt").write_text("fine")
        old = m._MAX_FILE_BYTES
        m._MAX_FILE_BYTES = 40
        try:
            found = {(Path(f.path).name, f.kind) for f in scan([self.d]).findings}
        finally:
            m._MAX_FILE_BYTES = old
        self.assertEqual(found, {("bad.txt", "binary-refused"), ("big.txt", "file-too-large")})

    def test_a_crash_inside_scanning_fails_closed_without_the_message(self):
        (self.d / "a.txt").write_text("hello")
        real = m.scan_bytes
        m.scan_bytes = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SECRET-IN-MESSAGE"))
        try:
            found = scan([self.d]).findings
        finally:
            m.scan_bytes = real
        self.assertEqual([f.kind for f in found], ["scan-error"])
        self.assertNotIn("SECRET-IN-MESSAGE", str(found))

    def test_tests_directories_are_scanned(self):
        (self.d / "tests").mkdir()
        (self.d / "tests" / "fixtures").mkdir()
        (self.d / "tests" / "fixtures" / "case.json").write_text(PEM)
        (self.d / "ok.txt").write_text("hello")
        self.assertEqual([f.kind for f in scan([self.d]).findings], ["private-key-header"])

    def test_wallet_file_names_are_refused_not_opened(self):
        (self.d / "ok.txt").write_text("hello")
        (self.d / "id_rsa").write_text("nothing secret inside")
        self.assertEqual([f.kind for f in scan([self.d]).findings], ["wallet-file-name"])


class Reports(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)
        (self.d / "ok.json").write_text("{}")

    def test_refused_by_name_is_never_opened_and_never_clean(self):
        text = " ".join(phrase(1, 12))
        for n in ("a.zip", "a.tar", "a.gz", "a.bz2", "a.xz", "a.tar.gpg", "a.db", "a.sqlite", "a.bin", "a.png"):
            (self.d / n).write_text(text)  # plaintext secret inside, but never opened
        report = scan([self.d])
        self.assertEqual(report.scanned, 1)
        self.assertEqual({f.kind for f in report.findings}, {"unexpected-file", "wallet-file-name"})  # NOT bip39-run
        self.assertEqual(len(report.refused_unexpected), 10)  # a.tar.gpg is refused as a wallet-style name, still counted
        self.assertGreaterEqual(len(report.findings), len(report.refused_unexpected))
        self.assertEqual(report.exit_code, EXIT_HITS)
        self.assertEqual(cli.main([str(self.d)]), EXIT_HITS)

    def test_expected_refused_names_are_normal_and_absent_ones_are_noted(self):
        (self.d / "history.db").write_bytes(b"\x00sqlite")
        (self.d / "a.tar.gpg").write_bytes(b"\x00gpg")
        r = scan([self.d], expected_refused=["history.db", "a.tar.gpg", "gone.db"])
        self.assertEqual(r.findings, [])
        self.assertEqual((r.exit_code, len(r.refused_expected), len(r.refused_unexpected)), (EXIT_CLEAN, 2, 0))
        self.assertEqual(r.expected_missing, ["gone.db"])
        self.assertEqual(scan([self.d], expected_refused=["history.db", "a.tar.gpg", "gone.db"], strict=True).exit_code, EXIT_HITS)
        (self.d / "other.db").write_bytes(b"\x00")
        r = scan([self.d], expected_refused=["history.db", "a.tar.gpg"])
        self.assertEqual((r.exit_code, len(r.refused_unexpected)), (EXIT_HITS, 1))

    def test_wallet_style_names_count_as_refused_and_never_exceed_findings(self):
        (self.d / "sub").mkdir()
        for n in ("a.gpg", "b.age", "c.tar.gpg", ".env", "sub/a.gpg"):
            (self.d / n).write_bytes(b"\x00")
        for n in ("x" * 30 + ".bin", "y" * 30 + ".bin"):  # two names that mask to the same printed path
            (self.d / n).write_bytes(b"\x00")
        r = scan([self.d])
        self.assertEqual(len(r.refused_unexpected), 7)
        self.assertEqual(len(r.findings), 7)  # nothing de-duplicated away
        self.assertEqual({f.kind for f in r.findings}, {"wallet-file-name", "unexpected-file"})

    def test_expected_refused_never_hides_a_scanned_text_file(self):
        (self.d / "history.json").write_text(PEM)
        r = scan([self.d], expected_refused=["history.json", "ok.json"])
        self.assertEqual([f.kind for f in r.findings], ["private-key-header"])
        self.assertEqual(len(r.refused_expected), 0)
        self.assertEqual(r.scanned, 2)

    def test_exclusion_matches_the_exact_path_only(self):
        url = "https://hc-ping.com/" + "abcdefghijklmnop"
        (self.d / "sub").mkdir()
        (self.d / "sub" / "ping-url.txt").write_text(url)
        (self.d / "ping-url.txt.bak.txt").write_text(url)
        for excluded in (["ping-url.txt"], ["ping"], ["sub"], ["sub/ping-url"], ["ping-url.txt.bak"]):
            r = scan([self.d], excluded=excluded)
            self.assertEqual(len(r.excluded), 0, excluded)  # a basename or a prefix never excludes
            self.assertEqual(len(r.findings), 2, excluded)
        r = scan([self.d], excluded=["sub/ping-url.txt"])
        self.assertEqual((len(r.excluded), len(r.findings)), (1, 1))
        r = scan([self.d], excluded=[str(self.d / "sub" / "ping-url.txt")])
        self.assertEqual(len(r.excluded), 1)

    def test_a_run_that_only_excluded_files_scanned_nothing(self):
        d2 = self.d / "only"
        d2.mkdir()
        (d2 / "ping-url.txt").write_text("https://hc-ping.com/" + "abcdefghijklmnop")
        r = scan([d2], excluded=["ping-url.txt"])
        self.assertEqual((r.scanned, len(r.excluded), r.exit_code), (0, 1, EXIT_ERROR))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--excluded", "ping-url.txt", str(d2)]), EXIT_ERROR)
        self.assertIn("nothing was scanned", err.getvalue())

    def test_unit_file_types_are_scanned(self):
        for n in (".service", ".timer", ".mount", ".socket", ".path", ".conf"):
            (self.d / ("u" + n)).write_text(PEM)
        r = scan([self.d])
        self.assertEqual((r.scanned, len(r.refused_unexpected)), (7, 0))
        self.assertEqual(len(r.findings), 6)

    def test_exclusion_is_an_explicit_list_and_counted(self):
        url = "https://hc-ping.com/" + "abcdefghijklmnop"
        (self.d / "ping-url.txt").write_text(url)
        (self.d / "named-like-ping.txt").write_text(url)
        r = scan([self.d], excluded=["ping-url.txt"])
        self.assertEqual(len(r.excluded), 1)
        self.assertEqual([Path(f.path).name for f in r.findings], ["named-like-ping.txt"])  # not honoured by pattern
        self.assertEqual(scan([self.d]).exit_code, EXIT_HITS)
        r2 = scan([self.d], excluded=["ping-url.txt", "named-like-ping.txt"])
        self.assertEqual((r2.exit_code, len(r2.excluded)), (EXIT_CLEAN, 2))

    def test_explicit_file_list_and_scope(self):
        (self.d / "a.py").write_text("x = 1")
        self.assertEqual(scan([self.d / "ok.json"]).scanned, 1)
        self.assertEqual([f.kind for f in scan([self.d / "a.py"]).findings], ["unexpected-file"])  # base scope
        self.assertEqual(scan([self.d / "a.py"], scope="release").exit_code, EXIT_CLEAN)
        with self.assertRaises(ScanError):
            scan([self.d], scope="nonsense")

    def test_masked_names_in_the_report(self):
        (self.d / ("ghp_" + "A1" * 20 + ".bin")).write_bytes(b"\x00")
        r = scan([self.d])
        self.assertNotIn("ghp_", json.dumps(r.as_dict()))
        self.assertIn("<masked>", r.refused_unexpected[0])


class Masking(unittest.TestCase):
    def test_printed_names_are_masked(self):
        token = "ghp_" + "A1" * 20
        for name in (token + ".env", "/x/" + PW + "/f.txt", "mnemonic-ZQMARKERZQ.txt", "a" * 30 + ".txt",
                     token + ".txt", "my-" + SECRET + "-Ab3dE5.json"):
            shown = mask_path(name)
            self.assertIn("<masked>", shown, name)
            for leaked in (token, PW, "ZQMARKERZQ", "a" * 30, "Ab3dE5"):
                self.assertNotIn(leaked, shown)
        for ok in ("README.md", "SHA256SUMS", "test_keys.py", "day-12.json", "history.db", "/var/tmp/records/net/2026-10-02/score.json"):
            self.assertEqual(mask_path(ok), ok)

    def test_long_names_and_phrase_names_are_masked(self):
        phrase_name = "_".join(phrase(21, 12)) + ".txt"
        self.assertEqual(mask_path("/a/" + phrase_name), "/a/<masked>")
        short = "_".join(phrase(21, 6)) + ".txt"
        self.assertEqual(mask_path("/a/" + short), "/a/" + short if len(short) <= 24 else "/a/<masked>")
        self.assertEqual(mask_path("/a/" + "abcdefghij_" * 3), "/a/<masked>")  # over 24 characters
        self.assertEqual(mask_path("/a/" + "abcdefghij_" * 2 + "ab"), "/a/" + "abcdefghij_" * 2 + "ab")  # 24 stays

    def test_no_code_path_prints_matched_text_or_content_derived_names(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        words = phrase(14, 12)
        token = "ghp_" + "A1" * 20
        (d / "n.txt").write_text("ok\n" + " ".join(words) + "\n" + f"{SEED} = 0x{HEX32}\n" + PEM + token + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([str(d)])
        text = out.getvalue()
        self.assertEqual(code, EXIT_HITS)
        self.assertIn("n.txt:2: bip39-run", text)
        for leaked in (words[0] + " " + words[1], HEX32, "AAAA", token):
            self.assertNotIn(leaked, text)
        self.assertEqual(set(vars(scan_bytes(PEM.encode(), "a")[0])), {"path", "line", "kind"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--json", str(d)])
        for leaked in (words[0], HEX32, token):
            self.assertNotIn(leaked, out.getvalue())


class Perf(unittest.TestCase):
    def timed(self, data):
        start = time.time()
        scan_bytes(data, "x.txt")
        return time.time() - start

    def test_one_line_inputs_scan_in_bounded_time(self):
        self.assertLess(self.timed(("key=x " * 20000).encode()[:100000]), 1.0)
        self.assertLess(self.timed(("key=x " * 200000).encode()[:1000000]), 10.0)
        arr = json.dumps([{"password": "ab%d" % i, "n": i} for i in range(50000)]).encode()
        self.assertLess(self.timed(arr), 10.0)
        self.assertLess(self.timed(("alpha beta " * 100000).encode()[:1000000]), 10.0)


class Cli(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)

    def test_exit_codes(self):
        (self.d / "ok.txt").write_text("hello")
        self.assertEqual(cli.main([str(self.d)]), EXIT_CLEAN)
        (self.d / "bad.txt").write_text(PEM)
        self.assertEqual(cli.main([str(self.d)]), EXIT_HITS)
        self.assertEqual(cli.main(["/nonexistent-path-for-scan"]), EXIT_ERROR)
        self.assertEqual(cli.main([]), EXIT_ERROR)
        self.assertEqual(cli.main(["--bogus", str(self.d)]), EXIT_ERROR)
        self.assertIsInstance(Finding("a", 1, "k"), Finding)

    def test_cli_summary_and_json(self):
        (self.d / "ok.txt").write_text("hello")
        (self.d / "history.db").write_bytes(b"\x00")
        cfg = self.d.parent / (self.d.name + ".cfg.json")
        cfg.write_text(json.dumps({"expected_refused": ["history.db", "missing.db"], "strict": False}))
        self.addCleanup(cfg.unlink)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--json", "--config", str(cfg), str(self.d)])
        data = json.loads(out.getvalue())
        self.assertEqual(code, EXIT_CLEAN)
        self.assertEqual((data["scanned"], data["refused"]["expected_count"], data["refused"]["unexpected_count"]), (1, 1, 0))
        self.assertEqual((data["excluded"]["count"], data["expected_missing"], data["exit_code"]), (0, ["missing.db"], 0))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--strict", "--expected-refused", "history.db", "--expected-refused", "missing.db", str(self.d)])
        self.assertEqual(code, EXIT_HITS)
        self.assertIn("scanned=1 refused_expected=1", out.getvalue())
        self.assertIn("note: expected-refused name not present", out.getvalue())


class Negative(unittest.TestCase):
    def test_public_values_pass(self):
        text = json.dumps({"ark_address": SS58, "genesis_hash": BLOCK_HASH, "sha": hashlib.sha256(b"q").hexdigest(),
                           "validator_hotkey": SS58, "public key": "0x" + HEX32})
        self.assertEqual(scan_bytes(text.encode(), "t.json"), [])
        self.assertEqual(scan_bytes(f"{HEX32}  fixture.json\n".encode(), "SHA256SUMS"), [])

    def test_the_real_wallet_addresses_document_and_repo_data_pass(self):
        doc = ROOT / "docs" / "reference" / "WALLET_ADDRESSES.md"
        if doc.exists():
            self.assertEqual(scan_paths([doc]), [])
        self.assertEqual(scan_paths([SERVICE_ROOT / "fixtures" / "shared"], scope="release"), [])
        self.assertEqual(scan_paths([ROOT / "docs" / "parameters" / "profiles"], scope="release"), [])


if __name__ == "__main__":
    unittest.main()


class CalculationSourceInventory(unittest.TestCase):
    def context(self):
        files = {
            path: hashlib.sha256(path.encode()).hexdigest()
            for path in (
                "noah/ark/keys.py",
                "noah/chain/duplicate_coldkeys.py",
                "noah/chain/ss58.py",
                "noah/deposits/scenario_keys.py",
                "noah/scoring/streak.py",
                "noah/secret_scan/__init__.py",
                "noah/secret_scan/__main__.py",
                "noah/secret_scan/_bip39.py",
                "noah/secretscrub.py",
            )
        }
        return {
            "day": 20,
            "height": 144440,
            "netuid": 2,
            "genesis_hash": BLOCK_HASH,
            "profile_sha256": HEX32,
            "source_agreement": "required_by_ingestion",
            "candidate": {
                "declared_commit": "a" * 40,
                "files": files,
                "source_sha256": self.digest(files),
            },
        }

    def digest(self, files):
        return hashlib.sha256(self.render(files).encode()).hexdigest()

    def render(self, obj):
        return json.dumps(obj, sort_keys=True, indent=2) + "\n"

    def test_canonical_inventory_paths_are_labels_not_credentials(self):
        raw = self.render(self.context())
        self.assertEqual(kinds(raw), set())
        normalized = m._source_inventory_labels(raw)
        self.assertEqual(len(normalized), len(raw))
        self.assertEqual(normalized.count("\n"), raw.count("\n"))
        for digest in self.context()["candidate"]["files"].values():
            self.assertIn(digest, normalized)  # Never hide actual values.

    def test_other_fields_and_duplicate_keys_still_detect_secrets(self):
        for location in ("outside", "candidate", "files"):
            obj = self.context()
            target = obj if location == "outside" else obj["candidate"]
            if location == "files":
                target = target["files"]
            target["private_" + "key"] = HEX32
            raw = self.render(obj)
            self.assertTrue(kinds(raw), location)
        raw = self.render(self.context())
        duplicate = raw.replace('  "day": 20,', '  "day": 20,\n  "day": 21,')
        self.assertTrue(kinds(duplicate))

    def test_malformed_inventory_is_not_normalized(self):
        for mutation in ("digest", "path", "value", "shape", "noncanonical"):
            obj = self.context()
            candidate = obj["candidate"]
            if mutation == "digest":
                candidate["source_sha256"] = "0" * 64
            elif mutation == "path":
                candidate["files"]["noah/../private_key.py"] = HEX32
                candidate["source_sha256"] = self.digest(candidate["files"])
            elif mutation == "value":
                candidate["files"]["noah/ark/keys.py"] = " ".join(phrase(77))
                candidate["source_sha256"] = self.digest(candidate["files"])
            elif mutation == "shape":
                del obj["source_agreement"]
            raw = self.render(obj) if mutation != "noncanonical" else json.dumps(obj)
            self.assertTrue(kinds(raw), mutation)
            self.assertEqual(m._source_inventory_labels(raw), raw)

    def test_same_path_digest_outside_inventory_is_not_exempt(self):
        self.assertTrue(kinds(json.dumps({"noah/ark/keys.py": HEX32})))


    def test_credential_in_inventory_filename_retains_original_scan_and_line(self):
        obj = self.context()
        token = "ghp_" + "A1" * 20
        path = "noah/" + token + ".py"
        obj["candidate"]["files"][path] = HEX32
        obj["candidate"]["source_sha256"] = self.digest(obj["candidate"]["files"])
        raw = self.render(obj)
        findings = scan_bytes(raw.encode(), "context.json")
        expected_line = next(i for i, line in enumerate(raw.splitlines(), 1) if path in line)
        self.assertTrue(any(f.kind == "token-shape" and f.line == expected_line for f in findings))


    def test_hex_credential_embedded_in_inventory_filename_is_not_hidden(self):
        obj = self.context()
        path = "noah/private_key_" + HEX32 + ".py"
        obj["candidate"]["files"][path] = HEX32
        obj["candidate"]["source_sha256"] = self.digest(obj["candidate"]["files"])
        raw = self.render(obj)
        self.assertIn("key-hex", kinds(raw))
