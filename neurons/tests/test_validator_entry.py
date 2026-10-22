"""The gate a validator must pass before its first period.

The interesting property is not that the gate refuses. It is that a run reports
EVERY answer rather than stopping at the first refusal. Stopping tells an
operator to fix one thing; they fix it, run again, and meet the next one, which
is how a five-minute problem becomes an evening.

The second property is that a refusal is never quiet. Anything that stops this
validator has to say so in a line an operator can act on, because the failure
this whole component guards against is a validator that looks fine and is not.
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import neurons.validator as validator_module  # noqa: E402
from neurons.validator import gate, load_settings  # noqa: E402
from noah.core.reader import ChainUnreadable  # noqa: E402
from noah.core.settings import Settings, SettingsError  # noqa: E402
from noah.core.startup import UnsupportedLibraryVersion  # noqa: E402

GOOD_ENV = {
    "NOAH_WALLET_NAME": "w",
    "NOAH_WALLET_HOTKEY": "h",
    "NOAH_NETWORK": "local",
    "NOAH_NETUID": "2",
    "NOAH_CHAIN_ENDPOINT": "ws://127.0.0.1:9955",
    "NOAH_LOG_LEVEL": "info",
    "NOAH_SCORE_INTERVAL_BLOCKS": "100",
    "NOAH_WEIGHT_INTERVAL_BLOCKS": "100",
    "NOAH_SETTLE_BLOCKS": "20",
    "NOAH_REQUIRE_TWIN_PROOF": "true",
    "NOAH_LOCAL_GENESIS": "0x933b",
}
SETTINGS = Settings.from_mapping(dict(GOOD_ENV))
OURS = "5OURS"


class Reader:
    """A chain answering the six operating questions, each settable."""

    def __init__(self, **over):
        self.o = over

    def subnet_exists(self, netuid):
        return self.o.get("subnet_exists", True)

    def has_validator_permit(self, netuid, uid):
        return self.o.get("permit", True)

    def epoch_consensus(self, netuid):
        return self.o.get("consensus", "Yuma")

    def owner_uid(self, netuid):
        return self.o.get("owner_uid", None)

    def seat_of(self, hotkey, netuid):
        if "seat_raises" in self.o:
            raise self.o["seat_raises"]
        return self.o.get("seat", 0)

    def current_block(self):
        return self.o.get("block", 10_000)

    def last_update(self, uid, netuid):
        if "last_update_raises" in self.o:
            raise self.o["last_update_raises"]
        return self.o.get("last_update", 9_000)

    def rate_limit(self, netuid):
        return self.o.get("rate_limit", 100)

    def historical_probe(self, depth):
        return self.o.get("history", True)

    def commit_reveal(self, netuid):
        return self.o.get("sealed", True)


def answers(**over):
    results, uid = gate(Reader(**over), SETTINGS, OURS)
    return dict(results), uid


class EveryQuestionIsAnsweredNotJustTheFirstFailure(unittest.TestCase):
    def test_a_healthy_chain_answers_every_question_yes(self):
        got, uid = answers()
        self.assertEqual(uid, 0)
        self.assertEqual(len(got), 6)
        self.assertTrue(all(not v.startswith("NO") for v in got.values()))

    def test_two_faults_are_both_reported_in_one_run(self):
        got, _ = answers(history=False, sealed=False)
        self.assertTrue(got["node answers history"].startswith("NO"))
        self.assertTrue(got["sealed publication on"].startswith("NO"))

    def test_a_late_question_still_answers_when_an_earlier_one_failed(self):
        got, _ = answers(history=False)
        self.assertTrue(got["sealed publication on"].startswith("yes"))


DIRECT_SETTINGS = Settings.from_mapping(dict(GOOD_ENV, NOAH_PUBLISH_MODE="direct"))


class TheSealedQuestionFollowsPublishMode(unittest.TestCase):
    """gate()'s sealed-publication question asks the opposite thing in direct mode.

    SETTINGS above (module-level, used by every other test via `answers()`)
    never sets NOAH_PUBLISH_MODE, so it defaults to "sealed" and every test
    above this class is proof the sealed-mode question and its label
    ("sealed publication on") are completely unchanged. These use
    DIRECT_SETTINGS instead to prove the new, mode-aware branch.
    """

    def test_sealed_mode_default_is_unchanged_end_to_end(self):
        # (a)+(b) restated through gate() itself, not just the underlying
        # check: SETTINGS defaults to sealed mode, so this is the exact
        # non-regression path -- unchanged label, unchanged pass/refuse.
        got, _ = answers(sealed=True)
        self.assertTrue(got["sealed publication on"].startswith("yes"))
        got, _ = answers(sealed=False)
        self.assertTrue(got["sealed publication on"].startswith("NO"))

    def test_direct_mode_with_sealing_off_passes(self):
        # (c) the new, correct case this fix exists for: a validator
        # configured for direct publishing, on a chain with sealed
        # publication off, passes the gate.
        results, uid = gate(Reader(sealed=False), DIRECT_SETTINGS, OURS)
        got = dict(results)
        self.assertEqual(uid, 0)
        self.assertEqual(got["sealed publication off (direct mode)"], "yes")

    def test_direct_mode_with_sealing_still_on_is_refused_honestly(self):
        # (d) the new misconfiguration case: a validator configured for direct
        # publishing while the chain still has sealed publication on is
        # refused, and the reported reason is the direct-mode one, not the
        # sealed-mode "sealed publication unavailable" wording.
        results, uid = gate(Reader(sealed=True), DIRECT_SETTINGS, OURS)
        got = dict(results)
        outcome = got["sealed publication off (direct mode)"]
        self.assertTrue(outcome.startswith("NO"))
        self.assertIn("direct", outcome.lower())
        self.assertNotIn("this validator publishes only by the sealed path", outcome)

    def test_direct_mode_gate_still_reports_every_other_question(self):
        # gate()'s whole point -- answer everything, not just the first
        # refusal -- must hold in direct mode too.
        results, _ = gate(Reader(sealed=True, history=False), DIRECT_SETTINGS, OURS)
        got = dict(results)
        self.assertEqual(len(got), 6)
        self.assertTrue(got["node answers history"].startswith("NO"))
        self.assertTrue(got["sealed publication off (direct mode)"].startswith("NO"))


class NoSeatStopsEverythingBecauseNothingElseIsMeaningful(unittest.TestCase):
    def test_holding_no_seat_is_reported_and_the_rest_is_not_guessed(self):
        # The remaining questions are about a seat. Answering them for a
        # validator that holds none would print reassuring lines under a fatal
        # one, which is worse than printing nothing. The subnet question stays,
        # because it was answered before the seat question and answered yes.
        got, uid = answers(seat=None)
        self.assertIsNone(uid)
        self.assertTrue(got["holds a seat"].startswith("NO"))
        self.assertEqual(list(got), ["subnet exists", "holds a seat"])

    def test_seat_zero_is_a_seat_and_not_an_absence(self):
        got, uid = answers(seat=0)
        self.assertEqual(uid, 0)
        self.assertTrue(got["holds a seat"].startswith("yes"))


class ARefusalAlwaysSaysWhatToDo(unittest.TestCase):
    def test_no_seat_says_how_to_get_one(self):
        got, _ = answers(seat=None)
        self.assertIn("Register", got["holds a seat"])

    def test_a_rate_limit_wait_says_it_is_not_a_fault(self):
        # The distinction that has already cost this project a wrong verdict: a
        # seat waiting out its window is healthy, and reads identically to a
        # broken one unless the message says so.
        got, _ = answers(last_update=9_950, block=10_000)
        self.assertTrue(got["may publish yet"].startswith("NO"))
        self.assertIn("not a fault", got["may publish yet"])


class SettingsAreReadBeforeAnythingConnects(unittest.TestCase):
    def test_a_settings_file_is_read_and_validated(self):
        import tempfile

        text = "\n".join(f"{k}={v}" for k, v in GOOD_ENV.items())
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(text)
            name = fh.name
        self.assertEqual(load_settings(name).netuid, 2)

    def test_a_settings_file_with_an_unknown_name_refuses(self):
        import tempfile

        text = "\n".join(f"{k}={v}" for k, v in GOOD_ENV.items()) + "\nNOAH_NOT_A_SETTING=1"
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(text)
            name = fh.name
        with self.assertRaises(SettingsError):
            load_settings(name)


if __name__ == "__main__":
    unittest.main()


class ASubnetThatDoesNotExistIsCaughtFirst(unittest.TestCase):
    """Before anything else, because almost nothing else would notice.

    A subnet that was never created answers the rate limit, the activity factor
    and the sealed-publication flag identically to a real one, so every later
    check passes. The one that does not is the seat check, which refuses for the
    wrong reason and tells an operator to register on a subnet that is not
    there.
    """

    def test_a_phantom_subnet_stops_the_gate_at_the_first_question(self):
        got, uid = answers(subnet_exists=False)
        self.assertIsNone(uid)
        self.assertEqual(list(got), ["subnet exists"])
        self.assertTrue(got["subnet exists"].startswith("NO"))

    def test_it_is_asked_before_the_seat_question(self):
        # Order is the whole point. The seat check would answer first and
        # answer wrongly.
        got, _ = answers(subnet_exists=False, seat=None)
        self.assertNotIn("holds a seat", got)

    def test_the_refusal_warns_against_registering_on_it(self):
        got, _ = answers(subnet_exists=False)
        self.assertIn("register on a subnet that is not there", got["subnet exists"])

    def test_a_real_subnet_passes_and_the_rest_still_runs(self):
        got, uid = answers()
        self.assertEqual(got["subnet exists"], "yes")
        self.assertEqual(uid, 0)
        self.assertEqual(len(got), 6)


class WeightsThrownAwayAtReveal(unittest.TestCase):
    """A seat can pass every other question and still set nothing.

    The permit is checked at reveal, not at commit, so this refusal has to
    happen before publishing or the failure arrives an epoch late looking like
    something else.
    """

    def test_a_seat_without_a_permit_is_refused_by_name(self):
        got, _ = answers(permit=False)
        self.assertTrue(got["weights are accepted"].startswith("NO"))

    def test_the_other_questions_are_still_answered(self):
        # It is not fatal to the run's usefulness; an operator wants the rest.
        got, _ = answers(permit=False)
        self.assertEqual(got["node answers history"], "yes")
        self.assertEqual(got["sealed publication on"], "yes")

    def test_the_owners_seat_passes_without_one(self):
        got, _ = answers(permit=False, owner_uid=0)
        self.assertEqual(got["weights are accepted"], "yes")

    def test_it_is_asked_after_the_rate_limit_and_before_the_node_questions(self):
        got, _ = answers()
        order = list(got)
        self.assertLess(order.index("may publish yet"), order.index("weights are accepted"))
        self.assertLess(order.index("weights are accepted"), order.index("node answers history"))


class AFailedChainReadDuringPreflightIsARefusalNotACrash(unittest.TestCase):
    """Every question above is answered by reading the chain, and a read can
    fail outright -- a dropped connection, a timeout -- for reasons that have
    nothing to do with the answer being asked. Each check's except clause used
    to name only its own semantic refusal (``NotRegistered``,
    ``CannotPublishYet``, and so on), so a read failure fell through every one
    of them and reached nobody, surfacing as a raw traceback that read like
    the validator itself was broken rather than like the chain not answering.

    ``ChainUnreadable`` (``noah.core.reader``) is what ``ChainReader`` raises
    for exactly that failure. It is a sibling ``SettingsError``, not a
    subclass of any of the six named refusals, so only a check that names it
    explicitly, alongside its own refusal, will catch it.
    """

    def test_a_dropped_read_during_the_seat_check_is_a_clean_refusal(self):
        got, uid = answers(seat_raises=ChainUnreadable("dropped mid-read"))
        self.assertIsNone(uid)
        self.assertTrue(got["holds a seat"].startswith("NO"))
        self.assertIn("dropped mid-read", got["holds a seat"])
        # Same shape as a genuine NotRegistered: nothing after it is guessed.
        self.assertEqual(list(got), ["subnet exists", "holds a seat"])

    def test_a_dropped_read_inside_the_looped_checks_is_a_clean_refusal(self):
        # "may publish yet" is the loop's own check: check_can_publish_yet
        # reads last_update, and a failed read there used to escape the
        # loop's `except refusal` entirely.
        got, uid = answers(last_update_raises=ChainUnreadable("dropped mid-read"))
        self.assertEqual(uid, 0)
        self.assertTrue(got["may publish yet"].startswith("NO"))
        self.assertIn("dropped mid-read", got["may publish yet"])

    def test_the_remaining_questions_still_run_after_a_failed_read(self):
        # gate()'s whole point is answering everything rather than stopping at
        # the first failure. A read failure has to keep that property exactly
        # like a real refusal does.
        got, _ = answers(last_update_raises=ChainUnreadable("dropped mid-read"))
        self.assertEqual(got["weights are accepted"], "yes")
        self.assertEqual(got["sealed publication on"], "yes")

    def test_an_unrelated_bug_is_still_not_swallowed(self):
        # gate()'s own except clause names ChainUnreadable specifically, not
        # Exception, so a TypeError reaching IT directly still crashes rather
        # than getting reported as a plausible-looking refusal. What this
        # test proves is that gate() itself adds no second place where an
        # unrelated bug could be mistaken for a refusal -- on top of, not
        # instead of, ChainReader's own such guarantee (noah/core/reader.py,
        # fixed by #510): before that fix, ChainReader's _query converted
        # EVERY exception it saw, including a TypeError from a real bug,
        # into ChainUnreadable, so this class's own fake `Reader` (a
        # duck-typed stand-in, not the real ChainReader) is what makes a
        # bare TypeError reachable here at all, independent of whichever
        # policy ChainReader itself currently applies.
        with self.assertRaises(TypeError):
            answers(last_update_raises=TypeError("not a chain-read refusal"))


class SurveyReader:
    """A chain answering what `survey()` needs, each read failure settable.

    Deliberately separate from `Reader` above: `gate()`'s six operating
    questions and `survey()`'s reads are different calls (survey runs only
    after gate has already passed), and giving `survey` its own minimal
    fake keeps each test naming exactly the read it means to fail.
    """

    def __init__(self, **over):
        self.o = over
        self.block = over.get("block", 10_000)

    def tempo(self, netuid):
        if "tempo_raises" in self.o:
            raise self.o["tempo_raises"]
        return self.o.get("tempo", 100)

    def current_block(self):
        if "current_block_raises" in self.o:
            raise self.o["current_block_raises"]
        return self.block

    def committed_in_period(self, netuid, period, tempo, hotkey=None, head=None):
        if "committed_raises" in self.o:
            raise self.o["committed_raises"]
        return self.o.get("committed", set())

    def live_uids(self, netuid):
        if "live_uids_raises" in self.o:
            raise self.o["live_uids_raises"]
        return self.o.get("live_uids", [0])

    def owner_uids(self, netuid):
        if "owner_uids_raises" in self.o:
            raise self.o["owner_uids_raises"]
        return self.o.get("owner_uids", set())


class DirectSurveyReportsActivityWithoutClaimingPublication(unittest.TestCase):
    def reader(self, **over):
        reader = SurveyReader(committed_raises=AssertionError("direct mode scanned sealed commits"))
        identity = Reader(**over)
        reader.seat_of = mock.Mock(side_effect=identity.seat_of)
        reader.last_update = mock.Mock(side_effect=identity.last_update)
        return reader

    def test_uid_zero_activity_is_not_a_publication_receipt(self):
        reader = self.reader(seat=0, last_update=9_000)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(validator_module.survey(reader, DIRECT_SETTINGS, OURS))
        reader.seat_of.assert_called_once_with(OURS, DIRECT_SETTINGS.netuid)
        reader.last_update.assert_called_once_with(0, DIRECT_SETTINGS.netuid)
        self.assertIn("block 9,000 (seat 0)", output.getvalue())
        self.assertIn("not proof that a scoring day was published", output.getvalue())
        self.assertIn("not evaluated by this survey", output.getvalue())
        self.assertIn("durable receipt/intent and finalized-chain reconciliation", output.getvalue())
        self.assertNotIn("already published", output.getvalue())

    def test_missing_or_unreadable_identity_fails_instead_of_reporting_unpublished(self):
        for over in ({"seat": None}, {"seat_raises": ChainUnreadable("dropped")}):
            with self.subTest(over=over):
                reader = self.reader(**over)
                self.assertFalse(validator_module.survey(reader, DIRECT_SETTINGS, OURS))
                reader.last_update.assert_not_called()

    def test_unreadable_activity_fails(self):
        reader = self.reader(last_update_raises=ChainUnreadable("dropped"))
        self.assertFalse(validator_module.survey(reader, DIRECT_SETTINGS, OURS))

    def test_programming_error_is_not_hidden(self):
        reader = self.reader(last_update_raises=TypeError("bad call"))
        with self.assertRaises(TypeError):
            validator_module.survey(reader, DIRECT_SETTINGS, OURS)

    def test_sealed_mode_still_uses_its_commit_evidence(self):
        reader = SurveyReader(committed={OURS})
        reader.last_update = mock.Mock(side_effect=AssertionError("sealed mode read activity"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(validator_module.survey(reader, SETTINGS, OURS))
        self.assertIn("already published     YES, by us", output.getvalue())
        reader.last_update.assert_not_called()


class ASurveyReadThatFailsIsAReportNotACrash(unittest.TestCase):
    """survey() (neurons/validator.py) runs only after gate() has already
    passed, purely to show an operator what a real period would act on.
    Found by codex adversarial review of #510 (2026-08-29): three of its
    four ChainReader reads had no guard at all, and the fourth caught bare
    `Exception`, so BOTH a real (fresh, post-gate) transport failure and a
    genuine validator bug reached an operator the same way -- a real
    failure should print a clean line like every other refusal in this
    module, and a genuine bug should still crash loudly rather than
    printing a plausible "could not tell".

    Each "reported not crashed" case also pins survey()'s return value:
    False. A second codex finding on the same commit was that main()
    ignored it and returned 0 unconditionally, so a dry run whose
    connection dropped mid-survey still reported success on the one
    signal automation would check.
    """

    def test_a_real_failure_reading_tempo_or_height_is_reported_not_crashed(self):
        from neurons.validator import survey

        ok = survey(SurveyReader(current_block_raises=ChainUnreadable("dropped")), SETTINGS, OURS)
        self.assertFalse(ok)

    def test_a_bug_reading_tempo_or_height_still_crashes(self):
        from neurons.validator import survey

        with self.assertRaises(TypeError):
            survey(SurveyReader(current_block_raises=TypeError("bad call")), SETTINGS, OURS)

    def test_a_real_failure_reading_what_was_committed_is_reported_not_crashed(self):
        from neurons.validator import survey

        ok = survey(SurveyReader(committed_raises=ChainUnreadable("dropped")), SETTINGS, OURS)
        self.assertFalse(ok)

    def test_a_bug_reading_what_was_committed_still_crashes(self):
        from neurons.validator import survey

        with self.assertRaises(TypeError):
            survey(SurveyReader(committed_raises=TypeError("bad call")), SETTINGS, OURS)

    def test_a_real_failure_reading_the_seat_list_is_reported_not_crashed(self):
        from neurons.validator import survey

        ok = survey(SurveyReader(live_uids_raises=ChainUnreadable("dropped")), SETTINGS, OURS)
        self.assertFalse(ok)

    def test_a_bug_reading_the_seat_list_still_crashes(self):
        from neurons.validator import survey

        with self.assertRaises(TypeError):
            survey(SurveyReader(live_uids_raises=TypeError("bad call")), SETTINGS, OURS)

    def test_a_healthy_chain_surveys_without_raising(self):
        from neurons.validator import survey

        self.assertTrue(survey(SurveyReader(), SETTINGS, OURS))

    def test_nothing_to_score_yet_is_a_normal_outcome_not_a_failure(self):
        # A young chain with no settled period is not a read failure, and
        # must not make main() treat the dry run as incomplete.
        from neurons.validator import survey

        self.assertTrue(survey(SurveyReader(block=10, tempo=100), SETTINGS, OURS))


class TheLibraryCheckRunsBeforeAnythingElseInMain(unittest.TestCase):
    """#543: `main()` must check the environment before it does anything else,
    not somewhere inside settings loading or chain connection. These tests
    exercise `main()`'s own wiring, not `check_library_versions()` itself
    (covered directly in `noah/core/tests/test_startup.py`): that it runs
    first, refuses loudly with a non-zero exit and a clear printed message,
    and does not block a healthy environment from reaching settings.
    """

    def test_a_library_refusal_stops_main_before_settings_are_read(self):
        def must_not_run(*_args, **_kwargs):
            raise AssertionError(
                "load_settings ran; check_library_versions did not stop main() first"
            )

        with (
            mock.patch.object(
                validator_module,
                "check_library_versions",
                side_effect=UnsupportedLibraryVersion(
                    "bittensor 10.0.0 is installed, below the floor pyproject.toml declares "
                    "(bittensor>=11.1.0,<12.0.0)."
                ),
            ),
            mock.patch.object(validator_module, "load_settings", side_effect=must_not_run),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = validator_module.main([])

        self.assertEqual(code, 2)
        printed = out.getvalue()
        self.assertIn("ENVIRONMENT REFUSED", printed)
        self.assertIn("bittensor 10.0.0", printed)

    def test_a_healthy_environment_lets_main_proceed_to_settings(self):
        class ReachedSettings(Exception):
            pass

        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.object(validator_module, "load_settings", side_effect=ReachedSettings),
        ):
            with self.assertRaises(ReachedSettings):
                validator_module.main([])


class MainPassesLedgerPath(unittest.TestCase):
    def test_main_passes_the_ledger_path_to_build_chain_adapter(self):
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return mock.Mock()

        wallet = mock.Mock()
        wallet.hotkey.ss58_address = "5" + "H" * 47
        bt = mock.Mock()
        bt.wallet.return_value = wallet
        settings = mock.Mock(network="test", netuid=1, chain_endpoint="x",
                             wallet_name="w", wallet_hotkey="h", settle_blocks=1,
                             log_level="info")
        reader = mock.Mock()
        reader.current_block.return_value = 100
        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.object(validator_module, "load_settings", return_value=settings),
            mock.patch.object(validator_module, "connect", return_value=(object(), "0xg")),
            mock.patch.object(validator_module, "ChainReader", return_value=reader),
            mock.patch.object(validator_module, "gate", return_value=([("a", "ok")], 3)),
            mock.patch.object(validator_module, "build_chain_adapter", side_effect=fake_build),
            mock.patch.object(validator_module, "run_forever") as run_forever,
            mock.patch.object(validator_module, "_default_state_dir", return_value=Path("/st")),
            mock.patch.dict("sys.modules", {"bittensor": bt}),
            mock.patch.dict("os.environ", {}, clear=False),
        ):
            import os

            os.environ.pop("NOAH_COMMIT_LEDGER_PATH", None)
            with redirect_stdout(io.StringIO()):
                code = validator_module.main([])
        self.assertEqual(code, 0)
        run_forever.assert_called_once()
        self.assertEqual(captured["commit_ledger_path"], Path("/st/commit-ledger.json"))


class CommitLedgerPathTests(unittest.TestCase):
    """Issue #334: the sealed-commit ledger sits beside loop.json by default and
    NOAH_COMMIT_LEDGER_PATH overrides it."""

    def test_default_is_beside_the_state_file(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("NOAH_COMMIT_LEDGER_PATH", None)
            self.assertEqual(
                validator_module._commit_ledger_path(Path("/s")), Path("/s/commit-ledger.json")
            )

    def test_env_overrides(self):
        with mock.patch.dict("os.environ", {"NOAH_COMMIT_LEDGER_PATH": "/x/l.json"}):
            self.assertEqual(validator_module._commit_ledger_path(Path("/s")), Path("/x/l.json"))


def test_epoch_restart_waits_for_rate_limit_without_skipping_other_gates():
    settings = Settings.from_mapping({**GOOD_ENV, 'NOAH_PUBLISH_MODE': 'direct',
                                     'NOAH_EPOCH_REPUBLICATION': 'true'})
    with mock.patch.object(validator_module, 'check_can_publish_yet',
                           side_effect=validator_module.CannotPublishYet('wait 10 blocks')):
        results, _ = gate(Reader(), settings, OURS)
    outcomes = dict(results)
    assert outcomes['may publish yet'].startswith('WAIT')
    assert 'weights are accepted' in outcomes
    with mock.patch.object(validator_module, 'check_can_publish_yet',
                           side_effect=ChainUnreadable('disconnected')):
        results, _ = gate(Reader(), settings, OURS)
    assert dict(results)['may publish yet'].startswith('NO')
