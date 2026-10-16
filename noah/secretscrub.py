"""A backstop that keeps the access key inside a configured endpoint URL out of any output.

The real secret is the key a hosted RPC provider puts in the URL (path segment, user info or query).
Site-by-site fixes cover the messages Noah writes itself; this module covers everything else. Once
the endpoint URLs are known (from the settings or the environment, or the content of a file named
by ``NOAH_ENDPOINT_2_FILE``) they are registered here, and ``install()`` wraps ``sys.stdout`` and
``sys.stderr`` and the exception hook so that any known secret piece is replaced before it is
written. The logging record factory in ``noah.chain.sources`` uses the same pieces.

Which pieces count as secret (the rule, so that a false hit is rare):
  * the whole URL, always;
  * the user info (user and password), and a password of any length of 4 or more;
  * every path segment of 8 characters or more;
  * the LAST path segment when it is 4 characters or more, contains a digit and is not a common
    word (``rpc``, ``api``, ``ws``, ``v1`` ...), because that is where providers put a short key.
    A short segment without a digit (``fake``) is treated as a word and ignored, to keep ordinary
    text intact;
  * a query value of 8 characters or more, or of 4 or more when its name looks like a key
    (``key``, ``token``, ``secret``, ``auth``, ``apikey`` ...);
  * each of these also in its percent-encoded, percent-decoded and lowercase forms.
Anything shorter is ignored. A key placed in the host name itself is not detected (it looks like a
host). A piece split across two separate ``write`` calls is not caught: print builds each line from
whole pieces, so this does not arise from ordinary use.

The registry never prints, logs or reprs its pieces.

KNOWN LIMITS (this is defense in depth; the site fixes are the primary protection):
  * a key split across two separate ``write`` calls is not caught;
  * output that bypasses ``sys.stdout``/``sys.stderr`` is not filtered: ``sys.stdout.buffer``,
    ``os.write``, ``sys.__stdout__``, a subprocess's own output;
  * anything written to a file is not filtered;
  * a key placed in the host name or in the URL fragment is not detected;
  * a middle path segment shorter than 8 characters, and a last segment of 3 characters or a
    word-like one (no digit), are not treated as secret;
  * conversely a last segment of 4 or more characters that contains a digit IS hidden everywhere in
    the process output, which can corrupt an unrelated number or hash that contains it;
  * the registry lock is not re-entrant: a signal handler that prints while the main thread is
    inside ``scrub`` could block;
  * the registry only knows endpoints it was given (settings, the environment, the second-endpoint
    file); a key typed anywhere else is protected by the site fixes only.
Nothing here raises: a value that cannot be encoded, a closed or missing stream, or an unreadable
endpoint file is skipped silently.
"""

import logging
import os
import re
import stat
import sys
import threading
import traceback
from urllib.parse import quote, unquote, urlsplit

from .chain.sources import endpoint_label

_COMMON_SEGMENTS = frozenset(
    {
        "rpc",
        "api",
        "ws",
        "wss",
        "v1",
        "v2",
        "v3",
        "http",
        "https",
        "node",
        "chain",
        "main",
        "mainnet",
        "public",
        "archive",
        "jsonrpc",
        "websocket",
        "socket",
    }
)
_KEYISH = re.compile(r"(?i)(key|token|secret|auth|pass|sig)")
_ENV_ENDPOINTS = (
    "NOAH_CHAIN_ENDPOINT",
    "NOAH_ENDPOINT_2",
    "NOAH_SUBMIT_ENDPOINT",
    "NOAH_AUTHOR_WS",
    "NOAH_AUTHOR_RPC",
    "NOAH_READER_WS",
    "NOAH_READER_RPC",
)
_lock = threading.Lock()


def _variants(text):
    """The spellings of ``text`` a message might carry (one that cannot be made is skipped)."""
    found = {text}
    for make in (
        lambda: quote(text, safe="", errors="replace"),
        lambda: quote(text, errors="replace"),
        lambda: unquote(text, errors="replace"),
        lambda: text.lower(),
    ):
        try:
            found.add(make())
        except Exception:  # noqa: BLE001
            pass
    return found


class _Registry:
    def __init__(self):
        self._urls = {}  # url -> label
        self._pieces = set()

    def __repr__(self):
        return "<secret registry (contents not shown)>"

    def clear(self):
        with _lock:
            self._urls.clear()
            self._pieces.clear()

    def add(self, url):
        try:
            self._add(url)
        except Exception:  # noqa: BLE001 - registering must never break an entry point
            pass

    def has_content(self):
        return bool(self._urls or self._pieces)

    def _add(self, url):
        if not isinstance(url, str) or "://" not in url:
            return
        try:
            parts = urlsplit(url.strip())
            _ = parts.port
        except ValueError:
            return
        pieces = set()
        segments = [seg for seg in parts.path.split("/") if seg]
        for index, segment in enumerate(segments):
            last = index == len(segments) - 1
            keylike = any(ch.isdigit() for ch in segment)  # "fake" is a word; "k9x2m4" is a key
            if len(segment) >= 8 or (
                last and len(segment) >= 4 and keylike and segment.lower() not in _COMMON_SEGMENTS
            ):
                pieces.add(segment)
        if parts.username and len(parts.username) >= 8:
            pieces.add(parts.username)
        if parts.password and len(parts.password) >= 4:
            pieces.add(parts.password)
        for pair in parts.query.split("&"):
            name, _, value = pair.partition("=")
            if value and (len(value) >= 8 or (len(value) >= 4 and _KEYISH.search(name))):
                pieces.add(value)
        spellings = set()
        for piece in pieces:
            for variant in _variants(piece):
                spellings.add(variant)
                spellings.add(variant.lower())
        url = url.strip()
        label = endpoint_label(url)
        with _lock:
            self._urls[url] = label
            for variant in _variants(url):
                self._urls.setdefault(variant, label)
            self._pieces |= {p for p in spellings if p}

    def scrub(self, text):
        if not isinstance(text, str) or not text:
            return text
        with _lock:
            urls = sorted(self._urls.items(), key=lambda item: len(item[0]), reverse=True)
            pieces = sorted(self._pieces, key=len, reverse=True)
        for url, label in urls:
            if url in text:
                text = text.replace(url, label)
        for piece in pieces:
            if piece.lower() in text.lower():
                text = re.sub(re.escape(piece), "(hidden)", text, flags=re.IGNORECASE)
        return text


_registry = _Registry()


def register_endpoint(url):
    """Register one endpoint URL (its pieces are secret from now on)."""
    _registry.add(url)


def register_settings(settings):
    """Register every endpoint a Settings object holds."""
    for name in ("chain_endpoint", "second_chain_endpoint", "submit_endpoint"):
        register_endpoint(getattr(settings, name, None))


def register_environment(environ=None):
    """Register the endpoint URLs in the environment, and the content of NOAH_ENDPOINT_2_FILE."""
    try:
        environ = os.environ if environ is None else environ
        for name in _ENV_ENDPOINTS:
            register_endpoint(str(environ.get(name, "")).strip())
        path = str(environ.get("NOAH_ENDPOINT_2_FILE", "")).strip()
        if path:
            register_endpoint(_read_small_regular_file(path))
    except Exception:  # noqa: BLE001 - registering must never break an entry point
        pass


MAX_ENDPOINT_FILE_BYTES = 64 * 1024


def _read_small_regular_file(path):
    """The text of a regular file of at most 64 KiB, else None: a FIFO, a device or a huge file is
    never read here (opening a FIFO would block)."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENDPOINT_FILE_BYTES:
            return None
        with open(path, "rb") as handle:
            return handle.read(MAX_ENDPOINT_FILE_BYTES).decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return None


def scrub(text):
    """``text`` with every registered secret piece replaced."""
    return _registry.scrub(text)


def reset():
    """Forget every registered endpoint (tests)."""
    _registry.clear()


class ScrubbingStream:
    """A text stream that scrubs what is written to it."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        if isinstance(text, str):
            try:
                text = _registry.scrub(text)
            except Exception:  # noqa: BLE001 - never lose or break output over the filter
                pass
        return self._stream.write(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __iter__(self):
        return iter(self._stream)


_installed = False
_enabled = True  # tests switch this off so that site tests run without the backstop (see conftest)
_log_factory_installed = False


def _excepthook(kind, value, trace):
    text = "".join(traceback.format_exception(kind, value, trace))
    stream = sys.stderr
    if stream is not None:
        stream.write(text)


def _install_log_scrub():
    """Scrub the message, arguments and traceback text of EVERY log record at creation, so a handler
    that was bound to the raw stream before ``install`` still emits scrubbed text."""
    global _log_factory_installed
    if _log_factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            if _registry.has_content():
                record.msg, record.args = _registry.scrub(record.getMessage()), ()
                if record.exc_info and isinstance(record.exc_info, tuple) and not record.exc_text:
                    record.exc_text = _registry.scrub(
                        "".join(traceback.format_exception(*record.exc_info))
                    )
                if record.stack_info:
                    record.stack_info = _registry.scrub(record.stack_info)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return record

    logging.setLogRecordFactory(factory)
    _log_factory_installed = True


def install():
    """Wrap sys.stdout/stderr (when they exist), route uncaught exceptions and every log record
    through the scrub. Idempotent; never raises."""
    global _installed
    try:
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name)
            if stream is not None and not isinstance(stream, ScrubbingStream):
                setattr(sys, name, ScrubbingStream(stream))
        sys.excepthook = _excepthook
        _install_log_scrub()
        _installed = True
    except Exception:  # noqa: BLE001
        pass


def set_enabled(value):
    """Switch ``activate`` on or off (tests run site tests with the backstop off)."""
    global _enabled
    _enabled = bool(value)


def activate(settings=None):
    """The one call an entry point makes: register what is configured, then install (no raise)."""
    if not _enabled:
        return
    try:
        register_environment()
        if settings is not None:
            register_settings(settings)
        install()
    except Exception:  # noqa: BLE001
        pass
