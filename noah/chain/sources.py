"""Reading the same chain fact from more than one source, and refusing to guess.

The whole module is one idea: a number that decides what somebody is paid may
only be used if independent sources, each demonstrably answering honestly about
the block in question, gave the same answer. Anything short of that is a
refusal, never a value.

A refusal deliberately does not inherit from ``Exception``. The most common
wrapper around a flaky chain read is a retry loop with ``except Exception``, and
a refusal that such a loop can swallow is not a refusal at all: the run finishes
with a plausible number and nobody is told.
"""

import argparse
import ipaddress
import logging
import re
import sys
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit


class Refusal(BaseException):
    """The reader declined to produce a number. Never catch this broadly."""


class SourceDisagreement(Refusal):
    """Sources gave different answers for the same read."""


class NotEnoughSources(Refusal):
    """Fewer independent sources answered than agreement requires."""


class SourceLied(Refusal):
    """A source gave an answer that cannot be honest."""


class Unverified(Refusal):
    """A source contributed to a read without having been checked at that block."""


MINIMUM_SOURCES = 2


@dataclass(frozen=True)
class Acceptance:
    """Evidence that one source answered honestly about one specific block.

    Acceptance is a value rather than a side effect so a caller cannot forget to
    consume it, and it is scoped to a single block because the failure it guards
    against is a property of the block. A source checked at block N tells you
    nothing about block N-1.
    """

    source: str
    block: int
    endpoint: str

    def __repr__(self):
        # The endpoint's label only: a hosted RPC URL may carry an API key.
        return (
            f"Acceptance(source={self.source!r}, block={self.block!r}, "
            f"endpoint={endpoint_label(self.endpoint)!r})"
        )


#: A Substrate block hash is a fixed-size 32-byte (256-bit) digest, always --
#: never shorter, never longer, and never anything but plain ASCII hex
#: digits. `validated_hash_digits` below checks this BEFORE trusting a
#: value as a real hash, so two broken sources that happen to agree on an
#: implausible placeholder (`0x1`, say -- parses fine as hex, decodes to a
#: real nonzero integer, and would otherwise clear every guard
#: `check_source_at_block` applies) cannot pass as agreement on a real block.
_BLOCK_HASH_HEX_DIGITS = 64

_HEX_DIGITS_RE = re.compile(r"[0-9a-fA-F]+")


def validated_hash_digits(block_hash):
    """Return canonical hex digits only for a valid 32-byte block-hash shape."""
    if isinstance(block_hash, bool) or not isinstance(block_hash, str):
        return None
    digits = block_hash[2:] if block_hash[:2].lower() == "0x" else block_hash
    if len(digits) != _BLOCK_HASH_HEX_DIGITS or not _HEX_DIGITS_RE.fullmatch(digits):
        return None
    return digits.lower()


def hash_canary(block_hash):
    """Convert a valid, nonzero block hash into a source-check canary; refuse malformed values."""
    digits = validated_hash_digits(block_hash)
    if digits is not None:
        return int(digits, 16)
    if isinstance(block_hash, int) and not isinstance(block_hash, bool):
        return repr(block_hash)
    return block_hash


def comparable_hash(block_hash):
    """Canonicalize valid hash spelling for equality and retain malformed values for refusal."""
    digits = validated_hash_digits(block_hash)
    return f"0x{digits}" if digits is not None else block_hash


# The default port for each scheme canonical_endpoint below recognises, so
# "ws://node" and "ws://node:80" fold to the identical string rather than
# counting as two distinct endpoints for the SAME node (RFC 3986 6.2.3).
_DEFAULT_PORTS = {"ws": 80, "http": 80, "wss": 443, "https": 443}


def canonical_endpoint(url):
    """Normalize scheme, host, default port, root path and fragment for endpoint comparison.

    Retain path and query because they may select distinct resources. Equality
    prevents counting spelling variants twice; inequality does not prove independence."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    raw_host = parts.hostname
    if not raw_host:
        return url
    host = raw_host.lower()
    if host != "." and host.endswith("."):
        # A trailing DNS root dot names the identical fully-qualified
        # domain -- "node.example" and "node.example." are the same host to
        # any real resolver and to the WebSocket client that dials it, but
        # would otherwise be two different identity strings here.
        host = host[:-1]
    is_ipv6 = False
    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP address. Fold a Unicode hostname and its punycode
        # spelling together too -- the SAME encoding a real WebSocket client
        # dials before either form ever reaches a socket, so this fails
        # toward the lowercased form on any name the codec will not accept
        # rather than inventing a second notion of "valid hostname."
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    else:
        host = str(parsed_ip)
        is_ipv6 = parsed_ip.version == 6

    scheme = parts.scheme.lower()
    default_port = _DEFAULT_PORTS.get(scheme)
    bracketed_host = f"[{host}]" if is_ipv6 else host
    netloc = bracketed_host if port is None or port == default_port else f"{bracketed_host}:{port}"
    path = "" if parts.path == "/" else parts.path
    return urlunsplit((scheme, netloc, path, parts.query, ""))


ENDPOINT_PLACEHOLDER = "(endpoint)"


def endpoint_label(url):
    """The part of an endpoint URL that is safe to print, log or record.

    ``scheme://host[:port]`` only. Some RPC providers put the API key in the URL
    (``wss://user:pw@host``, ``wss://host/<key>``, ``?apikey=...``), so userinfo,
    path, query and fragment are dropped. Anything that cannot be parsed into a
    scheme and a host gives ``ENDPOINT_PLACEHOLDER`` and is never echoed.

    DISPLAY ONLY. Two different keyed URLs on one host share a label but are
    different endpoints: identity checks must keep using ``canonical_endpoint``
    of the full URL, and connections must dial the full URL.
    """
    if not isinstance(url, str):
        return ENDPOINT_PLACEHOLDER
    try:
        parts = urlsplit(url)
        scheme = parts.scheme
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ENDPOINT_PLACEHOLDER
    if not scheme or not host:
        return ENDPOINT_PLACEHOLDER
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme.lower()}://{host}" + (f":{port}" if port is not None else "")


_MIN_SCRUB_PIECE = 4
_URL_IN_TEXT = re.compile(r"\b(?:wss?|https?)://[^\s'\"]+", re.IGNORECASE)


def _label_of_token(token):
    """The label of a URL token cut from free text, which may end in punctuation."""
    label = endpoint_label(token)
    while label == ENDPOINT_PLACEHOLDER and token and token[-1] in ").]>,;.":
        token = token[:-1]
        label = endpoint_label(token)
    return label


def scrub_endpoint_text(text, *urls):
    """Never raises: if the scrub itself fails, nothing of ``text`` is echoed.

    ``text`` with every given endpoint URL, and the secret-bearing parts of it
    (userinfo, path, query), replaced by ``endpoint_label``.

    For exception text from a library that echoes the URL it was given (for
    example ``InvalidURI: <url> isn't a valid URI``). Display only.
    """
    try:
        return _scrub_endpoint_text(text, urls)
    except Exception:  # noqa: BLE001 - redaction must not turn a refusal into a crash
        return "(text withheld: it could not be scrubbed)"


def _scrub_endpoint_text(text, urls):
    text = str(text)
    for url in urls:
        if not isinstance(url, str) or not url:
            continue
        label = endpoint_label(url)
        pieces = [url]
        try:
            parts = urlsplit(url)
            pieces += [parts.username, parts.password, parts.query]
            if len(parts.path) > 1:
                pieces += [parts.path, *parts.path.split("/")]
        except ValueError:
            pass

        # The full URL is always replaced. A short piece (a one-letter user) would
        # mangle unrelated words, so pieces replace only from 4 characters up. Each is also
        # replaced in its percent-encoded and percent-decoded spellings.
        def spellings(piece):
            # errors="replace": a lone surrogate or invalid byte in an operator's
            # URL must not make the redaction itself raise.
            return {
                piece,
                quote(piece, safe="", errors="replace"),
                quote(piece, errors="replace"),
                unquote(piece, errors="replace"),
                quote(unquote(piece, errors="replace"), safe="", errors="replace"),
            }

        full = spellings(url)
        smaller = set()
        for piece in pieces[1:]:
            if piece and len(piece) >= _MIN_SCRUB_PIECE:
                smaller |= spellings(piece)
        for variant in sorted(full, key=len, reverse=True):
            text = text.replace(variant, label)
        for variant in sorted(smaller - full, key=len, reverse=True):
            if len(variant) >= _MIN_SCRUB_PIECE:
                text = text.replace(variant, "(hidden)")
    # Any other endpoint-shaped URL in the text (a spelling the library normalised)
    # is reduced to its label as well.
    return _URL_IN_TEXT.sub(lambda match: _label_of_token(match.group(0)), text)


# Third-party loggers that print the URL they connect to ("Websocket connecting to
# <url>" at DEBUG; websockets logs the request path). Their records are held at
# WARNING and above, and whatever is still emitted is scrubbed.
_URL_LOGGERS = ("async_substrate_interface", "websockets", "bittensor")
_logging_guarded = False


def guard_endpoint_logging():
    """Keep an endpoint URL, and any key in it, out of third-party log records.

    Call it at the start of every entry point, and again after any
    ``logging.basicConfig``/level change. Idempotent. The level clamp makes the
    URL-printing loggers silent below WARNING whatever NOAH_LOG_LEVEL says; the
    record factory scrubs the text of any record they still emit.
    """
    global _logging_guarded
    from .. import secretscrub

    secretscrub.activate()
    names = [
        name
        for name in list(logging.root.manager.loggerDict)
        if name in _URL_LOGGERS or name.startswith(tuple(f"{p}." for p in _URL_LOGGERS))
    ]
    for name in set(names) | set(_URL_LOGGERS):
        logger = logging.getLogger(name)
        unset_or_lower = logger.level == logging.NOTSET or logger.level < logging.WARNING
        if name != "bittensor" and unset_or_lower:
            logger.setLevel(logging.WARNING)
    if _logging_guarded:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        name = record.name
        if name in _URL_LOGGERS or name.startswith(tuple(f"{p}." for p in _URL_LOGGERS)):
            try:
                text = record.getMessage()
            except Exception:  # noqa: BLE001 - never let logging raise
                text = str(record.msg)
            from .. import secretscrub

            record.msg, record.args = secretscrub.scrub(scrub_endpoint_text(text)), ()
        return record

    logging.setLogRecordFactory(factory)
    _logging_guarded = True


class QuietArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose errors never echo a value.

    argparse repeats the offending text ("unrecognized arguments: <text>", "invalid int
    value: '<text>'"), and an operator may have pasted an endpoint URL with a key in it into
    the wrong place. Prints the usage and a fixed line, exits 2 as argparse does.

    ``fixed_messages`` is the set of constant texts a script's own ``type=`` checks raise that
    carry no typed value; a message of exactly ``argument <option>: <one of them>`` is shown,
    everything else is replaced. No keyword matching.
    """

    def __init__(self, *args, fixed_messages=frozenset(), **kwargs):
        super().__init__(*args, **kwargs)
        self._fixed_messages = frozenset(fixed_messages)

    def error(self, message):
        shown = re.match(r"^argument [^:]+: (.*)$", message, re.S)
        if shown and shown.group(1) in self._fixed_messages:
            self.print_usage(sys.stderr)
            self.exit(2, f"{self.prog}: error: {message}\n")
        self.print_usage(sys.stderr)
        self.exit(
            2, f"{self.prog}: error: invalid or unrecognized arguments (values are not echoed)\n"
        )


def connect_scrubbed(factory, url, error_type):
    """``factory()``, but a failure is re-raised as ``error_type`` with the
    endpoint scrubbed from its text and the URL-bearing original not chained."""
    guard_endpoint_logging()
    try:
        return factory()
    except Exception as failure:
        raise error_type(scrub_endpoint_text(f"{type(failure).__name__}: {failure}", url)) from None


def check_source_at_block(source, block, canary_value, expected=None, endpoint=None):
    """Accept a source for reads at ``block``, or refuse it.

    The canary is a fact whose honest answer at this block is known, or at
    minimum cannot be zero. Anything else is refused, including every shape the
    chain uses to say nothing: hex strings, decimal strings, null, empty
    containers. Decoding happens at the edge; a value that arrives here still
    wearing its wire format has not been decoded, and guessing what it meant is
    the failure this module exists to prevent.
    """
    if isinstance(canary_value, bool) or not isinstance(canary_value, int):
        raise SourceLied(
            f"{source}: canary read at block {block} came back as {canary_value!r}, which is "
            f"not a decoded integer. Values that reach this check must already be decoded; "
            f"an undecoded value is refused rather than interpreted."
        )
    if canary_value < 0:
        raise SourceLied(
            f"{source}: canary read at block {block} came back negative ({canary_value})."
        )
    if canary_value == 0:
        raise SourceLied(
            f"{source}: canary read at block {block} returned 0, which cannot be the honest "
            f"answer. Every read from this source at this block is refused."
        )
    if expected is not None and canary_value != expected:
        raise SourceLied(
            f"{source}: canary read at block {block} returned {canary_value}, expected "
            f"{expected}. The source is answering, but not correctly."
        )
    return Acceptance(source=source, block=block, endpoint=endpoint or source)


def agreed(
    field,
    results,
    block,
    accepted,
    *,
    minimum_sources=MINIMUM_SOURCES,
    zero_is_impossible=False,
):
    """The value every independent, verified source agrees on.

    ``results`` maps a source name to its answer. ``accepted`` is the acceptances
    those sources earned at this same ``block``.

    Refuses when: fewer than ``minimum_sources`` distinct endpoints answered, any
    contributing source was not checked at this block, the answers differ in
    value or in type, or the unanimous answer is zero for a field whose honest
    answer cannot be zero.

    That last case is the one agreement alone cannot catch. The silent zero is a
    property of the block, so two pruned endpoints asked about the same block
    agree perfectly on zero, both scoring engines compute correctly from it, and
    both agree byte for byte on a wrong answer.
    """
    if minimum_sources < MINIMUM_SOURCES:
        raise ValueError(
            f"agreement requires at least {MINIMUM_SOURCES} sources; {minimum_sources} was "
            f"asked for. This is the guarantee, not a tunable."
        )

    verified = {a.source: a for a in accepted if a.block == block}
    unverified = sorted(set(results) - set(verified))
    if unverified:
        raise Unverified(
            f"{field} at block {block}: {', '.join(unverified)} contributed without passing a "
            f"check at this block. A source verified at another block is not verified here, "
            f"because the failure being guarded against is a property of the block."
        )

    endpoints = {verified[name].endpoint for name in results}
    if len(endpoints) < minimum_sources:
        answered = ", ".join(sorted(results)) or "none"
        raise NotEnoughSources(
            f"{field} at block {block}: {len(results)} source(s) answered ({answered}) but they "
            f"resolve to {len(endpoints)} distinct endpoint(s), and agreement requires at least "
            f"{minimum_sources}. Two names for one endpoint is one source."
        )

    values = list(results.values())
    first = values[0]
    if any((type(v), v) != (type(first), first) for v in values[1:]):
        detail = ", ".join(f"{source}={value!r}" for source, value in sorted(results.items()))
        raise SourceDisagreement(f"{field} at block {block}: sources disagree: {detail}")

    if zero_is_impossible and first == 0:
        answered = ", ".join(sorted(results))
        raise SourceLied(
            f"{field} at block {block}: every source ({answered}) returned 0, which cannot be "
            f"the honest answer for this field. Agreement on an impossible value is evidence "
            f"the sources share a fault, not evidence the value is right."
        )

    return first
