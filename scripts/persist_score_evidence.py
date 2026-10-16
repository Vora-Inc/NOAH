"""Persist scoring output and the evidence used to inspect it.

Real and fixture outputs use separate directories. Set ``NOAH_RUNTIME_ROOT``
to durable storage outside the source checkout; the legacy fallback derives
a location from the checkout's directory depth. Preserve evidence across
upgrades and verify the configured path before starting a publisher.

Canonical score filenames are ``day-<evalDay>.json``. The canonical writer checks
``evalDay`` and provides explicit conflict handling. A saved score proves
only the calculation recorded there, not a finalized weight publication.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SOFTWARE_NOAH_ROOT = Path(__file__).resolve().parents[1]
if str(_SOFTWARE_NOAH_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_NOAH_ROOT))

from noah.pathtext import plain_path_text  # noqa: E402
from noah.core.calculation_record import durable_write  # noqa: E402
from noah.scoring.fixture_io import render_output  # noqa: E402

# Legacy default root is derived from the source checkout. Configure
# NOAH_RUNTIME_ROOT explicitly for an installed validator.
_REPO_ROOT = _SOFTWARE_NOAH_ROOT.parent.parent

_RUNTIME_ROOT_OVERRIDE = os.environ.get("NOAH_RUNTIME_ROOT", "").strip()
RUNTIME_ROOT = (
    Path(_RUNTIME_ROOT_OVERRIDE) if _RUNTIME_ROOT_OVERRIDE else _REPO_ROOT.parent.parent / "runtime"
)

# Preserve these directory names for compatibility with existing evidence.
CANONICAL_SCORES_DIR = RUNTIME_ROOT / "ticket-16" / "scores"

# Synthetic results live in a subdirectory so readers listing canonical files
# cannot accidentally treat a fixture result as evidence of live operation.
FIXTURE_SCORES_DIR = CANONICAL_SCORES_DIR / "fixture"


def _with_chain_identity(output: dict, *, netuid=None, ark_address=None, genesis_hash=None) -> dict:
    """Return a copy of ``output`` with real chain-identity fields merged
    in as flat top-level keys, when given -- never mutates the caller's own
    dict, and never invents a value: each of ``netuid``/``ark_address``/
    ``genesis_hash`` is included only when the caller actually passed one.

    Called by both writers below, so a plain ``write_score_evidence(output,
    path)`` call with none of these (existing callers, existing tests)
    writes ``output`` completely unchanged -- this is purely additive.
    """
    if netuid is None and ark_address is None and genesis_hash is None:
        return output
    enriched = dict(output)
    if netuid is not None:
        enriched["netuid"] = netuid
    if ark_address is not None:
        enriched["ark_address"] = ark_address
    if genesis_hash is not None:
        enriched["genesis_hash"] = genesis_hash
    return enriched


def write_score_evidence(
    output: dict, out_path, *, netuid=None, ark_address=None, genesis_hash=None
) -> Path:
    """Write ``output`` (a ``noah-score-output/6`` dict) to ``out_path``.

    Serializes with ``noah.scoring.fixture_io.render_output`` -- the exact
    canonical form (sorted keys, two-space indent, trailing newline) the
    twin proof already compares byte-for-byte -- so a file this writes is
    directly comparable to any other renderer of the same dict. Creates
    parent directories if they do not exist yet. Returns the path written,
    as a ``Path``.

    ``netuid``/``ark_address``/``genesis_hash`` (issue #112, traced by #12):
    when given, merged into the written dict as real chain identity (see
    this module's own top docstring) via ``_with_chain_identity`` -- the
    caller's own ``output`` dict is never mutated. Omitted (the existing
    default), this writes ``output`` exactly as before, byte-for-byte.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output = _with_chain_identity(
        output, netuid=netuid, ark_address=ark_address, genesis_hash=genesis_hash
    )
    durable_write(out_path, render_output(output).encode("utf-8"), replace=True)
    return out_path


def canonical_score_evidence_path(eval_day) -> Path:
    """The canonical ``day-<evalDay>.json`` path
    ``score-file consumers``'s
    ``_REAL_SCORE_DAY_FILE_RE`` (``day-(\\d+)\\.json``, ``fullmatch``) looks
    for inside ``CANONICAL_SCORES_DIR``.

    ``eval_day`` must be the exact value already flowing out of a real
    ``noah-score-output/6`` dict's own ``evalDay`` field (a plain decimal
    string -- ``noah.scoring.engine.score_fixture`` always writes
    ``str(fx.eval_day)``) -- never re-derived from a chain height or
    anything else, so the filename and the JSON's own ``evalDay`` field
    can never independently drift apart. Refuses (``ValueError``) rather
    than writing a filename the reader's own regex would silently never
    match, on anything that is not a plain non-negative decimal integer.
    """
    eval_day_str = str(eval_day)
    if not eval_day_str.isdigit():
        raise ValueError(
            f"cannot build a canonical score-evidence filename from evalDay {eval_day!r}: "
            "expected a plain non-negative decimal integer string, exactly what "
            "noah.scoring.engine.score_fixture's own 'evalDay' field always contains -- a "
            "different shape here would silently never match "
            "the canonical score filename pattern ('day-(\\d+)\\.json')."
        )
    return CANONICAL_SCORES_DIR / f"day-{eval_day_str}.json"


def fixture_score_evidence_path(eval_day) -> Path:
    """The isolated fixture/synthetic-data counterpart of
    ``canonical_score_evidence_path``: the identical ``day-<evalDay>.json``
    filename convention and the identical digit-only validation, but rooted
    under ``FIXTURE_SCORES_DIR`` (a subdirectory of ``CANONICAL_SCORES_DIR``)
    -- never ``CANONICAL_SCORES_DIR`` itself. See ``FIXTURE_SCORES_DIR``'s
    own comment for why a subdirectory, not just a differently-named file,
    is the real isolation a fixture run needs.
    """
    eval_day_str = str(eval_day)
    if not eval_day_str.isdigit():
        raise ValueError(
            f"cannot build a fixture score-evidence filename from evalDay {eval_day!r}: "
            "expected a plain non-negative decimal integer string -- see "
            "canonical_score_evidence_path's own identical check."
        )
    return FIXTURE_SCORES_DIR / f"day-{eval_day_str}.json"


class CanonicalEvidenceConflict(Exception):
    """Raised by :func:`write_canonical_score_evidence` when
    ``on_conflict="refuse"`` and a previously written canonical result for
    the same day differs from this run's real computed result.

    Nothing is written when this is raised -- the existing canonical file
    is left completely untouched, unlike the default ``on_conflict=
    "overwrite"`` behavior (see that function's own docstring). This is
    opt-in per call via ``on_conflict``.
    """


def write_canonical_score_evidence(
    output: dict,
    *,
    on_conflict: str = "overwrite",
    netuid=None,
    ark_address=None,
    genesis_hash=None,
) -> dict:
    """Write ``output`` (a real ``noah-score-output/6`` dict) to the
    canonical ``day-<evalDay>.json`` path, IN ADDITION to whatever
    other location a caller also writes to by hand (``write_score_evidence``
    above, unchanged).

    ``netuid``/``ark_address``/``genesis_hash`` (issue #112, traced by #12):
    when given, merged into the written dict as real chain identity (see
    this module's own top docstring), identically to ``write_score_evidence``
    above -- including for the conflict comparison below, which compares
    against this same enriched text, not the caller's raw ``output``.

    Recomputing/re-deriving a day's score is expected and fine -- a
    deliberate re-score of an already-closed day is supported --
    but this deliberately never silently discards a different prior result
    for the same day. What happens when a real (non-byte-identical)
    difference is found is controlled by ``on_conflict``:

    - ``"overwrite"`` (the default, unchanged from this function's
      original behavior): prints a loud, explicit line naming the day and
      the path, then writes over the prior file. Meant for a human caller
      (``day_close_pipeline.py``'s own default CLI behavior) who reads
      that printed line and can act on it.
    - ``"refuse"``: raises :class:`CanonicalEvidenceConflict` instead of
      writing anything -- the existing file is left completely untouched.
      Meant for an unattended, automatic caller
      where nobody is watching stdout for the loud line above when it
      fires, so a real difference must stop the write outright rather than
      just being announced.

    Byte-identical content (a rerun that recomputed the exact same result)
    is always a silent no-op regardless of ``on_conflict`` -- there is
    nothing to overwrite or refuse in that case, and both modes already
    agree nothing changed.

    Returns ``{"path": <str>, "eval_day": <str>, "action": "written" |
    "overwritten" | "unchanged"}``. (``on_conflict="refuse"`` never
    returns ``"overwritten"`` -- that path raises instead.)
    """
    if on_conflict not in ("overwrite", "refuse"):
        raise ValueError(f"on_conflict must be 'overwrite' or 'refuse', got {on_conflict!r}")

    eval_day = output.get("evalDay")
    path = canonical_score_evidence_path(eval_day)
    output = _with_chain_identity(
        output, netuid=netuid, ark_address=ark_address, genesis_hash=genesis_hash
    )
    new_text = render_output(output)

    if path.exists():
        old_text = path.read_text(encoding="utf-8")
        if old_text == new_text:
            print(
                f"Canonical score evidence for day {eval_day} at {plain_path_text(path)} already "
                f"present "
                "and identical to this run's result -- nothing to overwrite."
            )
            return {"path": str(path), "eval_day": eval_day, "action": "unchanged"}
        if on_conflict == "refuse":
            raise CanonicalEvidenceConflict(
                f"canonical score evidence for day {eval_day} at {plain_path_text(path)} already "
                f"exists and "
                "differs from this run's real computed result. Refusing to overwrite it "
                "automatically (on_conflict='refuse') -- the existing file was left "
                "completely untouched. A human needs to look at this before any automated "
                f"evidence is trusted for day {eval_day}."
            )
        print(
            f"OVERWRITING CANONICAL SCORE EVIDENCE for day {eval_day} at {plain_path_text(path)}: "
            f"a "
            "previously written result for this exact day differs from this run's real "
            "computed result. This is a deliberate recompute being made visible, not a "
            "silent clobber -- the prior content is being replaced now."
        )
        durable_write(path, new_text.encode("utf-8"), replace=True)
        return {"path": str(path), "eval_day": eval_day, "action": "overwritten"}

    path.parent.mkdir(parents=True, exist_ok=True)
    durable_write(path, new_text.encode("utf-8"))
    print(f"Canonical score evidence written for day {eval_day} at {plain_path_text(path)}")
    return {"path": str(path), "eval_day": eval_day, "action": "written"}


def main(argv: list[str] | None = None) -> int:
    from noah.chain.sources import guard_endpoint_logging

    guard_endpoint_logging()
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(
            "usage: python3 persist_score_evidence.py <score-output.json|-> <out_path>",
            file=sys.stderr,
        )
        return 2
    source, out_path = argv
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        output = json.loads(text)
        path = write_score_evidence(output, out_path)
    except (OSError, ValueError) as error:
        # Their text repeats the typed paths.
        print(
            f"cannot read the score output or write the evidence ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2
    print(plain_path_text(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
