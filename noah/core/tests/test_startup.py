"""What must be true before the validator starts, and what each failure says.

Every check here refuses by name and says what to do about it. That matters more
than usual for this component: a validator that cannot publish looks identical
from outside whether it is broken, misconfigured, or simply too new to be
allowed yet. The first two need fixing and the third needs waiting, and telling
them apart at three in the morning is the whole job of these messages.
"""

import ast
import re
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest import mock

from .. import startup
from ..reader import ChainUnreadable
from ..startup import (
    CannotPublishYet,
    DirectPathMisconfigured,
    NotArchiveCapable,
    NotRegistered,
    NoValidatorPermit,
    SealedPathUnavailable,
    UnsupportedLibraryVersion,
    check_can_publish_yet,
    check_commit_reveal_enabled,
    check_has_validator_permit,
    check_library_versions,
    check_reads_history,
    check_registered,
)

# Shared by every test class below that reads a REAL pyproject.toml through a
# REAL tomllib -- either this project's actual one, or a fake, injected copy
# via `mock.patch("noah.core.startup._PYPROJECT", ...)`. Both need an
# interpreter that actually has `tomllib` (3.11+); on one that does not, the
# code under test correctly and silently falls back to `_FALLBACK_SPECIFIERS`
# regardless of what `_PYPROJECT` is patched to, so these classes would not
# crash on Python 3.10 -- they would just fail on an assertion that assumed a
# real parse happened. `.github/workflows/ci.yml`'s 3.10 job is a real,
# supported matrix entry (`requires-python = ">=3.10"`), not a hypothetical:
# found by codex adversarial review (2026-08-29) that an earlier version of
# one of these classes did an unguarded `import tomllib` and broke that job
# outright (a collection-time `ModuleNotFoundError`) rather than skipping
# cleanly; auditing the other classes with the same "patch _PYPROJECT and
# expect a real read" shape here found two more that would fail the same way,
# just without crashing collection. The 3.11/3.12/3.13 CI matrix jobs still
# run and enforce every one of these classes every time.
_needs_real_tomllib = unittest.skipUnless(
    startup._tomllib() is not None,
    "no TOML parser on this interpreter (tomllib is 3.11+); see the module-level "
    "comment above _needs_real_tomllib for why this class cannot run here.",
)


class Chain:
    """A stand-in answering only what each check asks of it."""

    def __init__(self, **answers):
        self._answers = answers

    def __getattr__(self, name):
        if name not in self._answers:
            raise AttributeError(name)
        value = self._answers[name]
        if isinstance(value, Exception):

            def raiser(*_a, **_k):
                raise value

            return raiser
        return lambda *_a, **_k: value


class Registration(unittest.TestCase):
    def test_a_registered_hotkey_passes_and_gives_back_its_seat(self):
        chain = Chain(seat_of=7)
        self.assertEqual(check_registered(chain, "5HOT", netuid=2), 7)

    def test_seat_zero_is_a_real_seat_and_not_an_absence(self):
        # Zero is a valid uid. Treating it as falsy would refuse a registered
        # validator holding the first seat, which is the one the canonical
        # validator is most likely to hold.
        self.assertEqual(check_registered(Chain(seat_of=0), "5HOT", netuid=2), 0)

    def test_an_unregistered_hotkey_is_refused_by_name(self):
        with self.assertRaises(NotRegistered) as caught:
            check_registered(Chain(seat_of=None), "5HOT", netuid=2)
        self.assertIn("5HOT", str(caught.exception))

    def test_the_refusal_says_the_subnet_it_looked_on(self):
        with self.assertRaises(NotRegistered) as caught:
            check_registered(Chain(seat_of=None), "5HOT", netuid=44)
        self.assertIn("44", str(caught.exception))


class TheBlackout(unittest.TestCase):
    """The finding this component exists for.

    A newly registered validator cannot publish for its first hundred blocks,
    and that clock starts at REGISTRATION rather than at first attempt. On the
    live network that is a twenty-minute blackout at startup, and a validator
    that does not know this looks broken during it.
    """

    def test_a_validator_past_its_blackout_may_publish(self):
        chain = Chain(current_block=1000, last_update=800, rate_limit=100)
        self.assertEqual(check_can_publish_yet(chain, uid=1, netuid=2), 0)

    def test_a_freshly_registered_validator_is_told_to_wait_not_that_it_is_broken(self):
        chain = Chain(current_block=850, last_update=800, rate_limit=100)
        with self.assertRaises(CannotPublishYet) as caught:
            check_can_publish_yet(chain, uid=1, netuid=2)
        self.assertIn("50", str(caught.exception))

    def test_the_refusal_says_it_is_waiting_rather_than_failing(self):
        chain = Chain(current_block=850, last_update=800, rate_limit=100)
        with self.assertRaises(CannotPublishYet) as caught:
            check_can_publish_yet(chain, uid=1, netuid=2)
        message = str(caught.exception).lower()
        self.assertIn("wait", message)
        self.assertIn("not a fault", message)

    def test_the_exact_boundary_block_is_allowed(self):
        # Off by one here is twelve seconds of a validator refusing to work for
        # no reason, every time it restarts.
        chain = Chain(current_block=900, last_update=800, rate_limit=100)
        self.assertEqual(check_can_publish_yet(chain, uid=1, netuid=2), 0)

    def test_one_block_short_of_the_boundary_is_refused(self):
        chain = Chain(current_block=899, last_update=800, rate_limit=100)
        with self.assertRaises(CannotPublishYet):
            check_can_publish_yet(chain, uid=1, netuid=2)


class ReadingHistory(unittest.TestCase):
    """Scoring replays days, so a node that only keeps recent state is useless.

    The failure this prevents is slow and quiet: everything works for days, and
    then the first score that reaches past the node's horizon is wrong rather
    than refused, because a pruned node answers a historical read with a
    plausible zero rather than an error.
    """

    def test_a_node_that_answers_a_deep_read_passes(self):
        self.assertIsNone(check_reads_history(Chain(historical_probe="0xabc"), depth=1000))

    def test_a_node_that_answers_nothing_at_depth_is_refused(self):
        for answer in (None, "", 0):
            with self.subTest(answer=answer):
                with self.assertRaises(NotArchiveCapable):
                    check_reads_history(Chain(historical_probe=answer), depth=1000)

    def test_a_node_that_raises_at_depth_is_refused_too(self):
        # ChainUnreadable, not a bare RuntimeError: after #510
        # (noah/core/reader.py), ChainReader.historical_probe only ever
        # raises ChainUnreadable for a genuine failure -- anything else it
        # raises is a bug, and check_reads_history's own except clause
        # (below) now matches that exactly, catching ChainUnreadable
        # specifically rather than Exception. A bare RuntimeError here would
        # no longer be an honest stand-in for what a real failure looks
        # like reaching this function.
        chain = Chain(historical_probe=ChainUnreadable("state discarded"))
        with self.assertRaises(NotArchiveCapable):
            check_reads_history(chain, depth=1000)

    def test_the_refusal_says_how_deep_it_probed(self):
        with self.assertRaises(NotArchiveCapable) as caught:
            check_reads_history(Chain(historical_probe=None), depth=4321)
        self.assertIn("4321", str(caught.exception))


class TheHistoryProbeIsActuallyWiredToARealReader(unittest.TestCase):
    """Neither test_reader.py nor the class above alone proves this composes.

    test_reader.py proves ChainReader.historical_probe returns the query's
    real answer. The class above proves check_reads_history refuses a falsy
    answer. This drives a real ChainReader through check_reads_history the
    way a live validator actually would, which is the only thing that proves
    the fix in reader.py actually reaches this check rather than the two
    files each independently testing half of a gap that only shows up when
    they are composed.
    """

    class Client:
        def __init__(self, block, tempo_at_depth):
            self.block = block
            self._tempo = tempo_at_depth

        def query(self, item, params=None, block=None):
            assert block is not None, "current_block must read .block, not query"
            return self._tempo

    def test_a_pruned_node_that_answers_none_without_raising_is_refused(self):
        from ..reader import ChainReader

        client = self.Client(block=8_000, tempo_at_depth=None)
        with self.assertRaises(NotArchiveCapable):
            check_reads_history(ChainReader(client), depth=300)

    def test_a_pruned_node_that_answers_a_default_zero_is_refused(self):
        from ..reader import ChainReader

        client = self.Client(block=8_000, tempo_at_depth=0)
        with self.assertRaises(NotArchiveCapable):
            check_reads_history(ChainReader(client), depth=300)

    def test_a_node_that_answers_for_real_passes(self):
        from ..reader import ChainReader

        client = self.Client(block=8_000, tempo_at_depth=20)
        self.assertIsNone(check_reads_history(ChainReader(client), depth=300))

    def test_a_bug_in_the_probe_crashes_here_too_not_just_inside_reader_py(self):
        """The composition gap #510 left one layer up, found by re-reading
        this exact file after fixing reader.py.

        reader.py's own fix makes ChainReader.historical_probe let a real
        code bug propagate instead of silently reading as "cannot serve
        history". That fix is pointless if THIS function -- the only real
        caller historical_probe has -- catches it right back with its own
        bare `except Exception` and reports it as the same clean
        NotArchiveCapable refusal #510 is about in the first place. Proven
        here through a REAL ChainReader, the same way the rest of this
        class proves the probe itself composes: a fake client that raises
        a plain TypeError from query() must reach the caller of
        check_reads_history as a TypeError, not as a refusal that reads
        like the node was merely too small.
        """
        from ..reader import ChainReader

        class BuggyClient:
            block = 8_000

            def query(self, item, params=None, block=None):
                raise TypeError("historical_probe called the client wrong")

        with self.assertRaises(TypeError):
            check_reads_history(ChainReader(BuggyClient()), depth=300)


class TheSealedPath(unittest.TestCase):
    def test_commit_reveal_enabled_passes(self):
        self.assertIsNone(check_commit_reveal_enabled(Chain(commit_reveal=True), netuid=2))

    def test_commit_reveal_disabled_is_refused(self):
        # The design requires the sealed path. A subnet with it switched off
        # would silently accept weights in the open instead.
        with self.assertRaises(SealedPathUnavailable):
            check_commit_reveal_enabled(Chain(commit_reveal=False), netuid=2)

    def test_an_unreadable_answer_is_refused_rather_than_assumed_on(self):
        with self.assertRaises(SealedPathUnavailable):
            check_commit_reveal_enabled(Chain(commit_reveal=None), netuid=2)

    def test_a_truthy_non_true_answer_from_a_real_reader_is_refused(self):
        """The same composition gap as the history probe above, for #454's P2.

        ChainReader.commit_reveal used to coerce with bool(...), which turns
        any truthy-but-undecoded answer into a real True and makes this
        check unable to ever refuse it. This drives a real ChainReader
        through the check to prove the two compose correctly now.
        """
        from ..reader import ChainReader

        class Client:
            block = 8_000

            def query(self, item, params=None, block=None):
                return "false"  # truthy in Python, and not a decoded True

        with self.assertRaises(SealedPathUnavailable):
            check_commit_reveal_enabled(ChainReader(Client()), netuid=2)


class TheSealedPathByPublishMode(unittest.TestCase):
    """`check_commit_reveal_enabled` asks a different question per `publish_mode`.

    `publish_mode` defaults to "sealed", the only behavior this function had
    before direct publishing existed, so a caller that never passes it -- every
    caller in this file above, and every real caller before tonight -- keeps
    getting exactly today's check. That non-regression is the one thing here
    that must never break, since sealed mode is every other deployment.
    """

    def test_sealed_mode_explicit_and_enabled_passes_unchanged(self):
        # (a) sealed mode + commit-reveal True -> passes.
        self.assertIsNone(
            check_commit_reveal_enabled(Chain(commit_reveal=True), netuid=2, publish_mode="sealed")
        )

    def test_sealed_mode_explicit_and_disabled_is_refused_unchanged(self):
        # (b) sealed mode + commit-reveal False -> refuses, same as today.
        with self.assertRaises(SealedPathUnavailable):
            check_commit_reveal_enabled(Chain(commit_reveal=False), netuid=2, publish_mode="sealed")

    def test_omitting_publish_mode_still_defaults_to_sealed(self):
        # The exact non-regression the default argument exists to protect: a
        # caller that predates this parameter, passing nothing, still gets
        # sealed mode's refusal for a subnet with commit-reveal off.
        with self.assertRaises(SealedPathUnavailable):
            check_commit_reveal_enabled(Chain(commit_reveal=False), netuid=2)

    def test_direct_mode_and_disabled_passes(self):
        # (c) direct mode + commit-reveal False -> passes. This is the real
        # precondition direct publishing needs and the new case this exists for.
        self.assertIsNone(
            check_commit_reveal_enabled(Chain(commit_reveal=False), netuid=2, publish_mode="direct")
        )

    def test_direct_mode_and_enabled_is_refused_as_a_misconfiguration(self):
        # (d) direct mode + commit-reveal True -> refuses. A validator configured
        # for direct while the chain still has sealed publication on is itself a
        # real misconfiguration, worth refusing at startup rather than tolerating.
        with self.assertRaises(DirectPathMisconfigured) as caught:
            check_commit_reveal_enabled(Chain(commit_reveal=True), netuid=2, publish_mode="direct")
        self.assertIn("direct", str(caught.exception).lower())

    def test_direct_mode_refusal_does_not_reuse_sealed_mode_wording(self):
        # The two refusals are honestly distinct claims: sealed mode's says the
        # sealed path is unavailable; direct mode's says the chain is not
        # configured the way the CONFIGURED mode needs. Proven by exception
        # type (not just message text), since reusing SealedPathUnavailable's
        # exact class for this case is exactly what the task calls out to avoid.
        with self.assertRaises(DirectPathMisconfigured):
            check_commit_reveal_enabled(Chain(commit_reveal=True), netuid=2, publish_mode="direct")
        self.assertFalse(issubclass(DirectPathMisconfigured, SealedPathUnavailable))
        self.assertFalse(issubclass(SealedPathUnavailable, DirectPathMisconfigured))

    def test_direct_mode_an_unreadable_answer_is_refused_rather_than_assumed_off(self):
        # Mirrors test_an_unreadable_answer_is_refused_rather_than_assumed_on
        # above: a None (unreadable/undecoded) answer must not be read as the
        # False direct mode wants, the same "no quiet fallback" rule.
        with self.assertRaises(DirectPathMisconfigured):
            check_commit_reveal_enabled(Chain(commit_reveal=None), netuid=2, publish_mode="direct")


if __name__ == "__main__":
    unittest.main()


class WeightsThatWillBeThrownAway(unittest.TestCase):
    """A seat with no permit may COMMIT, and that is the whole danger.

    The permit is not checked when a commit is made. It is checked at reveal,
    an epoch later. So a validator without one commits successfully, finds its
    own commit on a read-back, and has its activity stamped, because activity is
    stamped at commit. Then the reveal is discarded and no weights are ever set.

    It reads as healthy on every check there is while doing nothing at all,
    which is precisely the failure this component exists to prevent.
    """

    class Chain:
        def __init__(self, permits, owner=None):
            self.permits = permits
            self.owner = owner

        def has_validator_permit(self, netuid, uid):
            return self.permits[uid]

        def epoch_consensus(self, netuid):
            return "Yuma"

        def owner_uid(self, netuid):
            return self.owner

    def test_a_seat_with_a_permit_passes(self):
        check_has_validator_permit(self.Chain([False, True]), 1, 2)

    def test_a_seat_without_one_refuses(self):
        with self.assertRaises(NoValidatorPermit):
            check_has_validator_permit(self.Chain([False, False]), 1, 2)

    def test_the_owners_seat_passes_without_a_permit(self):
        """The runtime's own exemption, at subnets/weights.rs:1205-1210.

        Refusing here would stop the one validator that can always publish.
        """
        check_has_validator_permit(self.Chain([False, False], owner=1), 1, 2)

    def test_a_non_owner_seat_is_not_exempted_by_an_owner_existing(self):
        # The exemption is for THAT seat, not for any subnet that has one.
        with self.assertRaises(NoValidatorPermit):
            check_has_validator_permit(self.Chain([False, False, False], owner=0), 2, 2)

    def test_the_refusal_says_the_commit_would_have_succeeded(self):
        """Otherwise an operator sees a refusal and assumes publishing fails loudly.

        The whole point is that it does not: it succeeds, and the discarding
        happens later and looks like something else.
        """
        with self.assertRaises(NoValidatorPermit) as caught:
            check_has_validator_permit(self.Chain([False]), 0, 2)
        message = str(caught.exception)
        self.assertIn("commits successfully", message)
        self.assertIn("healthy on every check", message)

    def test_the_refusal_says_what_to_do_about_it(self):
        with self.assertRaises(NoValidatorPermit) as caught:
            check_has_validator_permit(self.Chain([False]), 0, 2)
        self.assertIn("Acquire a permit", str(caught.exception))


# The real floors pyproject.toml declares today, kept here only so tests can
# start from "everything passes" and vary one library at a time. Not a second
# copy this check itself reads from -- check_library_versions() below reads
# pyproject.toml directly on every call, and
# TheFloorComesFromPyprojectTomlItself proves that.
REAL_FLOORS = {
    "async-substrate-interface": "2.2.1",
    "bittensor": "11.1.0",
    "bittensor-core": "0.1.3",
}


def _fake_version(overrides):
    """A stand-in for importlib.metadata.version, answering only named libraries.

    Deliberately refuses an unlisted name rather than falling back to asking
    the real interpreter what is actually installed here: a test that did that
    would pass or fail depending on this venv's own contents, which is exactly
    the kind of environment-dependent test this whole check exists to make
    unnecessary.
    """

    def _version(name):
        if name not in overrides:
            raise AssertionError(f"test did not specify a version for {name!r}")
        value = overrides[name]
        if isinstance(value, BaseException):
            raise value
        return value

    return _version


def _patched_versions(**overrides):
    merged = dict(REAL_FLOORS, **overrides)
    return mock.patch("importlib.metadata.version", _fake_version(merged))


class EveryLibraryAtOrAboveItsFloorPasses(unittest.TestCase):
    def test_every_library_exactly_at_its_declared_floor_passes(self):
        with _patched_versions():
            self.assertIsNone(check_library_versions())

    def test_a_library_above_its_floor_passes(self):
        with _patched_versions(bittensor="11.2.0"):
            self.assertIsNone(check_library_versions())

    def test_a_patch_release_above_the_floor_passes(self):
        with _patched_versions(bittensor="11.1.1"):
            self.assertIsNone(check_library_versions())


class ALibraryBelowItsFloorRefuses(unittest.TestCase):
    def test_async_substrate_interface_a_full_major_version_below_its_floor_refuses(self):
        # The exact shape #542 found: a full major version short.
        with _patched_versions(**{"async-substrate-interface": "1.6.1"}):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        message = str(caught.exception)
        self.assertIn("async-substrate-interface", message)
        self.assertIn("1.6.1", message)
        self.assertIn("2.2.1", message)

    def test_bittensor_below_its_floor_refuses(self):
        with _patched_versions(bittensor="10.9.9"):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        message = str(caught.exception)
        self.assertIn("bittensor 10.9.9", message)
        self.assertIn("11.1.0", message)

    def test_bittensor_core_below_its_floor_refuses(self):
        with _patched_versions(**{"bittensor-core": "0.0.9"}):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        message = str(caught.exception)
        self.assertIn("bittensor-core 0.0.9", message)
        self.assertIn("0.1.3", message)

    def test_two_libraries_below_floor_are_both_named_in_one_refusal(self):
        # gate()'s own property, in neurons/validator.py: report everything
        # found in one pass rather than stopping at the first problem.
        with _patched_versions(bittensor="10.0.0", **{"bittensor-core": "0.0.1"}):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        message = str(caught.exception)
        self.assertIn("bittensor 10.0.0", message)
        self.assertIn("bittensor-core 0.0.1", message)


class ALibraryNotInstalledAtAllRefuses(unittest.TestCase):
    def test_async_substrate_interface_not_installed_refuses(self):
        with _patched_versions(**{"async-substrate-interface": metadata.PackageNotFoundError()}):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        message = str(caught.exception)
        self.assertIn("async-substrate-interface", message)
        self.assertIn("NOT INSTALLED", message)

    def test_bittensor_not_installed_refuses(self):
        with _patched_versions(bittensor=metadata.PackageNotFoundError()):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        self.assertIn("bittensor", str(caught.exception))
        self.assertIn("NOT INSTALLED", str(caught.exception))

    def test_bittensor_core_not_installed_refuses(self):
        with _patched_versions(**{"bittensor-core": metadata.PackageNotFoundError()}):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        self.assertIn("bittensor-core", str(caught.exception))
        self.assertIn("NOT INSTALLED", str(caught.exception))


class TheRefusalNamesWhatAnOperatorNeeds(unittest.TestCase):
    def test_the_refusal_names_the_interpreter_path(self):
        # "wrong python" is the actual operator error most of the time this
        # fires; the message has to say which interpreter it is.
        with _patched_versions(bittensor="10.0.0"):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        self.assertIn(sys.executable, str(caught.exception))

    def test_the_refusal_points_at_install_sh(self):
        with _patched_versions(bittensor="10.0.0"):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        self.assertIn("scripts/install.sh", str(caught.exception))


@_needs_real_tomllib
class TheFloorComesFromPyprojectTomlItself(unittest.TestCase):
    """Proof this reads the floor out of pyproject.toml on every call rather
    than carrying a second, hand-typed copy of it -- the exact duplication
    shape #541 found four separate times, each a value declared once and
    quietly drifting from its own source unnoticed.

    Skipped on an interpreter with no TOML parser: see `_needs_real_tomllib`.
    """

    def test_changing_the_declared_floor_changes_what_this_check_enforces(self):
        text = (
            "[project]\n"
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'dev = ["bittensor>=99.0.0"]\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
            fake_pyproject = Path(handle.name)

        # bittensor 11.1.0 satisfies the REAL floor this project declares, and
        # would pass against it. It must still fail here, because the floor
        # this check enforces has to come from whichever pyproject.toml is
        # actually read, not from a value remembered anywhere in this file.
        with mock.patch("noah.core.startup._PYPROJECT", fake_pyproject):
            with _patched_versions(bittensor="11.1.0"):
                with self.assertRaises(UnsupportedLibraryVersion) as caught:
                    check_library_versions(("bittensor",))
        self.assertIn("99.0.0", str(caught.exception))

    def test_a_library_missing_from_pyprojecttoml_is_a_bug_in_the_check_not_a_refusal(self):
        text = "[project]\ndependencies = []\n"
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
            fake_pyproject = Path(handle.name)

        with mock.patch("noah.core.startup._PYPROJECT", fake_pyproject):
            with _patched_versions(bittensor="11.1.0"):
                with self.assertRaises(UnsupportedLibraryVersion) as caught:
                    check_library_versions(("bittensor",))
        self.assertIn("no longer declares", str(caught.exception))


class TheOldMistakeCannotReappearAtModuleScope(unittest.TestCase):
    """`#543`, found live: a bare `import tomllib` sat at this file's own
    module scope, and raised `ModuleNotFoundError` at import time -- before
    `check_library_versions`, before `main()`, before argument parsing --
    on any interpreter below Python 3.11 with no `tomli` fallback installed
    (which this project does not declare). `/usr/bin/python3` on macOS
    (3.9.6, confirmed live 2026-08-29) hit it exactly this way.

    Parses this file's own source with `ast` and checks only the MODULE-
    LEVEL statements (`tree.body`, not anything nested inside a function),
    so `_tomllib()`'s own lazy, function-scoped `import tomllib` is
    untouched -- only a reappearance of the historic mistake, one call
    frame up, trips this.
    """

    def test_no_module_scope_import_tomllib(self):
        source = Path(startup.__file__).read_text()
        tree = ast.parse(source, str(startup.__file__))
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                self.assertNotIn(
                    "tomllib",
                    names,
                    "a bare `import tomllib` reappeared at module scope in "
                    "noah/core/startup.py -- this is the exact #543 crash: it "
                    "raises ModuleNotFoundError at import time, on any "
                    "interpreter below Python 3.11, before this file's own "
                    "refusal message ever gets a chance to run.",
                )
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(
                    node.module,
                    "tomllib",
                    "a module-scope `from tomllib import ...` reappeared in "
                    "noah/core/startup.py -- the same #543 crash shape as a "
                    "bare `import tomllib` at module scope.",
                )


class WhenThisInterpreterHasNoTomlParser(unittest.TestCase):
    """`#543`, reproduced without needing this suite to actually run under a
    below-3.11 interpreter. Setting ``sys.modules["tomllib"] = None`` makes
    Python's own import machinery raise ``ImportError`` for ``import
    tomllib`` wherever it is attempted, on ANY interpreter -- including this
    project's own 3.11+ dev venv that runs this suite. That is exactly what
    ``_tomllib()``'s ``except ImportError`` is written to catch, so this
    proves the fallback path actually runs under a real (simulated) absence,
    not just that it reads correctly in isolation.
    """

    def test_tomllib_helper_returns_none_when_unavailable(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertIsNone(startup._tomllib())

    def test_declared_specifier_returns_the_sentinel_not_a_crash(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertIs(startup._declared_specifier("bittensor"), startup._TOMLLIB_UNAVAILABLE)

    def test_check_library_versions_passes_using_the_fallback_floors(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            with _patched_versions():
                self.assertIsNone(check_library_versions())

    def test_check_library_versions_still_refuses_a_library_below_its_fallback_floor(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            with _patched_versions(bittensor="10.9.9"):
                with self.assertRaises(UnsupportedLibraryVersion) as caught:
                    check_library_versions()
        message = str(caught.exception)
        # Message quality must survive the fallback path exactly as it does
        # the normal one: found version, required floor, and the
        # interpreter named on its own line, so the "wrong python" fix is
        # still the thing an operator reads off this refusal.
        self.assertIn("bittensor 10.9.9", message)
        self.assertIn("11.1.0", message)
        self.assertIn(sys.executable, message)
        self.assertIn("scripts/install.sh", message)

    def test_check_library_versions_still_refuses_a_library_not_installed_at_all(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            with _patched_versions(bittensor=metadata.PackageNotFoundError()):
                with self.assertRaises(UnsupportedLibraryVersion) as caught:
                    check_library_versions()
        message = str(caught.exception)
        self.assertIn("bittensor", message)
        self.assertIn("NOT INSTALLED", message)

    def test_check_library_versions_still_enforces_the_ceiling_via_the_fallback(self):
        # The whole reason _FALLBACK_SPECIFIERS carries the full specifier,
        # not just the floor: bittensor's <12.0.0 ceiling must still bind
        # even on an interpreter with no TOML parser to read it out of
        # pyproject.toml directly.
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            with _patched_versions(bittensor="12.0.0"):
                with self.assertRaises(UnsupportedLibraryVersion) as caught:
                    check_library_versions()
        self.assertIn("bittensor 12.0.0", str(caught.exception))

    def test_the_real_module_import_survives_this_interpreter_having_no_tomllib(self):
        """The actual #543 crash was never `check_library_versions()`
        raising -- it was `import noah.core.startup` itself raising, before
        any function in this file ever got to run. A subprocess, run under
        THIS SAME interpreter (whatever Python actually runs this suite),
        with `tomllib` pre-poisoned in `sys.modules` before the import,
        proves the module body itself has no top-level statement that needs
        `tomllib` -- independent of which Python version happens to run
        this suite, and without the identity/leakage hazards of
        `importlib.reload()` on a module other tests in this process also
        import.
        """
        repo_root = Path(startup.__file__).resolve().parents[2]
        script = (
            "import sys\n"
            "sys.modules['tomllib'] = None\n"
            "import noah.core.startup\n"
            "print('module imported without crashing')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("module imported without crashing", result.stdout)


@_needs_real_tomllib
class TheFallbackSpecifiersCannotDrift(unittest.TestCase):
    """``_FALLBACK_SPECIFIERS`` is a second, hand-typed copy of a value
    pyproject.toml already declares -- the exact shape `#541` found four
    real instances of, each one a value written once and quietly duplicated,
    drifting from its own source unnoticed. The difference here is
    enforcement, not restraint: this test parses pyproject.toml directly,
    with a real ``tomllib`` (this project's own dev interpreter is always
    3.11+ -- ``.venv-validator/bin/python3.11``), and fails the moment
    ``_FALLBACK_SPECIFIERS`` disagrees with what is actually declared. Two
    typed copies that CI proves identical is a different and safe shape from
    `#541`'s actual failure, which had no such enforcement at all.

    The specifier compared is the FULL one (``">=11.1.0,<12.0.0"``), not just
    its floor: `_FALLBACK_SPECIFIERS` was upgraded from floor-only to
    full-specifier so a Python 3.10 interpreter -- the one this fallback
    exists for -- still gets bittensor's `<12.0.0` ceiling enforced, not only
    its `>=11.1.0` floor. A test that only checked the floor half would not
    catch that ceiling silently drifting.

    Skipped entirely (class-level, above) on an interpreter with no TOML
    parser, rather than letting it run and fail. This project's own
    `requires-python = ">=3.10"` and `.github/workflows/ci.yml`'s own matrix
    both mean Python 3.10 is a real, supported CI job, not a hypothetical --
    and on that job the unguarded `import tomllib` two methods down used to
    break the whole class outright (a collection-time `ModuleNotFoundError`,
    not a clean, reported test failure) before this skip existed. The
    3.11/3.12/3.13 CI matrix jobs still run and enforce this class every
    time. Found by codex adversarial review (2026-08-29).
    """

    def test_this_suite_actually_runs_with_a_real_tomllib(self):
        # If this stops holding, the test below stops proving anything: it
        # would be comparing the fallback against nothing, silently, rather
        # than against the real manifest. Asserted, not assumed.
        self.assertIsNotNone(
            startup._tomllib(),
            "this test suite's own interpreter has no tomllib, so "
            "TheFallbackSpecifiersCannotDrift cannot verify "
            "_FALLBACK_SPECIFIERS against the real pyproject.toml. Run the "
            "suite on this project's own dev interpreter "
            "(.venv-validator/bin/python3.11 or newer).",
        )

    def test_every_fallback_specifier_matches_the_real_declared_specifier(self):
        import tomllib as real_tomllib

        data = real_tomllib.loads(startup._PYPROJECT.read_text())
        project = data.get("project", {})
        lines = list(project.get("dependencies") or [])
        for extra in (project.get("optional-dependencies") or {}).values():
            lines.extend(extra)

        declared_specifiers = {}
        for line in lines:
            # Extras removed, not truncated at: "bittensor[wallet]>=11.1.0"
            # must keep its ">=11.1.0" half. An earlier version of both this
            # test and the production code it checks truncated at the first
            # "[" instead, which drops the specifier entirely for any real
            # dependency written with extras -- found by codex adversarial
            # review (2026-08-29). This independent reading has to make the
            # same fix `_declared_specifier` did, or it would silently stop
            # verifying that fix the moment a guarded dependency ever adds
            # an extra.
            clause = re.sub(r"\[[^\]]*\]", "", line.split(";")[0])
            bare = clause
            for sep in (">=", "<=", "==", "!=", "~=", ">", "<"):
                bare = bare.split(sep)[0]
            name = bare.strip().lower().replace("_", "-")
            declared_specifiers[name] = clause.strip()[len(bare.strip()) :].strip()

        for name, fallback_specifier in startup._FALLBACK_SPECIFIERS.items():
            with self.subTest(name=name):
                self.assertIn(
                    name,
                    declared_specifiers,
                    f"_FALLBACK_SPECIFIERS names {name!r} but pyproject.toml declares no "
                    f"constraint for it at all.",
                )
                self.assertEqual(
                    fallback_specifier,
                    declared_specifiers[name],
                    f"_FALLBACK_SPECIFIERS[{name!r}] = {fallback_specifier!r} but "
                    f"pyproject.toml actually declares {declared_specifiers[name]!r}. "
                    f"This is the #541 drift shape: update _FALLBACK_SPECIFIERS to "
                    f"match.",
                )

    def test_every_chain_library_has_a_fallback_specifier(self):
        # The inverse gap: a name added to CHAIN_LIBRARIES with no matching
        # _FALLBACK_SPECIFIERS entry raises loudly (check_library_versions'
        # own guard), but only on a below-floor interpreter -- exactly the
        # environment where discovering it live is most expensive. Caught
        # here instead, on every interpreter, every run.
        for name in startup.CHAIN_LIBRARIES:
            with self.subTest(name=name):
                self.assertIn(name, startup._FALLBACK_SPECIFIERS)


class TheFullConstraintIsEnforcedNotOnlyTheFloor(unittest.TestCase):
    """Codex adversarial review (2026-08-29) of an earlier, floor-only
    version of this check: `bittensor==12.0.0` satisfied `>=11.1.0` and was
    never checked against the `<12.0.0` half of the constraint pyproject.toml
    actually declares, even though that ceiling is documented there at
    length as guarding the exact `bittensor.result` symbols
    `noah/core/reader.py` depends on. These tests pin that the ceiling -- not
    only the floor -- is what this check actually enforces.
    """

    def test_a_version_at_the_ceiling_refuses(self):
        # bittensor>=11.1.0,<12.0.0 -- 12.0.0 satisfies the floor and
        # violates the ceiling.
        with _patched_versions(bittensor="12.0.0"):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        message = str(caught.exception)
        self.assertIn("bittensor 12.0.0", message)
        self.assertIn("does not satisfy", message)

    def test_a_version_above_the_ceiling_refuses(self):
        with _patched_versions(bittensor="13.0.0"):
            with self.assertRaises(UnsupportedLibraryVersion):
                check_library_versions()

    def test_a_version_just_under_the_ceiling_passes(self):
        with _patched_versions(bittensor="11.9.9"):
            self.assertIsNone(check_library_versions())


class APreReleaseVersionRefusesRatherThanGuessing(unittest.TestCase):
    """Codex adversarial review (2026-08-29): a prior digit-prefix scan made
    ``"11.1.0rc1"`` compare EQUAL to the floor ``"11.1.0"``, when real
    version ordering (PEP 440) places a release candidate BEFORE the release
    it precedes -- an installed pre-release silently read as meeting a floor
    it does not meet. This project takes on no new dependency for full
    PEP 440 ordering, so a version that is not a plain dotted-integer release
    now refuses rather than guesses.
    """

    def test_a_release_candidate_of_the_floor_version_refuses(self):
        with _patched_versions(bittensor="11.1.0rc1"):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        message = str(caught.exception)
        self.assertIn("bittensor 11.1.0rc1", message)
        self.assertIn("cannot confidently evaluate", message)

    def test_a_post_release_marker_also_refuses_rather_than_silently_passing(self):
        with _patched_versions(bittensor="11.1.0.post1"):
            with self.assertRaises(UnsupportedLibraryVersion) as caught:
                check_library_versions()
        self.assertIn("cannot confidently evaluate", str(caught.exception))


@_needs_real_tomllib
class AnUnrecognisedComparisonOperatorRefusesRatherThanPassing(unittest.TestCase):
    """``_COMPARATORS`` only evaluates the five operators this project's own
    declared specifiers actually use. "~=" and "!=" are not declared
    anywhere in pyproject.toml today; if one appeared, this proves it is
    refused rather than silently treated as satisfied -- the same direction
    `#510` already chose for a shape this file cannot yet interpret.

    Skipped on an interpreter with no TOML parser: see `_needs_real_tomllib`.
    """

    def test_a_compatible_release_operator_refuses_rather_than_silently_passing(self):
        text = (
            "[project]\n"
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'dev = ["bittensor~=11.1"]\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
            fake_pyproject = Path(handle.name)

        with mock.patch("noah.core.startup._PYPROJECT", fake_pyproject):
            with _patched_versions(bittensor="11.1.0"):
                with self.assertRaises(UnsupportedLibraryVersion) as caught:
                    check_library_versions(("bittensor",))
        self.assertIn("cannot confidently evaluate", str(caught.exception))


@_needs_real_tomllib
class ExtrasSyntaxDoesNotSilentlyDisableTheCheck(unittest.TestCase):
    """A dependency written with PEP 508 extras, ``bittensor[wallet]>=11.1.0``,
    puts the version clause AFTER the bracketed extra. An earlier version of
    `_declared_specifier` truncated the whole line at its first "[", which
    dropped the specifier along with the extra: `_declared_specifier` then
    returned an empty string, `_clauses("")` found no clauses to evaluate,
    and `check_library_versions` passed regardless of what was actually
    installed -- the single worst failure shape this guard can have, and
    found by codex adversarial review (2026-08-29) rather than a real
    dependency line, since no guarded library uses extras in this project's
    pyproject.toml today. These tests pin that a hypothetical one would
    still be enforced, not silently waved through.
    """

    def test_a_dependency_written_with_extras_still_has_its_specifier_read(self):
        text = (
            "[project]\n"
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'dev = ["bittensor[wallet]>=11.1.0,<12.0.0"]\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
            fake_pyproject = Path(handle.name)

        with mock.patch("noah.core.startup._PYPROJECT", fake_pyproject):
            self.assertEqual(startup._declared_specifier("bittensor"), ">=11.1.0,<12.0.0")

    def test_a_below_floor_version_still_refuses_when_the_dependency_has_extras(self):
        text = (
            "[project]\n"
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'dev = ["bittensor[wallet]>=11.1.0,<12.0.0"]\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
            fake_pyproject = Path(handle.name)

        with mock.patch("noah.core.startup._PYPROJECT", fake_pyproject):
            with _patched_versions(bittensor="10.0.0"):
                with self.assertRaises(UnsupportedLibraryVersion) as caught:
                    check_library_versions(("bittensor",))
        self.assertIn("bittensor 10.0.0", str(caught.exception))
