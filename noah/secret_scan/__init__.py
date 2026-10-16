"""Scan text artifacts for recognizable secret material without printing matches.

Reports show file, line and finding type. Unknown file types, unreadable files,
symlinks, oversized files and binary content are refused instead of counted as
clean. Explicit exclusions are counted. Patterns cover common credentials, key
encodings and recovery phrases, including bounded nested Base64 text.

This is pattern matching, not proof of absence. It does not inspect encrypted or
compressed data, arbitrary obfuscation, secrets split across files or unknown
formats. Review the release contents as well as the scan report."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import unicodedata
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ._bip39 import WORDS as _BIP39

EXIT_CLEAN, EXIT_ERROR, EXIT_HITS = 0, 2, 3
PHRASE_LENGTHS = (12, 15, 18, 21, 24)
_MAX_DEPTH = 3
_MAX_FILE_BYTES = 32 * 1024 * 1024

BASE_EXTS = frozenset({".json", ".txt", ".conf", ".service", ".timer", ".mount", ".socket", ".path", ".sql", ".md"})
BASE_NAMES = frozenset({"SHA256SUMS", "software-commit"})
RELEASE_EXTS = BASE_EXTS | frozenset({
    ".py", ".ts", ".tsx", ".js", ".sh", ".toml", ".yaml", ".yml", ".lock", ".example", ".cfg", ".ini", ".css",
    ".html", ".sha256",
})
RELEASE_NAMES = BASE_NAMES | frozenset({"LICENSE", ".gitignore", "Makefile", ".gitkeep"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


class ScanError(Exception):
    """The scan could not be done at all (missing path, nothing scanned). Exit code 2."""


# ------------------------------------------------------------ name masking

_SECRET_WORD_IN_NAME = re.compile(r"(?i)seed|mnemonic|secret|passw|token|private|credential|wallet|recovery|key")


def _component_is_secretish(c: str) -> bool:
    if len(c) > 24:
        return True
    run = 0
    for w in re.findall(r"[a-z]+", c.lower()):  # a phrase used as a file name
        run = run + 1 if w in _BIP39 else 0
        if run >= 12:
            return True
    if re.search(r"[A-Za-z0-9+/=]{24,}", c):
        return True
    if any(p.search(c) for p in _TOKEN_SHAPES):
        return True
    has_u, has_l, has_d = (re.search(p, c) is not None for p in ("[A-Z]", "[a-z]", "[0-9]"))
    if len(c) >= 8 and has_u and has_l and has_d:
        return True
    if _SECRET_WORD_IN_NAME.search(c) and ((has_u and has_l) or has_d or len(c) > 24):
        return True
    return False


def mask_path(path) -> str:
    """The path with every secret-looking component replaced by ``<masked>``."""
    parts = str(path).replace("\\", "/").split("/")
    return "/".join("<masked>" if p and _component_is_secretish(p) else p for p in parts)


# ---------------------------------------------------------------- patterns

_PEM = re.compile(
    r"-----BEGIN\s+(?:[A-Z0-9]+\s+)*PRIVATE\s+KEY(?:\s+BLOCK)?-----|-----BEGIN\s+(?:ENCRYPTED|OPENSSH|PGP\s+PRIVATE)[A-Z0-9 ]*-----"
    r"|PuTTY-User-Key-File-\d|AGE-SECRET-KEY-1[A-Z0-9]{20,}|\bxprv[1-9A-HJ-NP-Za-km-z]{100,}",
    re.I,
)
_STRONG = (
    r"seed|mnemonic|secret|private(?![\s_-]*(?:testnet|network|chain|repo|repository|server|subnet|environment|rpc|node|reader|fork))"
    r"|passphrase|privkey|secretkey|recovery[ _-]?phrase"
)
# A keyword may start at a camelCase boundary (``secretSeed``): (?<![A-Za-z]) or a lower-to-Upper step.
_LB = r"(?:(?<![A-Za-z])|(?<=[a-z0-9])(?=[A-Z]))"
_KEYWORD = re.compile(
    rf"{_LB}(?i:(?:{_STRONG})|(?:api|signing|auth|access|encryption|master|raw)[_-]?key)(?![a-z])"
    r"|(?<![A-Za-z0-9_])(?i:key)(?![a-z])"
)
_STRONG_KEYWORD = re.compile(rf"{_LB}(?i:(?:{_STRONG}))(?![a-z])")
_PUBLIC_KEY_WORD = re.compile(r"(?:public|pub|storage)[\s_-]*key\s*$", re.I)
_BENIGN_LABEL = re.compile(
    r"(?:hash|sha\d*|digest|checksum|genesis|block|extrinsic|txid|fingerprint|commit|sums?|pinned\s+at)\W{0,6}$", re.I
)
_HEX = re.compile(r"(?<![0-9A-Za-z])(?:0x)?(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{128})(?![0-9A-Za-z])")
_B64 = re.compile(
    r"(?<![A-Za-z0-9+/=_-])(?:[A-Za-z0-9+/_-]{43}=?|[A-Za-z0-9+/_-]{86}={0,2}|[A-Za-z0-9+/_-]{87}=?|[A-Za-z0-9+/_-]{88})"
    r"(?![A-Za-z0-9+/=_-])"
)
_B58_ALPHA = "1-9A-HJ-NP-Za-km-z"
_B58 = re.compile(rf"(?<![{_B58_ALPHA}0-9_-])(?:[{_B58_ALPHA}]{{43,44}}|[{_B58_ALPHA}]{{87,88}})(?![{_B58_ALPHA}0-9_-])")
_WIF = re.compile(rf"(?<![A-Za-z0-9])[5KL][{_B58_ALPHA}]{{50,51}}(?![A-Za-z0-9])")
_SS58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{46,48}$")
_TOKEN_SHAPES = tuple(
    re.compile(p)
    for p in (
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{22,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b",
        r"\bK00[A-Za-z0-9/+]{28}(?![A-Za-z0-9/+])",
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*",  # JWT
        r"\bnpm_[A-Za-z0-9]{30,}",
        r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}",
        r"\bwhsec_[A-Za-z0-9]{16,}",
        r"\bAIza[0-9A-Za-z_-]{35}",
        r"\bhf_[A-Za-z0-9]{30,}",
        r"\bhc[rw]_[A-Za-z0-9]{16,}",  # Healthchecks read/write API keys
        r"(?<![0-9])\d{8,10}:AA[A-Za-z0-9_-]{33}\b",  # Telegram bot token
        r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
        r"(?i)discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}",
        r"(?i)hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+",
        r"(?i)hc-ping\.com(?::\d+)?/(?![A-Za-z0-9_-]*(?:REPLACE|EXAMPLE|YOUR|PLACEHOLDER|XXX))[A-Za-z0-9_-]{8,}",
        r"(?i)healthchecks\.io(?::\d+)?/ping/(?![A-Za-z0-9_-]*(?:REPLACE|EXAMPLE|YOUR|PLACEHOLDER|XXX))[A-Za-z0-9_-]{8,}",
        r"(?i)x-api-key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_-]{20,}",
        r"\b[a-z][a-z0-9+.-]{1,15}://[^/\s:@\"']{0,64}:[^/\s@\"']{3,}@[^\s/\"']+",  # user:pw@ and :pw@ URLs
        r"\b[a-z][a-z0-9+.-]{1,15}://[A-Za-z0-9_-]{20,}@",  # token as the user part
        r"(?i)\b(?:basic|bearer)\s+(?=[A-Za-z0-9+/=._~-]*\d)[A-Za-z0-9+/=._~-]{16,}",
    )
)
_BACKBLAZE_CTX = re.compile(r"(?i)backblaze|\bb2\b|application[_ ]?key")
_B2_KEY = re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{31}(?![A-Za-z0-9/+])")
_BAD_NAME = re.compile(
    r"(?i)(^\.env(\..*)?$|\.env$|\.pem$|\.key$|\.p12$|\.p8$|\.pfx$|\.ppk$|\.gpg$|\.asc$|\.kdbx?$|\.jks$|\.age$|\.tfstate$"
    r"|^id_(rsa|dsa|ed25519|ecdsa)|keystore|keyfile|mnemonic|wallet\.(json|dat)$|^(cold|hot)key(\.[a-z]+)?$"
    r"|^seed(\.|$)|^recovery([-_. ](phrase|seed|words|key|code))?(\.[a-z0-9]+)?$"
    r"|^(secret|secrets|credential|credentials|password|passwords|token|tokens)(\.[a-z0-9]+)?$"
    r"|^\.netrc$|^_netrc$|^\.pgpass$|^\.git-credentials$|^\.npmrc$|^\.pypirc$|^\.htpasswd$|^rclone\.conf$|^kubeconfig$)"
)
_ENV_NAME_OK = re.compile(r"(?i)\.(example|sample|template)$")
_PLACEHOLDER = re.compile(
    r"(?i)^(?:|\"\"|''|none|null|nil|true|false|yes|no|changeme|change[-_]?me|pending|todo|tbd|redacted|x+|\*+|\.\.\.|"
    r"<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|secret|password|token|key|"
    r"os\.environ.*|getenv.*|env\[.*|"
    r"(?:your|example|placeholder|sample|dummy|fake|test)(?:[-_ ][a-z]{1,10}){0,2})$"
)

# ------------------------------------------------------------ name logic

_SECRET_PARTS = {
    "key", "keys", "secret", "secrets", "seed", "mnemonic", "pass", "passwd", "password", "passphrase", "pwd",
    "token", "private", "privkey", "credential", "credentials", "apikey", "ciphertext", "wif",
}
_SECRET_PREFIX = ("secret", "mnemonic", "password", "passphrase", "privat", "seedphrase", "apikey")
_BENIGN_PARTS = {
    "count", "index", "idx", "id", "path", "file", "name", "len", "length", "size", "type", "bytes", "limit",
    "ttl", "expiry", "expires", "url", "dir", "ref", "hash", "kind",
}
_PUBLIC_JOINED = re.compile(r"hotkey|coldkey|address|ss58")
_PHRASE_NAME = re.compile(r"passphrase|password|passwd|pwd|mnemonic|seedphrase|recovery")


def _split_name(name: str) -> list:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [p for p in re.split(r"[_.\-]+", spaced.lower()) if p]


def _name_is_secretish(name: str) -> bool:
    parts = _split_name(name)
    joined = "".join(parts)
    if "public" in parts or "pub" in parts or re.search(r"hotkey|coldkey|pubkey|publickey", joined):
        parts = [p for p in parts if p not in ("hot", "cold", "public", "pub")]
        joined = re.sub(r"hotkey|coldkey|pubkey|publickey", "", joined)
        if not re.search(r"secret|seed|mnemonic|pass|token|private", joined):
            return False
    if parts and parts[-1] in _BENIGN_PARTS and parts[-1] != "key":
        return False
    return any(p in _SECRET_PARTS or p.startswith(_SECRET_PREFIX) for p in parts)


def _name_is_weak(name: str) -> bool:
    """Secret-ish only because of a bare KEY part."""
    return not any(p in _SECRET_PARTS - {"key", "keys"} or p.startswith(_SECRET_PREFIX) for p in _split_name(name))


def _name_is_public_identifier(name: str) -> bool:
    return bool(_PUBLIC_JOINED.search("".join(_split_name(name))))


_HEX_VALUE = re.compile(r"(?:0x)?[0-9a-fA-F]+")


def _hex_len(v: str):
    m = _HEX_VALUE.fullmatch(v)
    return len(v) - (2 if v.lower().startswith("0x") else 0) if m else None


def _clean_value(v: str) -> str:
    v = v.strip()
    v = re.sub(r"\s+#.*$", "", v).strip()
    return v.strip(" ,;.)}]\"'`")


def _value_is_secret(
    raw: str, name: str, quoted: bool, line_start: bool = True, inline_quoted: bool = False, whole_rest: bool = False
) -> bool:
    """``whole_rest``: ``raw`` is a whole value (a quoted value, or the whole rest of an env-style line),
    taken because the first token alone looked like code or was too short."""
    v = _clean_value(raw)
    ends_with_bracket = re.sub(r"[\s,;.\"'`]+$", "", raw)[-1:] in (")", "]")  # _clean_value strips closing brackets
    if len(v) < 8 or _PLACEHOLDER.match(v) or re.fullmatch(r"[0-9_.,]+", v) or v.lower() == name.lower():
        return False
    if _SS58.match(v):
        return False
    hl = _hex_len(v)
    if hl is not None and (v.lower().startswith("0x") or hl >= 32):
        return hl in (32, 40, 48, 64, 128)
    if v[0] in "/~." or (v.startswith(("http://", "https://")) and not re.search(r"://[^/\s:@]*:[^/\s@]+@", v)):
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*\(.*\)[,)]*", v):  # a function call
        return False
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", v):  # attribute chain
        return False
    if re.fullmatch(r"_*[A-Za-z]+(?:_+[A-Za-z]+)*_*", v) and "_" in v:  # snake_case identifier, no digits
        return False
    if v.startswith("$") or re.match(r"[A-Za-z0-9_.-]+:/", v):  # shell expansion, or a name:/path pair
        return False
    if re.match(r"[A-Za-z_][A-Za-z0-9_.]*=", v):  # another NAME=value written in prose or code
        return False
    if re.search(r"\)\s*\.|\(\)|\([\"']", v):  # chained call or call with a literal: code
        return False
    if re.fullmatch(r"(?:[A-Z][a-z]+){2,}", v):  # CamelCase identifier
        return False
    if (
        re.match(r"[A-Za-z_][\w.]*[\[(]", v)
        and not re.search(r"[)\]][A-Za-z0-9!@#$%^&*_+=~-]", v)
        and not ((whole_rest or inline_quoted) and not ends_with_bracket)  # ``Pa(55) w0rd!xyz``: not a call
    ) or v[0] in "{(<[" or re.match(r"[a-zA-Z]{0,2}[\"']", v):
        return False  # a call, subscript, literal or f-string: code, not a value (a character right after the closing bracket, as in ``Pa(55)w0rd!x``, marks a password)
    if re.fullmatch(r"[A-Z_]+", v):  # constant
        return False
    if re.search(r"\s", v):  # a passphrase with spaces
        words = v.split()
        if len(words) < 2 or len(v) < 12 or (not (inline_quoted or whole_rest) and re.search(r"[(){}\[\]<>]", v)):
            return False
        if any(w in ("if", "else", "and", "or", "not", "in", "is", "for", "return", "lambda", "yield", "as", "with") for w in words) or all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", w) and "_" in w for w in words
        ):
            return False  # code, not a passphrase
        if not quoted and not line_start:
            return False  # prose or code after the name, not an env-style line
        if inline_quoted or _PHRASE_NAME.search("".join(_split_name(name))):
            return True
        # any other secret-named key: a whole spaced value counts when it carries a digit or punctuation
        if re.match(r"[\w.\[\], ]+\s=\s", v) or re.search(r"\w\[\d+\]", v):
            return False  # a type annotation with a default (``x: object = None``) or a subscript: code
        return whole_rest and re.search(r"[0-9!@#$%^&*+~]", v) is not None
    return True


# --------------------------------------------------------------- helpers


class _Lines:
    """Line numbers from offsets in O(log n), so many findings never go quadratic."""

    def __init__(self, text: str):
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def of(self, idx: int) -> int:
        return bisect_right(self.starts, idx)

    def start(self, idx: int) -> int:
        return self.starts[bisect_right(self.starts, idx) - 1]


def _near_keyword(flat: str, start: int, end: int) -> bool:
    before = flat[max(0, start - 80):start]
    after = flat[end:end + 80]
    for m in _KEYWORD.finditer(before):
        if m.group(0).lower() == "key" and _PUBLIC_KEY_WORD.search(before[: m.end()]):
            continue
        return True
    return bool(_KEYWORD.search(after))


def _strong_near(flat: str, start: int, end: int, window: int = 80) -> bool:
    return bool(
        _STRONG_KEYWORD.search(flat[max(0, start - window):start]) or _STRONG_KEYWORD.search(flat[end:end + window])
    )


# --------------------------------------------------------- BIP39 phrases

_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s", "ј": "j", "ԁ": "d",
    "ɡ": "g", "һ": "h", "ո": "n", "ս": "u", "ν": "v", "ο": "o", "α": "a", "ρ": "p", "τ": "t", "ι": "i", "κ": "k",
})
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\\x1b\[[0-9;]*[A-Za-z]|\\u001b\[[0-9;]*[A-Za-z]|\\033\[[0-9;]*[A-Za-z]", re.I)
_LABEL = re.compile(r"(?i)\b(?:word|w|idx|index|no|num|number|n)_?\d+\b\s*[:=]?")
_STR_PREFIX = re.compile(r"(?<![A-Za-z0-9_])(?:[bBuUrR]|[bB][rR]|[rR][bB])(?=['\"])")


def _fold_words_line(line: str, cf_as_space: bool = False) -> str:
    line = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), line)
    line = _ANSI.sub(" ", line)
    line = re.sub(r"\\[nrt]", " ", line)
    line = re.sub(r"<[^>]{0,200}>", " ", line)
    line = re.sub(r"&[A-Za-z#0-9]{1,8};", " ", line)
    line = _STR_PREFIX.sub("", line)
    line = _LABEL.sub(" ", line)
    line = unicodedata.normalize("NFKD", line)
    line = "".join(
        (" " if cf_as_space else "") if unicodedata.category(c) == "Cf" else c
        for c in line
        if unicodedata.category(c) not in ("Mn", "Cc") or c in "\n\t"
    )
    return line.translate(_CONFUSABLES).lower()


def _bip39_runs(text: str) -> list:
    """Lines where a recovery phrase starts.

    Words are read after normalising (NFKD, homoglyph fold, tags/ANSI/escapes and string
    prefixes removed, labels and numbering ignored; invisible characters both removed and
    read as spaces). Three streams are checked: all words, the first word of every line,
    and the last word of every line (a phrase written one word per line behind a prefix).
    A hit needs a window of 12/15/18/21/24 words, at most one typo per 12 words, at least
    half distinct words, and mostly UNIFORM separators between the words (digits and
    whitespace ignored; one odd separator among eleven is fine), OR a whole run of listed
    words of exactly 12/15/18/21/24 with uniform separators (``abandon x11 about``)."""
    hits = set()
    lines = text.split("\n")
    for cf_as_space in (False, True):
        allt, firsts, lasts = [], [], []
        for line_no, line in enumerate(lines, 1):
            folded = _fold_words_line(line, cf_as_space)
            prev = None
            line_toks = []
            for mt in re.finditer(r"[a-z]+", folded):
                nsep = None if prev is None else re.sub(r"[\s\d]+", "", folded[prev:mt.start()])
                line_toks.append((mt.group(0), line_no, nsep))
                prev = mt.end()
            if line_toks:
                allt.extend(line_toks)
                firsts.append((line_toks[0][0], line_no, None))
                lasts.append((line_toks[-1][0], line_no, None))
        hits |= _window_hits(allt)
        for stream in (firsts, lasts):  # one word per line behind or before other text
            hits |= _window_hits(stream, per_line=True)
    return sorted(hits)


_NEAR_CACHE: dict = {}


def _edit_distance_le(a: str, b: str, limit: int) -> bool:
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= limit


def _near_listed(word: str) -> bool:
    """A mistyped phrase word: close to a listed word (1 edit for 3 to 4 letters, 2 for
    longer). Short or distant words are ordinary prose."""
    if word in _NEAR_CACHE:
        return _NEAR_CACHE[word]
    limit = 1 if len(word) <= 4 else 2
    near = len(word) >= 3 and any(_edit_distance_le(word, w, limit) for w in _BIP39 if abs(len(w) - len(word)) <= limit)
    _NEAR_CACHE[word] = near
    return near


def _uniform(window) -> bool:
    seps = [t[2] for t in window[1:] if t[2] is not None]
    if not seps:
        return True
    return Counter(seps).most_common(1)[0][1] * 4 >= len(seps) * 3


def _window_hits(toks, per_line: bool = False) -> set:
    """``per_line``: the stream holds one word per line (first or last word of each line);
    its windows must be on consecutive lines and nearly all distinct, because source code
    starts many consecutive lines with the same few listed words."""
    n = len(toks)
    hits = set()
    if n < 12:
        return hits
    inl = [1 if t[0] in _BIP39 else 0 for t in toks]
    pre = [0]
    for v in inl:
        pre.append(pre[-1] + v)
    i = 0
    while i < n:  # maximal runs of listed words
        if not inl[i]:
            i += 1
            continue
        j = i
        while j < n and inl[j]:
            j += 1
        length = j - i
        run = toks[i:j]
        if not per_line and _uniform(run) and (
            length in PHRASE_LENGTHS or (length >= 12 and len({t[0] for t in run}) * 2 >= length)
        ):
            hits.add(toks[i][1])
        i = j
    for length in PHRASE_LENGTHS:
        allowed = max(1, length // 12)
        i = 0
        while i + length <= n:
            if length - (pre[i + length] - pre[i]) <= allowed:
                window = toks[i:i + length]
                listed = [t for t in window if t[0] in _BIP39]
                misses = [t[0] for t in window if t[0] not in _BIP39]
                distinct = len({t[0] for t in listed})
                if (
                    (distinct * 4 >= length * 3 if per_line else distinct * 2 >= length)
                    and (not per_line or window[-1][1] - window[0][1] == length - 1)
                    and all(_near_listed(w) for w in misses)
                    and _uniform(window)
                ):
                    hits.add(listed[0][1])
                    i += length
                    continue
            i += 1
    return hits


# ------------------------------------------------------------ key=value

_KV = re.compile(
    r"(?<![A-Za-z0-9_.])-{0,2}(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{1,64})[\"']?[ \t]*(?P<sep>[:=])(?![=:])[ \t]*"
    r"(?=(?P<val>\"(?:[^\"\\\n]|\\.){0,400}\"|'(?:[^'\\\n]|\\.){0,400}'|[^\n]{0,400}))"
)
_QPAIR = re.compile(r"[\"']([A-Za-z_][A-Za-z0-9_.\-]{1,64})[\"']\s*,\s*[\"']([^\"'\n]{8,400})[\"']")
_LEAD = re.compile(r"[\"']((?:[A-Za-z]+[ _-]){1,2})$")


def _kv_hits(lines_text, add_line) -> None:
    for line_no, line in lines_text:
        if len(line) < 4:
            continue
        is_def = re.match(r"\s*(?:async\s+)?def\s", line) is not None
        for q in _QPAIR.finditer(line):  # SQL rows: VALUES('api_token','<value>')
            if (
                _name_is_secretish(q.group(1))
                and re.search(r"[0-9!@#$%^&*+/=]", q.group(2))  # a real value, not the next word of a list
                and not re.fullmatch(r"[\w./-]+\.[A-Za-z0-9]{1,5}", q.group(2))  # not a file name
                and _value_is_secret(q.group(2), q.group(1), True)
            ):
                add_line("env-assignment", line_no)
                break
        for m in _KV.finditer(line):
            name, sep, val = m.group("name"), m.group("sep"), m.group("val")
            if sep == ":" and is_def:
                continue  # a typed parameter in a function signature
            lead = (
                _LEAD.search(line[max(0, m.start("name") - 24):m.start("name")])
                if line[m.end("name"):m.end("name") + 1] in ("\"", "'")
                else None
            )
            if lead:  # a JSON key with a space: "public key", "secret seed"
                if re.search(r"(?i)(?:public|pub|storage)[ _-]$", lead.group(1)):
                    continue
                name = lead.group(1).replace(" ", "_") + name
            quoted = val[:1] in ("'", '"')
            inline_quoted = False
            qpos = m.start("name") - 1
            if (
                not quoted and qpos >= 0 and line[qpos] in ("'", '"')
                and re.search(r"(?i)\bEnvironment\s*=\s*$", line[:qpos]) is not None
            ):
                close = val.find(line[qpos])  # systemd: Environment="NAME=value with (punctuation)!"
                if close > 0:
                    val, quoted, inline_quoted = val[:close], True, True
            line_start = m.start() < 40 and re.fullmatch(r"\s*(?:export\s+|set\s+)?", line[:m.start()]) is not None
            if _name_is_secretish(name):
                cands = []
                if quoted:
                    cands.append((val, True))  # a quoted value is the whole value, taken whole
                else:
                    body = re.sub(r"\s+#.*$", "", val.strip())
                    first = re.split(r"[\s&]", body, maxsplit=1)[0]
                    has_more = len(body) > len(first)
                    if sep == ":" and has_more and not re.search(r"[0-9!@#$%^*+/=]", first):
                        first = ""  # prose after a colon: only a digit/symbol token counts
                    if first:
                        cands.append((first, False))
                    if has_more and line_start:  # NAME=<spaced value>: the whole rest of an env-style line
                        cands.append((body, True))
                for cand, whole_rest in cands:
                    clean = _clean_value(cand)
                    weak_plain = (
                        _name_is_weak(name) and not re.search(r"[0-9!@#$%^&*+/=]", clean) and len(clean) < 24
                    )
                    if not weak_plain and _value_is_secret(cand, name, quoted, line_start, inline_quoted, whole_rest):
                        add_line("env-assignment", line_no)
                        break
            elif _name_is_public_identifier(name):
                v = _clean_value(re.split(r"[\s&]", val.strip(), maxsplit=1)[0] if not quoted else val)
                if _hex_len(v) in (64, 128):  # public keys are SS58 here; raw hex is not an address
                    add_line("raw-key-hex", line_no)


def _json_hits(node, hits, depth=0):
    if depth > 40:
        return
    if isinstance(node, dict):
        keys = {str(k).lower() for k in node}
        if {"encoded", "encoding"} <= keys or {"crypto", "kdf"} <= keys or "ciphertext" in keys:
            hits.append("keystore-json")
        for v in node.values():
            _json_hits(v, hits, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _json_hits(v, hits, depth + 1)


# ------------------------------------------------------------ text scan

_B64_CHARS = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")


def _b64_candidates(text: str, lines: _Lines):
    """Yield ``(blob, line)``: inline blobs, and runs of wrapped base64 lines joined."""
    flat = text.replace("\n", " ")
    for m in re.finditer(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/=_-])", flat):
        yield m.group(0), lines.of(m.start())
    group, first = [], 0
    for line_no, line in enumerate(text.split("\n") + [""], 1):
        s = line.strip()
        if s and _B64_CHARS.fullmatch(s) and len(s) <= 200:
            if not group:
                first = line_no
            group.append(s)
            continue
        if group and s and re.fullmatch(r"[A-Za-z0-9+/_-]{2,}={0,2}", s) and len(s) < len(group[-1]):
            group.append(s)  # the short last line of a wrapped blob
        if len(group) >= 2:
            yield "".join(group), first
        group = []


def _decode_b64(blob: str):
    blob = blob.replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None


def _printable_text(raw: bytes):
    try:
        inner = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if inner and sum(c.isprintable() or c in "\n\t\r" for c in inner) / len(inner) > 0.9:
        return inner
    return None


def _source_inventory_labels(text: str) -> str:
    """Disambiguate path labels in canonical calculation source inventories.

    Only labels are neutralized; digest values and every other context field
    still pass through all scanners. This is the JSON equivalent of the existing
    sha256sum-line handling, not a hash-value or whole-file exclusion.
    """
    if '"source_agreement": "required_by_ingestion"' not in text:
        return text
    try:

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        context = json.loads(text, object_pairs_hook=unique)
        if (
            not isinstance(context, dict)
            or text != json.dumps(context, sort_keys=True, indent=2) + "\n"
        ):
            return text
        required = {
            "day",
            "height",
            "netuid",
            "genesis_hash",
            "profile_sha256",
            "candidate",
            "source_agreement",
        }
        if (
            not required <= context.keys()
            or context["source_agreement"] != "required_by_ingestion"
        ):
            return text
        candidate = context["candidate"]
        if not isinstance(candidate, dict) or set(candidate) != {
            "declared_commit",
            "source_sha256",
            "files",
        }:
            return text
        revision = candidate["declared_commit"]
        if revision is not None and (
            not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
        ):
            return text
        files = candidate["files"]
        if not isinstance(files, dict) or not files:
            return text
        component = r"[a-zA-Z_][a-zA-Z_0-9]*"
        folders = rf"(?:{component}/)*"
        source_path = (
            rf"(?:(?:noah|neurons)/{folders}{component}\.py"
            rf"|verification/noah-ts/src/{folders}{component}\.ts)"
        )
        for path, digest in files.items():
            if not re.fullmatch(source_path, path):
                return text
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                return text
        encoded = (json.dumps(files, sort_keys=True, indent=2) + "\n").encode()
        if hashlib.sha256(encoded).hexdigest() != candidate["source_sha256"]:
            return text
        # Canonical JSON fixes both the location and spelling of these labels.
        start = text.index('  "candidate": {\n')
        candidate_text = json.dumps(candidate, sort_keys=True, indent=2)
        candidate_text = "\n".join("  " + line for line in candidate_text.splitlines())
        end = start + len('  "candidate": ') + len(candidate_text) - 2
        region = text[start:end]
        for path in files:
            label = json.dumps(path) + ":"
            region = region.replace(label, '"' + "f" * len(path) + '":')
        return text[:start] + region + text[end:]
    except (ValueError, TypeError, RecursionError):
        return text

def scan_text(text: str, name: str = "<text>", depth: int = 0) -> list:
    """Scan decoded text. ``name`` is used as given (callers mask it)."""
    credential_text = _source_inventory_labels(text)
    found = []
    decoded = depth > 0
    pre = "decoded-" if decoded else ""
    lines = _Lines(text)
    flat = text.replace("\n", " ")
    credential_flat = credential_text.replace("\n", " ")

    def add(kind, index):
        found.append(Finding(name, lines.of(index), pre + kind))

    def add_line(kind, line_no):
        found.append(Finding(name, line_no, pre + kind))

    for m in _PEM.finditer(text):
        add("private-key-header", m.start())
    for line_no in _bip39_runs(text):
        add_line("bip39-run", line_no)
    for pat, kind in ((_HEX, "key-hex"), (_B64, "key-base64"), (_B58, "key-base58")):
        # Only hex-digest attribution uses neutralized source labels. All
        # intrinsic credential/phrase/encoded-content scans retain original text.
        for m in pat.finditer(flat):
            # A credential embedded in the filename itself must still be seen.
            in_label = flat[m.start():m.end()] != credential_flat[m.start():m.end()]
            key_flat = credential_flat if pat is _HEX and not in_label else flat
            if not _near_keyword(key_flat, m.start(), m.end()):
                continue
            if pat is _HEX:
                label_before = _BENIGN_LABEL.search(flat[max(0, m.start() - 30):m.start()])
                if label_before and not _strong_near(flat, m.start(), m.end(), 24):
                    continue  # "genesis hash: <hex>" near a stray word is fine; "seed hash: <hex>" is not
                if text[lines.start(m.start()):m.start()].strip() == "" and re.match(r" [ *]\S", text[m.end():m.end() + 3]):
                    continue  # a sha256sum line: "<hash>  <file name>"
            tok = m.group(0)
            if pat is not _HEX and not (
                re.search(r"[A-Z]", tok) and re.search(r"[a-z]", tok) and re.search(r"[0-9+/=]", tok)
            ):
                continue
            if pat is _B64 and re.fullmatch(r"[0-9a-fA-F]+", tok):
                continue
            add(kind, m.start())
    for m in _WIF.finditer(flat):
        tok = m.group(0)
        if re.search(r"[A-Z]", tok) and re.search(r"[a-z]", tok) and re.search(r"[0-9]", tok):
            add("key-base58", m.start())
    for pat in _TOKEN_SHAPES:
        for m in pat.finditer(text):
            add("token-shape", m.start())
    if _BACKBLAZE_CTX.search(flat):
        for m in _B2_KEY.finditer(flat):
            tok = m.group(0)
            if re.search(r"[0-9]", tok) and re.search(r"[A-Za-z]", tok) and _BACKBLAZE_CTX.search(
                flat[max(0, m.start() - 80):m.end() + 80]
            ):
                add("token-shape", m.start())
    _kv_hits(list(enumerate(credential_text.split("\n"), 1)), add_line)
    if re.search(r'"ciphertext"\s*:|"kdf"\s*:|"encoded"\s*:\s*"[^"]{20,}', text, re.I) and re.search(
        r'"(?:encoding|kdf|cipher)"', text, re.I
    ):
        add("keystore-json", 0)
    if text.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if parsed is not None:
            kinds = []
            _json_hits(parsed, kinds)
            for k in kinds:
                found.append(Finding(name, 1, pre + k))
    for blob, line in _b64_candidates(text, lines):
        if re.fullmatch(r"[0-9a-fA-F]+", blob):
            continue
        raw = _decode_b64(blob)
        if not raw or len(raw) < 8:
            continue
        inner = _printable_text(raw)  # text only: archives and binary are never decoded
        if inner is None:
            continue
        if depth >= _MAX_DEPTH:
            found.append(Finding(name, line, "decoded-too-deep"))
            continue
        for f in scan_text(inner, name, depth + 1):
            found.append(Finding(name, line, "decoded-" + f.kind.removeprefix("decoded-")))
    return found


def scan_bytes(data: bytes, name: str = "<bytes>") -> list:
    """Scan one blob of text. Invalid UTF-8 is a finding (``binary-refused``); the name is
    masked in the findings."""
    shown = mask_path(name)
    found = []
    if _BAD_NAME.search(Path(name).name) and not _ENV_NAME_OK.search(name):
        found.append(Finding(shown, 0, "wallet-file-name"))
    text = None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            text = None
    if text is None:
        try:
            text = data.decode("utf-8")
            if "\x00" in text:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "nul")
        except UnicodeDecodeError:
            return found + [Finding(shown, 0, "binary-refused")]
    return found + scan_text(text, shown)


# ---------------------------------------------------------- files and report


@dataclass
class ScanReport:
    """Result of one scan. Every name in it is already masked."""

    findings: list = field(default_factory=list)
    scanned: int = 0
    refused_expected: list = field(default_factory=list)
    refused_unexpected: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    expected_missing: list = field(default_factory=list)
    strict: bool = False

    @property
    def exit_code(self) -> int:
        if self.scanned == 0:
            return EXIT_ERROR
        if self.findings or (self.strict and self.expected_missing):
            return EXIT_HITS
        return EXIT_CLEAN

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "refused": {
                "expected_count": len(self.refused_expected),
                "unexpected_count": len(self.refused_unexpected),
                "expected": list(self.refused_expected),
                "unexpected": list(self.refused_unexpected),
            },
            "excluded": {"count": len(self.excluded), "names": list(self.excluded)},
            "expected_missing": list(self.expected_missing),
            "findings": [{"path": f.path, "line": f.line, "kind": f.kind} for f in self.findings],
            "strict": self.strict,
            "exit_code": self.exit_code,
            "note": "nothing was scanned" if self.scanned == 0 else "",
        }

    def summary_line(self) -> str:
        return (
            f"scanned={self.scanned} refused_expected={len(self.refused_expected)} "
            f"refused_unexpected={len(self.refused_unexpected)} excluded={len(self.excluded)} "
            f"expected_missing={len(self.expected_missing)} findings={len(self.findings)}"
        )


def _walk(root: Path, report: ScanReport):
    """Yield ``(file, relative name)`` for regular files under ``root``; report (never
    skip) symlinks, special files, ``.git`` directories and unreadable directories."""
    try:
        mode = os.lstat(root).st_mode
    except OSError:
        report.findings.append(Finding(mask_path(root), 0, "unreadable"))
        return
    if stat.S_ISREG(mode):
        yield root, root.name
        return
    if not stat.S_ISDIR(mode):
        report.findings.append(Finding(mask_path(root), 0, "special-file-refused"))
        return

    def onerror(err):
        report.findings.append(Finding(mask_path(getattr(err, "filename", root)), 0, "unreadable"))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        dirnames.sort()
        for d in list(dirnames):
            p = Path(dirpath, d)
            if p.is_symlink():
                report.findings.append(Finding(mask_path(p), 0, "symlink-refused"))
                dirnames.remove(d)
            elif d == ".git":
                report.findings.append(Finding(mask_path(p), 0, "git-directory-refused"))
                dirnames.remove(d)
        for f in sorted(filenames):
            p = Path(dirpath, f)
            try:
                m = os.lstat(p).st_mode
            except OSError:
                report.findings.append(Finding(mask_path(p), 0, "unreadable"))
                continue
            if stat.S_ISLNK(m):
                report.findings.append(Finding(mask_path(p), 0, "symlink-refused"))
            elif not stat.S_ISREG(m):
                report.findings.append(Finding(mask_path(p), 0, "special-file-refused"))
            else:
                yield p, str(p.relative_to(root)).replace(os.sep, "/")


def _allowed(name: str, exts, names) -> bool:
    return name in names or any(name.lower().endswith(e) for e in exts)


def scan(paths, *, scope: str = "base", excluded=(), expected_refused=(), strict: bool = False) -> ScanReport:
    """Scan files and directories. ``paths`` may be an explicit list of files.

    ``scope`` is ``"base"`` (bundles, backup inputs) or ``"release"``. ``excluded`` is an
    explicit list of exact paths (absolute, or relative to a scanned directory) that are
    skipped and counted; never patterns. ``expected_refused`` lists names (exact relative
    path or exact base name) that are binary by design: refusing them is normal; any other
    refused file is an ``unexpected-file`` finding. Raises :class:`ScanError` for a path
    that does not exist. ``report.exit_code`` is 2 when nothing was scanned."""
    if scope not in ("base", "release"):
        raise ScanError(f"unknown scope {scope!r}")
    exts, names = (RELEASE_EXTS, RELEASE_NAMES) if scope == "release" else (BASE_EXTS, BASE_NAMES)
    excl = {str(e) for e in excluded}
    expected = {str(e) for e in expected_refused}
    seen_expected = set()
    report = ScanReport(strict=strict)
    for p in paths:
        root = Path(p)
        if not os.path.lexists(root):
            raise ScanError("a path to scan does not exist")
        for f, rel in _walk(root, report):
            shown = mask_path(f)
            if str(f) in excl or rel in excl:
                report.excluded.append(shown)
                continue
            hit = next((e for e in (f.name, rel, str(f)) if e in expected), None)
            if hit is not None and not _allowed(f.name, exts, names):
                seen_expected.add(hit)  # named by the caller as binary or encrypted by design
                report.refused_expected.append(shown)
                continue
            if _BAD_NAME.search(f.name) and not _ENV_NAME_OK.search(f.name):
                report.refused_unexpected.append(shown)  # refused by name, never opened: counted as refused
                report.findings.append(Finding(shown, 0, "wallet-file-name"))
                continue
            if not _allowed(f.name, exts, names):
                report.refused_unexpected.append(shown)
                report.findings.append(Finding(shown, 0, "unexpected-file"))
                continue
            try:
                if os.stat(f).st_size > _MAX_FILE_BYTES:
                    report.findings.append(Finding(shown, 0, "file-too-large"))
                    continue
                data = f.read_bytes()
            except OSError:
                report.findings.append(Finding(shown, 0, "unreadable"))
                continue
            report.scanned += 1
            try:
                for fd in dict.fromkeys(scan_bytes(data, str(f))):  # de-duplicate within one file only
                    if fd.kind != "wallet-file-name":
                        report.findings.append(fd)
            except Exception:  # noqa: BLE001 -- fail closed without leaking anything
                report.findings.append(Finding(shown, 0, "scan-error"))
    report.expected_missing = sorted(mask_path(e) for e in expected - seen_expected)
    return report


def scan_paths(paths, **kw) -> list:
    """``scan`` returning only the findings. Raises :class:`ScanError` when no file was
    scanned and nothing else was found (an empty scan is never "clean")."""
    report = scan(paths, **kw)
    if report.scanned == 0 and not report.findings:
        raise ScanError("nothing was scanned: no files were found under the given paths")
    return report.findings
