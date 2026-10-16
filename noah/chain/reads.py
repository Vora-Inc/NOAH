"""How a read is performed, and what has to be true before its answer counts."""

import json
from dataclasses import dataclass, field

from .sources import Refusal, endpoint_label


class NotAHeight(Refusal):
    """A block height that is not a plain, positive, already-decoded integer."""


class NotAFieldName(Refusal):
    """A field or source name that is not usable text."""


class HeadWentBackwards(Refusal):
    """A read of a block the chain has not produced yet."""


class ControlKeyMissing(Refusal):
    """A read whose method could not find something known to be there."""


class NotDecoded(Refusal):
    """A value still wearing its wire format on a path that decides a payment."""


class ReadKindChanged(Refusal):
    """One block recorded both as the head and as history."""


def _height(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAHeight(f"{what} came back as {value!r}, which is not a decoded integer.")
    if value <= 0:
        raise NotAHeight(f"{what} must be a positive block height, got {value}.")
    return value


def _decoded_bool(value, what):
    """A real boolean, or a refusal.

    Checked by type rather than by truth. Every wire form of "missing" is a
    non-empty string, and a non-empty string is true, so a truthiness test here
    blesses exactly the answer it exists to refuse. The same discipline is
    written down in this package already, in the agreement gate: a value that
    arrives still wearing its wire format has not been decoded, and guessing
    what it meant is the failure being guarded against.
    """
    if not isinstance(value, bool):
        raise NotDecoded(
            f"{what} came back as {value!r}, which is not a decoded true or false. Every "
            f"wire form of 'missing' is a non-empty string and therefore true, so this is "
            f"checked by type and not by truth."
        )
    return value


def _name(value, what):
    if not isinstance(value, str) or not value.strip():
        raise NotAFieldName(f"{what} must be a non-empty name, got {value!r}.")
    return value


@dataclass(frozen=True)
class ReadContext:
    """Where a read sits relative to the chain's head, and what that requires.

    Takes no tolerance and never will. See the module docstring: an allowance
    here is the depth heuristic the written rules forbid, and it would arrive
    looking like a kindness.
    """

    at_block: int
    head_block: int

    def __post_init__(self):
        _height(self.at_block, "the block being read")
        _height(self.head_block, "the chain head")
        if self.at_block > self.head_block:
            raise HeadWentBackwards(
                f"a read was asked for block {self.at_block} while the head is "
                f"{self.head_block}. Either the head we compared against is stale or the "
                f"source is answering about a different chain, and neither can be read past."
            )

    @property
    def kind(self):
        return "head" if self.at_block == self.head_block else "catch-up"

    @property
    def needs_agreement(self):
        """Whether independent sources must agree before this answer is used.

        Every catch-up read does. Only a read at the head is exempt, and only
        because it is not history yet.
        """
        return self.kind == "catch-up"


@dataclass(frozen=True)
class KeyRead:
    """One storage key asked for, and whether the method still works.

    ``control_present`` is whether the same method, in the same call, found a
    key known to exist. Carried alongside rather than checked separately so a
    caller cannot report an absence without having established that its
    question was well formed.

    Prefer ``from_probe``, which derives both flags from the raw answers rather
    than taking them on trust. A hand-set boolean is a claim; a raw answer is a
    measurement.
    """

    field: str
    block: int
    present: bool
    control_present: bool

    @classmethod
    def from_probe(cls, *, field, block, raw, control_raw):
        """Build a read from the two raw storage answers.

        A raw storage read is either the stored value or nothing at all, so
        presence is derived here rather than asserted by a caller who might
        derive it differently. ``control_raw`` is the answer for a key known to
        exist, asked through the same method in the same call.
        """
        return cls(
            field=field,
            block=block,
            present=raw is not None,
            control_present=control_raw is not None,
        )


def confirmed_absence(read):
    """Whether this key is genuinely absent, or a refusal.

    Refuses whenever the control key was missing, whatever this key said. If
    the method cannot find what is known to be there, nothing it reports about
    anything else is worth having, including a value.
    """
    if not isinstance(read, KeyRead):
        raise NotDecoded(
            f"a read came in as {read!r}, which is not a key read. An absence has to carry "
            f"what was asked, where, and whether the method still finds what exists."
        )
    _name(read.field, "the field being read")
    _height(read.block, "the block being read")
    _decoded_bool(read.present, f"whether {read.field} was present at block {read.block}")
    _decoded_bool(
        read.control_present, f"whether the control key was present at block {read.block}"
    )
    if not read.control_present:
        raise ControlKeyMissing(
            f"reading {read.field} at block {read.block}: the control key, which is known to "
            f"exist, came back missing from the same method in the same call. A key built "
            f"with too few parts is accepted and answers 'not there' without complaining, so "
            f"a broken question and a real absence are the same bytes. Nothing this method "
            f"said about this block is used."
        )
    return not read.present


@dataclass
class Provenance:
    """What a run read, from where, at what heights, and what it was handed.

    Sorted on the way out rather than kept in arrival order, because two
    independent implementations have to produce identical bytes and nothing may
    depend on the order a dictionary happened to be built in.
    """

    sources: tuple
    _reads: list = field(default_factory=list)
    _configured: dict = field(default_factory=dict)

    def __repr__(self):
        # Labels only: a source's endpoint is a URL that may carry an access key.
        shown = (
            {n: endpoint_label(e) for n, e in self.sources.items()}
            if isinstance(self.sources, dict)
            else type(self.sources).__name__
        )
        return f"Provenance(sources={shown!r}, reads={len(self._reads)})"

    def __post_init__(self):
        # A mapping and nothing else. A bare string is iterable, so it used to
        # pass this one character at a time and record seven sources from one
        # dropped comma. A generator is truthy, so it used to pass and then be
        # consumed, leaving a record naming no sources at all.
        if not isinstance(self.sources, dict) or not self.sources:
            # Shown by label only: a value here is an endpoint URL that may carry a key.
            shown = (
                {n: endpoint_label(e) for n, e in self.sources.items()}
                if isinstance(self.sources, dict)
                else type(self.sources).__name__
            )
            raise NotAFieldName(
                f"a run has to say which endpoint each of its sources reads from, as a "
                f"mapping of name to endpoint, and it has to have at least one. Got "
                f"{shown!r}. A record that names none cannot be checked by anybody, "
                f"and a record that names only labels cannot show they were independent."
            )
        for name, endpoint in self.sources.items():
            _name(name, "a source name")
            _name(endpoint, f"the endpoint behind source {name!r}")

    def read(self, field_name, context):
        """Record one read of one field, and whether it was at the head."""
        _name(field_name, "the field being read")
        if type(context) is not ReadContext:
            # Exact type, not isinstance. A subclass can shadow the kind with a
            # plain class attribute and record a block millions behind the head
            # as a head read, and nothing about the object would look wrong.
            raise NotAFieldName(
                f"a read of {field_name} was recorded without saying where it sat relative "
                f"to the head, which is the thing this record exists to preserve."
            )
        entry = (context.at_block, field_name, context.kind)
        for at_block, name, kind in self._reads:
            if (at_block, name) == (context.at_block, field_name) and kind != context.kind:
                raise ReadKindChanged(
                    f"{field_name} at block {context.at_block} was already recorded as a "
                    f"{kind} read and is now being recorded as a {context.kind} read. A block "
                    f"is the head exactly once, so a record holding both is claiming the "
                    f"agreement exemption applied to a block that is now history."
                )
        self._reads.append(entry)

    def configured(self, field_name, where_from):
        """Record one field that was handed in rather than read from a chain."""
        _name(field_name, "the field being configured")
        _name(where_from, "where the configured value came from")
        self._configured[field_name] = where_from

    def as_dict(self):
        """The record, in a form that is the same bytes on any machine.

        ``single_source`` is stated rather than left to be inferred from the
        length of the source list, because two names for one endpoint is one
        source and agreement with itself is not agreement.
        """
        reads = sorted(set(self._reads))
        return {
            # The label, never the URL: a keyed endpoint must not reach a stored record.
            "sources": [
                {"name": n, "endpoint": endpoint_label(e)} for n, e in sorted(self.sources.items())
            ],
            # Distinct endpoints, not distinct labels. Two names for one node is
            # one source, and agreement with itself is not agreement.
            "single_source": len(set(self.sources.values())) < 2,
            "reads": [{"field": f, "block": b, "kind": k} for b, f, k in reads],
            "blocks_read": sorted({b for b, _f, _k in reads}),
            "read_fields": sorted({f for _b, f, _k in reads}),
            "configured_fields": dict(sorted(self._configured.items())),
        }

    def as_json(self):
        """The record as bytes, in the canonical form this project compares.

        Sorted keys, two-space indent, trailing newline, matching the fixture
        serialisation the two scoring implementations are held to. A dictionary
        is not bytes, and comparing dictionaries ignores key order entirely, so
        a claim about identical bytes needs a serialiser to be a claim at all.
        """
        return json.dumps(self.as_dict(), sort_keys=True, indent=2) + "\n"
