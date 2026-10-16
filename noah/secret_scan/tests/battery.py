"""Runs the agreed acceptance battery (acceptance_cases.json) and returns a result table.

``python -m noah.secret_scan.tests.battery`` prints it. Secret-looking values are generated
here, at run time, from fixed seeds; none is stored in the repository."""

import base64
import json
import random
import string
import sys
from pathlib import Path

from noah.secret_scan import scan_bytes
from noah.secret_scan._bip39 import ORDERED

CASES = Path(__file__).resolve().parents[1] / "acceptance_cases.json"
SEEDS = (1, 2, 3)
SS58 = "5D8HcAqogib6u8cX7WArLcTmmDp2Wv7arXzk6jvJUrmFsGxv"


def _alnum(rng, n, alphabet=string.ascii_letters + string.digits):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _mixed(rng, n):
    s = [rng.choice(string.ascii_uppercase), rng.choice(string.ascii_lowercase), rng.choice(string.digits)]
    s += [rng.choice(string.ascii_letters + string.digits) for _ in range(n - 3)]
    rng.shuffle(s)
    return "".join(s)


# Secret-shaped fragments are assembled here, never stored whole in acceptance_cases.json.
FRAGMENTS = {
    "N1": "API_" + "TOKEN", "N2": "DB_" + "PASSWORD", "N3": "PASS" + "WORD", "N4": "SEC" + "RET", "N5": "TO" + "KEN",
    "N6": "PASS" + "PHRASE", "N7": "MNEMONIC" + "_NOTE", "N8": "secret" + "Seed", "N9": "private" + "Key",
    "N10": "secret" + "_seed", "N11": "secret" + "_token", "N12": "WALLET_" + "SECRET", "N13": "SECRET_" + "KEY",
    "N14": "NOAH_VALIDATOR_" + "HOTKEY", "N15": "TOKEN" + "_COUNT", "N16": "INSERT" + "_TOKEN", "N17": "NOAH_WALLET_" + "PASSWORD", "N18": "pass" + "word", "N19": "access" + "Token", "N20": "DB_" + "SECRET",
    "N21": "api_" + "key", "N22": "secret" + "Key", "N23": "client" + "Secret", "N24": "auth" + "Token",
    "PB": "-----BEGIN " + "PRIVATE KEY-----", "PE": "-----END " + "PRIVATE KEY-----",
    "OB": "-----BEGIN OPENSSH " + "PRIVATE KEY-----", "PG": "post" + "gres",
}


def values(seed):
    rng = random.Random(seed)
    hexd = "0123456789abcdef"
    b = bytes(rng.randrange(256) for _ in range(32))
    b64 = base64.b64encode(b).decode()
    while not (any(c.isupper() for c in b64) and any(c.islower() for c in b64) and any(c.isdigit() for c in b64)):
        b = bytes(rng.randrange(256) for _ in range(32))
        b64 = base64.b64encode(b).decode()
    return {
        **FRAGMENTS,
        "HEX64": _alnum(rng, 64, hexd), "HEX40": _alnum(rng, 40, hexd), "HEX128": _alnum(rng, 128, hexd),
        "PW": _mixed(rng, 14), "GHP": "ghp_" + _alnum(rng, 36), "ACCESS_ID_SUFFIX": _alnum(rng, 16, string.ascii_uppercase + string.digits),
        "JWT": "eyJ" + _alnum(rng, 12) + ".eyJ" + _alnum(rng, 14) + "." + _alnum(rng, 20),
        "UUID": "-".join(_alnum(rng, n, hexd) for n in (8, 4, 4, 4, 12)),
        "BEARER": _mixed(rng, 30), "SS58": SS58, "BLOCKHASH": "0x" + _alnum(rng, 64, hexd), "N": str(rng.randrange(10**6)),
        "B64_32": b64, "PHRASE": " ".join(rng.sample(ORDERED, 12)),
    }


def words_for(case, seed):
    rng = random.Random(seed * 7919)
    if "literal_runs" in case:
        return [w for w, count in case["literal_runs"] for _ in range(count)]
    n = case.get("words", 12)
    if case.get("repeat_words"):
        d = rng.sample(ORDERED, n - case["repeat_words"])
        words = d + d[: case["repeat_words"]]
    else:
        words = rng.sample(ORDERED, n)
    typos = case.get("typo_at")
    if typos is not None:
        for i in typos if isinstance(typos, list) else [typos]:
            words[i] = words[i][:-1] + "q"
    return words


def render(case, seed):
    v = values(seed)
    if "template" in case:
        return case["template"].format(**v)
    words = words_for(case, seed)
    if case.get("odd_separator"):
        parts = [f"{i + 1}{')' if i == 11 else '.'} {w}" for i, w in enumerate(words)]
        return " ".join(parts)
    if "per_word" in case:
        body = case["join"].join(case["per_word"].format(w=w, n=i + 1, i=i) for i, w in enumerate(words))
        return case.get("prefix", "") + body + case.get("suffix", "")
    phrase = " ".join(words)
    if "wrap" in case:
        return case["wrap"].replace("{PHRASE}", phrase)
    if "b64_of_phrase" in case:
        blob = phrase
        for _ in range(case["b64_of_phrase"]):
            blob = base64.b64encode(blob.encode()).decode()
        cols = case.get("wrap_cols")
        if cols:
            blob = "\n".join(blob[i:i + cols] for i in range(0, len(blob), cols))
        return "payload:\n" + blob + "\n" if cols else "payload: " + blob + "\n"
    raise ValueError(case["id"])


def run(cases=None):
    """Return rows ``(id, scope, expect, result)``; result is caught/missed/clean/FALSE-POSITIVE/info/see-test."""
    rows = []
    for c in cases or json.load(open(CASES))["cases"]:
        expect = c["expect"]
        renderable = "template" in c or "per_word" in c or "literal_runs" in c or "wrap" in c or "b64_of_phrase" in c or c.get("odd_separator")
        if not renderable:
            rows.append((c["id"], c["scope"], expect, "see-test"))
            continue
        outcomes = []
        for seed in SEEDS:
            text = render(c, seed)
            found = scan_bytes(text.encode(), "case.txt")
            kinds = {f.kind for f in found}
            if c["scope"] == "in" and expect == "hit":
                want = c.get("kind", "")
                ok = bool(found) if not want.endswith("bip39-run") else any(k.endswith("bip39-run") for k in kinds)
                outcomes.append(ok)
            elif expect == "clean":
                outcomes.append(not found)
            else:  # out of scope: informational
                outcomes.append(bool(found))
        if c["scope"] == "in" and expect == "hit":
            res = "caught" if all(outcomes) else "MISSED"
        elif expect == "clean":
            res = "clean" if all(outcomes) else "FALSE-POSITIVE"
        else:
            res = "info:" + ("flagged" if any(outcomes) else "not flagged") + f"/{expect}"
        rows.append((c["id"], c["scope"], expect, res))
    return rows


def main() -> int:
    rows = run()
    for r in rows:
        print("%-52s %-12s %-9s %s" % r)
    bad = [r for r in rows if r[3] in ("MISSED", "FALSE-POSITIVE")]
    print(f"\n{len(rows)} cases; in-scope caught/clean "
          f"{sum(r[3] in ('caught', 'clean') for r in rows)}; problems {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
