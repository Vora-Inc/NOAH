"""Separate the validator's commit identity from its proxy signer.

The commit identity holds the seat and owns the published weights. The dedicated
proxy key signs the restricted Proxy.proxy call, pays its fee and supplies its
nonce. NOAH_PROXY_SIGNER_ADDRESS enables the route; NOAH_EXPECTED_SIGNER_ADDRESS
names the commit identity. Mainnet requires this route. The module checks identity,
delegation and available balance; it does not create keys or delegations."""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..chain.ss58 import NotAnAddress, public_key_of
from ..pathtext import plain_path_text
from .publish_intent import PublicationStatusUnknown
from .settings import SettingsError

# The chain's own name for the restricted proxy type this route needs, and the delay a
# signer must have: a delayed proxy needs an announcement first and cannot send directly.
REQUIRED_PROXY_TYPE = "NonFungible"
REQUIRED_DELAY = 0

# Documented defaults (override with the constructor in tests, not by environment):
# the free balance, in rao, the proxy signer must hold before a send is attempted. A
# failed proxy send still costs its fee; this is a floor, not a budget.
MIN_SIGNER_BALANCE_RAO = 100_000_000  # 0.1 TAO

SIGNER_ENV = "NOAH_PROXY_SIGNER_ADDRESS"


class ProxyRouteRefused(SettingsError):
    """The proxy route is misconfigured or its delegation is not what it must be.
    Nothing is signed."""


class CommitIdentity:
    """The public face of the commit identity: all this code ever holds of it.

    ``ss58_address`` and ``public_key`` mirror the two attributes the existing code reads
    from a hotkey keypair (``wallet.hotkey.ss58_address`` for records and scans,
    ``hotkey.public_key`` for the sealed payload), so substituting it changes nothing
    else. It has no signing ability by construction."""

    def __init__(self, ss58_address):
        try:
            key = public_key_of(ss58_address)
        except (NotAnAddress, TypeError, ValueError) as error:
            raise ProxyRouteRefused(
                "the commit identity address (the value is not shown) is not a valid address "
                f"({type(error).__name__}). Nothing is signed."
            ) from None
        self.ss58_address = ss58_address
        self.public_key = bytes(key)

    def __repr__(self):
        return f"CommitIdentity({self.ss58_address})"


def _same(a, b):
    try:
        return public_key_of(a) == public_key_of(b)
    except (NotAnAddress, TypeError, ValueError):
        return a == b


def proxy_mode(settings):
    value = getattr(settings, "proxy_signer_address", None)
    return isinstance(value, str) and bool(value)


def commit_address(settings, loaded_ss58):
    """The address every validator-identity question (seat, registration, rate limit,
    records, scan) is asked about. In proxy mode that is the configured commit identity,
    never the loaded key; otherwise it is the loaded hotkey, as before."""
    if proxy_mode(settings):
        expected = getattr(settings, "expected_signer_address", None)
        if isinstance(expected, str) and expected:
            return expected
        raise ProxyRouteRefused(
            f"{SIGNER_ENV} is set but NOAH_EXPECTED_SIGNER_ADDRESS (the commit identity's "
            f"public address) is not. In the proxy route the loaded key is NOT the "
            f"validator, so the validator's address must be named. Nothing is signed."
        )
    return loaded_ss58


def identity_from_settings(settings):
    """``CommitIdentity`` in proxy mode, ``None`` otherwise."""
    if not proxy_mode(settings):
        return None
    return CommitIdentity(commit_address(settings, None))


def check_proxy_signing_identity(settings, loaded_ss58):
    """Real runs only. In proxy mode the loaded wallet hotkey must be the configured proxy
    signer and must NOT be the commit identity (the validator's own key never belongs on a
    server). A no-op when the proxy route is off."""
    if not proxy_mode(settings):
        return
    signer = settings.proxy_signer_address
    commit = commit_address(settings, loaded_ss58)
    if _same(signer, commit):
        raise ProxyRouteRefused(
            f"{SIGNER_ENV} equals the commit identity (the value is not shown). The proxy "
            f"signer must be a "
            f"different, dedicated key. Nothing is signed."
        )
    if _same(loaded_ss58, commit):
        raise ProxyRouteRefused(
            "the loaded wallet hotkey IS the commit identity (the value is not shown). "
            "The validator's own "
            "key must never be loaded on a server; the proxy route signs with the dedicated "
            "configured proxy signer. Nothing was signed."
        )
    if not _same(loaded_ss58, signer):
        raise ProxyRouteRefused(
            f"the loaded wallet hotkey is {loaded_ss58}, but {SIGNER_ENV} (the value is not "
            f"shown) is a different address. Wrong "
            f"wallet or wrong hotkey name. Nothing was signed."
        )


# ---------------------------------------------------------------------------
# Delegation and balance, read-only
# ---------------------------------------------------------------------------


def _unwrap(value):
    return getattr(value, "value", value)


def _address_of(raw):
    """An ss58 string from whatever shape a delegate arrives in, or ``None``."""
    raw = _unwrap(raw)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("Id", "id", "value", "ss58_address"):
            if key in raw:
                return _address_of(raw[key])
    return None


def _type_name(raw):
    raw = _unwrap(raw)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and len(raw) == 1:
        return next(iter(raw))
    return None


def parse_proxies(raw):
    """``[{delegate, proxy_type, delay}]`` from a ``Proxy.Proxies`` storage value, or
    raise ``ValueError`` when the shape is not understood (the caller fails closed)."""
    raw = _unwrap(raw)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and isinstance(_unwrap(raw[0]), (list, tuple)):
        entries = _unwrap(raw[0])  # (proxies, deposit)
    elif isinstance(raw, (list, tuple)):
        entries = raw
    else:
        raise ValueError(f"unexpected Proxy.Proxies shape {type(raw).__name__}")
    out = []
    for entry in entries:
        entry = _unwrap(entry)
        if not isinstance(entry, dict):
            raise ValueError("a proxy entry is not a mapping")
        delegate = _address_of(entry.get("delegate"))
        proxy_type = _type_name(entry.get("proxy_type"))
        delay = entry.get("delay")
        if delegate is None or proxy_type is None or delay is None:
            raise ValueError("a proxy entry lacks delegate, proxy_type or delay")
        out.append({"delegate": delegate, "proxy_type": proxy_type, "delay": int(_unwrap(delay))})
    return out


def delegation_problems(client, settings):
    """Read ``Proxy.Proxies(commit identity)``: the proxy signer must be present with the
    required type and delay 0. Returns a list of problem strings (empty means fine). A read
    that fails or cannot be understood is a problem: it fails closed."""
    commit = commit_address(settings, None)
    signer = settings.proxy_signer_address
    try:
        raw = client.query(("Proxy", "Proxies"), [commit])
        proxies = parse_proxies(raw)
    except Exception as error:  # noqa: BLE001 - reported, not raised
        return [
            f"could not read the delegation Proxy.Proxies of the commit identity "
            f"({type(error).__name__}; the error text is not shown); refusing rather than guessing."
        ]
    mine = [p for p in proxies if _same(p["delegate"], signer)]
    if not mine:
        return [
            f"the chain shows NO proxy delegation from the commit identity to the "
            f"configured signer "
            f"({len(proxies)} other delegate(s) exist). The send would fail and cost a fee."
        ]
    problems = []
    if not any(p["proxy_type"] == REQUIRED_PROXY_TYPE and p["delay"] == REQUIRED_DELAY for p in mine):
        seen = ", ".join(f"{p['proxy_type']} delay {p['delay']}" for p in mine)
        problems.append(
            f"the delegation to the configured proxy signer is {seen}, not "
            f"{REQUIRED_PROXY_TYPE} with delay "
            f"{REQUIRED_DELAY}."
        )
    return problems


def signer_free_balance_rao(client, address):
    account = _unwrap(client.query(("System", "Account"), [address]))
    data = account["data"] if isinstance(account, dict) else None
    free = _unwrap(data["free"]) if isinstance(data, dict) else None
    if free is None:
        raise ValueError("System.Account carried no free balance")
    return int(free)


def balance_problem(client, settings, *, floor_rao=MIN_SIGNER_BALANCE_RAO):
    signer = settings.proxy_signer_address
    try:
        free = signer_free_balance_rao(client, signer)
    except Exception as error:  # noqa: BLE001
        return (
            f"could not read the proxy signer's balance ({type(error).__name__}; "
            f"the error text is not shown); "
            f"refusing rather than guessing."
        )
    if free < floor_rao:
        return (
            f"the configured proxy signer holds {free} rao, below the floor of {floor_rao} rao "
            f"that must cover fees."
        )
    return None


def gate_lines(reader, settings):
    """Two extra startup questions in proxy mode, as ``(label, outcome)`` pairs in the same
    "yes" / "NO. ..." shape the validator's gate prints. Empty when the proxy route is off."""
    if not proxy_mode(settings):
        return []
    client = reader.client
    lines = []
    problems = delegation_problems(client, settings)
    lines.append(
        ("proxy delegation", "yes" if not problems else "NO. " + " ".join(problems))
    )
    problem = balance_problem(client, settings)
    lines.append(("proxy signer funded", "yes" if problem is None else f"NO. {problem}"))
    return lines


# ---------------------------------------------------------------------------
# The INNER result of a proxied call
# ---------------------------------------------------------------------------

INNER_OK = "inner_ok"
INNER_ERR = "inner_err"
INNER_UNKNOWN = "inner_unknown"


def _events_of(events):
    for entry in events or []:
        record = _unwrap(entry)
        if not isinstance(record, dict):
            continue
        event = record.get("event", record)
        if not isinstance(event, dict):
            continue
        module = event.get("module_id")
        name = event.get("event_id")
        if isinstance(module, str) and isinstance(name, str):
            attributes = event.get("attributes")
            yield module, name, attributes


def classify_inner(events):
    """What happened INSIDE the proxy wrapper, from the extrinsic's own events.

    ``(INNER_OK, None)`` only when ``Proxy.ProxyExecuted`` is present with an Ok result.
    ``(INNER_ERR, <dispatch error>)`` when it carries an Err (the outer extrinsic is
    included and charged; the commit did NOT happen) or when ``System.ExtrinsicFailed``
    is present. ``(INNER_UNKNOWN, None)`` when there is no ``ProxyExecuted`` at all:
    events not returned, or not understood. Never "ok" by default."""
    failed = None
    executed = None
    for module, name, attributes in _events_of(events):
        if (module, name) == ("System", "ExtrinsicFailed"):
            failed = attributes
        elif (module, name) == ("Proxy", "ProxyExecuted"):
            result = attributes.get("result") if isinstance(attributes, dict) else attributes
            executed = result
    if failed is not None:
        return INNER_ERR, failed
    if executed is None:
        return INNER_UNKNOWN, None
    result = _unwrap(executed)
    if isinstance(result, dict) and "Err" in result:
        return INNER_ERR, result["Err"]
    if isinstance(result, dict) and "Ok" in result:
        return INNER_OK, None
    if result == "Ok":
        return INNER_OK, None
    return INNER_UNKNOWN, None


# ---------------------------------------------------------------------------
# Bounded failure / fee backoff
# ---------------------------------------------------------------------------

BACKOFF_REASON_PREFIX = "PUBLICATION FAILURE LIMIT"
# Documented defaults. Three consecutive failed sends (each may have cost the proxy signer
# a fee) stop sending until a person resets it; between failures a minimum spacing doubles.
MAX_CONSECUTIVE_FAILURES = 3
BASE_SPACING_SECONDS = 1800  # 30 min, then 60 min


class PublicationFailureLimit(PublicationStatusUnknown):
    """Sends are paused after repeated failures, or are still inside the spacing window.
    Nothing is sent. A subclass of the 'unknown' stop so the loop waits with this reason
    and the outcome alert goes red; a person resets it (``scripts/publication_backoff.py``)."""


def _now():
    return datetime.now(timezone.utc)


class PublishFailureBackoff:
    """A persisted count of consecutive failed sends. Fail-closed on a corrupt file."""

    def __init__(self, path, *, max_failures=MAX_CONSECUTIVE_FAILURES, base_spacing=BASE_SPACING_SECONDS, clock=None):
        self.path = Path(path)
        self.max_failures = max_failures
        self.base_spacing = base_spacing
        self._clock = clock or _now

    def load(self):
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            return {"corrupt": f"could not be read ({type(error).__name__})"}
        try:
            doc = json.loads(text)
            if not isinstance(doc, dict) or type(doc.get("failures")) is not int:
                raise ValueError("no integer failures")
        except ValueError:
            return {"corrupt": "is not a valid backoff record"}
        return doc

    def raise_if_blocked(self):
        doc = self.load()
        if doc is None:
            return
        if "corrupt" in doc:
            raise PublicationFailureLimit(
                f"{BACKOFF_REASON_PREFIX}: the failure record {plain_path_text(self.path)} "
                f"{doc['corrupt']}; nothing is "
                f"sent until a person looks at it (scripts/publication_backoff.py)."
            )
        if doc["failures"] >= self.max_failures:
            raise PublicationFailureLimit(
                f"{BACKOFF_REASON_PREFIX}: {doc['failures']} consecutive failed sends (last "
                f"{doc.get('last_failure_at', 'at an unknown time')}). Each may have cost the "
                f"proxy signer a fee. Nothing more is sent until a person reads the failure and "
                f"resets it with scripts/publication_backoff.py."
            )
        until = doc.get("not_before")
        if until:
            if datetime.fromisoformat(until) > self._clock():
                raise PublicationFailureLimit(
                    f"{BACKOFF_REASON_PREFIX}: {doc['failures']} failed send(s); the next attempt "
                    f"is not allowed before {until}. Nothing is sent now."
                )

    def record_failure(self, reason):
        doc = self.load()
        failures = (doc["failures"] if doc and "corrupt" not in doc else 0) + 1
        now = self._clock()
        spacing = self.base_spacing * (2 ** (failures - 1))
        from datetime import timedelta

        payload = {
            "schema_version": 1,
            "failures": failures,
            "last_failure_at": now.isoformat(),
            "last_reason": str(reason)[:400],
            "not_before": (now + timedelta(seconds=spacing)).isoformat(),
        }
        self._write(payload)
        return payload

    def record_success(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def reset(self, evidence_note):
        note = (evidence_note or "").strip()
        if not note:
            raise ValueError("resetting the failure limit needs a non-empty evidence note")
        doc = self.load()
        if doc is None:
            raise FileNotFoundError(f"no failure record at {plain_path_text(self.path)}")
        stamp = self._clock().strftime("%Y%m%dT%H%M%SZ")
        archive = self.path.with_name(f"{self.path.stem}.reset-{stamp}{self.path.suffix}")
        archive.write_text(json.dumps({**doc, "reset_at": self._clock().isoformat(), "evidence_note": note}, sort_keys=True))
        self.path.unlink()
        return archive

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}.{threading.get_ident()}")
        with open(tmp, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
