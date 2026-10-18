"""Reading the chain for what this validator needs to know about itself.

The tests that matter here are about the difference between a read that said
"no" and a read that did not happen. Every one of these six facts, answered
wrongly in the permitting direction, lets the validator start when it should
not.
"""

import unittest

from ..reader import ChainReader, ChainUnreadable, Connection, unwrap


def node_down():
    """The real bittensor exception a dropped connection (or an exhausted
    endpoint pool, or an RPC-level failure of any kind) normalizes into,
    verified by tracing the installed client's own code
    (`bittensor._substrate.RpcSubstrate._read`; see reader.py's own module
    docstring, "A dropped connection is not the same fact as a bug in this
    code", for the full traced path). Every place in this file that used to
    fake "the chain could not be read" with a bare `RuntimeError` raises
    this instead (#510): `RuntimeError` is not one of the three types
    `noah.core.reader._transport_failure_types` now catches, so a bare
    `RuntimeError` would (correctly, post-#510) propagate as itself rather
    than becoming `ChainUnreadable` -- these tests need to prove the real
    distinction, not exercise a stand-in exception hierarchy nothing real
    ever raises.

    Deferred, matching this file's own existing convention for bittensor
    imports (`OwnerSeatsAreFoundByEitherRoute.chain` below, `reader.py`'s
    `_owner_hotkey_or_absent`): a dev extra, not a runtime dependency.

    Imported from `bittensor.result`, the module that genuinely defines it,
    not `bittensor` the top-level package -- matching reader.py's own
    `_transport_failure_types`, whose docstring has the full reasoning: the
    sibling worktree's signer audit matches an import's literal module
    string and does not resolve re-exports, so a bare `import bittensor as
    bt` here (caught 2026-08-29, part of #510's own regression) is a stray
    finding that precise `from bittensor.result import RpcConnectionError`
    is not.
    """
    from bittensor.result import RpcConnectionError

    return RpcConnectionError("node down")


class Wrapped:
    def __init__(self, value):
        self.value = value


class Client:
    def __init__(self, **answers):
        self.a = answers
        self.block = answers.get("block", 8_000)

    def query(self, item, params=None, block=None):
        key = item[1]
        if key in self.a.get("raises", ()):
            raise node_down()
        if key in self.a.get("raises_bug", ()):
            raise TypeError("validator called client with the wrong arguments")
        if block is not None and self.a.get("no_history"):
            raise node_down()
        if block is not None and self.a.get("no_history_bug"):
            raise TypeError("validator called client with the wrong arguments")
        return self.a.get(key)

    def query_map(self, item, params=None):
        key = item[1]
        if key in self.a.get("raises", ()):
            raise node_down()
        if key in self.a.get("raises_bug", ()):
            raise AttributeError("'NoneType' object has no attribute 'value'")
        return self.a.get(key, [])


class Unwrapping(unittest.TestCase):
    """One unwrap, defaulting to the result itself.

    `getattr(result, "value", None)` looks careful and discards every value that
    arrives bare. That produced three separate false results in one session, each
    a confident wrong answer rather than an error, so the property is held here
    rather than trusted to a convention.
    """

    def test_a_wrapped_value_is_unwrapped(self):
        self.assertEqual(unwrap(Wrapped(7)), 7)

    def test_a_bare_value_survives_instead_of_becoming_none(self):
        self.assertEqual(unwrap(7), 7)
        self.assertEqual(unwrap([1, 2, 3]), [1, 2, 3])

    def test_a_bare_empty_list_is_still_a_list(self):
        self.assertEqual(unwrap([]), [])

    def test_a_falsey_wrapped_value_is_not_mistaken_for_absence(self):
        self.assertEqual(unwrap(Wrapped(0)), 0)


class TheSixReads(unittest.TestCase):
    def test_a_hotkey_that_holds_a_seat_is_found(self):
        client = Client(Keys=[(Wrapped(0), Wrapped("5AAA")), (Wrapped(1), Wrapped("5BBB"))])
        self.assertEqual(ChainReader(client).seat_of("5BBB", 2), 1)

    def test_a_hotkey_that_holds_none_answers_none(self):
        client = Client(Keys=[(Wrapped(0), Wrapped("5AAA"))])
        self.assertIsNone(ChainReader(client).seat_of("5ZZZ", 2))

    def test_seat_zero_is_a_real_seat_and_not_an_absence(self):
        client = Client(Keys=[(Wrapped(0), Wrapped("5AAA"))])
        self.assertEqual(ChainReader(client).seat_of("5AAA", 2), 0)

    def test_the_activity_stamp_comes_from_this_seats_position(self):
        client = Client(LastUpdate=[100, 200, 300])
        self.assertEqual(ChainReader(client).last_update(1, 2), 200)

    def test_a_seat_past_the_end_of_the_list_refuses(self):
        # Reading position 9 of a three-entry list would otherwise raise
        # something obscure or, worse, wrap around.
        client = Client(LastUpdate=[100, 200, 300])
        with self.assertRaises(ChainUnreadable):
            ChainReader(client).last_update(9, 2)

    def test_the_rate_limit_and_the_sealed_flag_are_read_as_asked(self):
        client = Client(WeightsSetRateLimit=100, CommitRevealWeightsEnabled=True)
        reader = ChainReader(client)
        self.assertEqual(reader.rate_limit(2), 100)
        self.assertTrue(reader.commit_reveal(2))

    def test_sealed_publication_switched_off_reads_as_false(self):
        client = Client(CommitRevealWeightsEnabled=False)
        self.assertFalse(ChainReader(client).commit_reveal(2))

    def test_a_truthy_answer_that_is_not_true_is_passed_through_unchanged(self):
        """The same shape as subnet_exists's own identity check, and the same
        reason: `check_commit_reveal_enabled` downstream refuses anything that
        is not literally True. Every wire form of "not decoded" -- a string
        that merely looks false, a raw 1 that never went through a decoder --
        is truthy, so `bool(...)` here would turn every one of them into a
        real True and the downstream check would never see the difference.
        """
        for answer in ("false", "0", 1):
            with self.subTest(answer=answer):
                client = Client(CommitRevealWeightsEnabled=answer)
                self.assertEqual(ChainReader(client).commit_reveal(2), answer)
                self.assertIsNot(ChainReader(client).commit_reveal(2), True)


class AFailedReadIsNeverAPermittingAnswer(unittest.TestCase):
    def test_an_unreadable_rate_limit_refuses_rather_than_returning_zero(self):
        # Zero would read as "no limit" and let a validator publish into a wall.
        with self.assertRaises(ChainUnreadable):
            ChainReader(Client(raises=("WeightsSetRateLimit",))).rate_limit(2)

    def test_an_unreadable_sealed_flag_refuses_rather_than_returning_false(self):
        with self.assertRaises(ChainUnreadable):
            ChainReader(Client(raises=("CommitRevealWeightsEnabled",))).commit_reveal(2)

    def test_an_unreadable_seat_map_refuses_rather_than_reporting_no_seat(self):
        # seat_of and live_uids read the seat map with query_map rather than
        # query, a different client method _query never touches. Found by
        # adversarial review of #492 (codex exec, 2026-08-28): that method
        # called client.query_map directly, so a dropped connection here
        # escaped as a raw exception instead of becoming ChainUnreadable --
        # the same failure #492 fixed for query, just one call site over.
        # None is a real, permitting-adjacent answer ("holds no seat"), so a
        # read that merely failed must never produce it.
        with self.assertRaises(ChainUnreadable):
            ChainReader(Client(raises=("Keys",))).seat_of("5AAA", 2)

    def test_an_unreadable_seat_list_refuses_rather_than_returning_empty(self):
        # Same gap, same fix, the other query_map-based read: an empty list
        # would read as "no seats registered" rather than "could not tell".
        with self.assertRaises(ChainUnreadable):
            ChainReader(Client(raises=("Keys",))).live_uids(2)

    def test_an_unreadable_height_refuses(self):
        class NoBlock:
            @property
            def block(self):
                raise node_down()

        with self.assertRaises(ChainUnreadable):
            ChainReader(NoBlock()).current_block()


class EachRealTransportFailureTypeBecomesChainUnreadable(unittest.TestCase):
    """`_transport_failure_types` names three bittensor SDK types, not one.

    Every other test in this file that exercises "a real transport failure"
    uses `node_down()` (`bittensor.RpcConnectionError`), which regression-
    proofs that ONE of the three -- but `_query`'s except clause names all
    three, and a change that accidentally dropped one from the tuple, or
    mis-ordered the isinstance check, would not be caught by
    RpcConnectionError-only coverage. Coverage gap named directly by codex
    adversarial review (2026-08-29): "Only RpcConnectionError is injected.
    ChainError and RpcPolicyError are not tested."
    """

    def test_chain_error_becomes_chain_unreadable(self):
        # Imported from bittensor.result, the module that genuinely defines
        # it -- see node_down()'s docstring above and reader.py's
        # _transport_failure_types for why a bare `import bittensor` is a
        # stray finding against the sibling worktree's signer audit.
        from bittensor.result import ChainError

        class C:
            block = 8_000

            def query(self, item, params=None, block=None):
                raise ChainError("state discarded, no archive configured")

            def query_map(self, item, params=None):
                raise ChainError("state discarded, no archive configured")

        with self.assertRaises(ChainUnreadable):
            ChainReader(C()).rate_limit(2)
        with self.assertRaises(ChainUnreadable):
            ChainReader(C()).seat_of("5AAA", 2)

    def test_rpc_policy_error_becomes_chain_unreadable(self):
        from bittensor.result import RpcPolicyError

        class C:
            block = 8_000

            def query(self, item, params=None, block=None):
                raise RpcPolicyError("rate limited", status_code=429)

            def query_map(self, item, params=None):
                raise RpcPolicyError("rate limited", status_code=429)

        with self.assertRaises(ChainUnreadable):
            ChainReader(C()).rate_limit(2)
        with self.assertRaises(ChainUnreadable):
            ChainReader(C()).seat_of("5AAA", 2)

    def test_rpc_connection_error_becomes_chain_unreadable(self):
        # The third of the three, held here explicitly rather than only
        # implicitly through node_down()'s use elsewhere in this file.
        from bittensor.result import RpcConnectionError

        class C:
            block = 8_000

            def query(self, item, params=None, block=None):
                raise RpcConnectionError("endpoint unreachable")

            def query_map(self, item, params=None):
                raise RpcConnectionError("endpoint unreachable")

        with self.assertRaises(ChainUnreadable):
            ChainReader(C()).rate_limit(2)
        with self.assertRaises(ChainUnreadable):
            ChainReader(C()).seat_of("5AAA", 2)


class ARealBugPropagatesInsteadOfBecomingChainUnreadable(unittest.TestCase):
    """#510: a bare `except Exception` used to catch a validator code bug the
    same way it caught a dropped connection, so a `TypeError` from calling
    the client wrong, or an `AttributeError` from an unwrapped value whose
    shape was assumed, read as a clean "chain unreadable" refusal -- exactly
    as permitting-safe-looking as an actually dead connection, and just as
    silent. codex live-reproduced this against the real classes: a client
    whose query() raised TypeError, read through the real ChainReader into
    the real gate(), reported "subnet exists: NO" instead of crashing.

    Every case here is the same shape held closed, one call path at a time:
    the exception must come out of ChainReader exactly as the client raised
    it, not wrapped, so it is loud instead of plausible.
    """

    def test_a_query_based_read_lets_a_bug_through(self):
        with self.assertRaises(TypeError):
            ChainReader(Client(raises_bug=("WeightsSetRateLimit",))).rate_limit(2)

    def test_a_query_map_based_read_lets_a_bug_through(self):
        with self.assertRaises(AttributeError):
            ChainReader(Client(raises_bug=("Keys",))).seat_of("5AAA", 2)

    def test_current_block_lets_a_bug_through(self):
        class BuggyBlock:
            @property
            def block(self):
                raise TypeError("current_block called with the wrong arguments")

        with self.assertRaises(TypeError):
            ChainReader(BuggyBlock()).current_block()

    def test_a_storage_item_that_does_not_exist_propagates_rather_than_refusing(self):
        """The other named example in #510: StorageFunctionNotFound is a
        ValueError, raised by the real client for a storage item name or
        pallet that does not exist (a typo, a wrong module string), not for
        a dropped connection. Reproduced here at an ordinary call site (NOT
        `_owner_hotkey_or_absent`, which deliberately catches this one
        exception itself -- see OwnerSeatsAreFoundByEitherRoute below):
        before #510 this was caught by `_query`'s bare `except Exception`
        and silently became ChainUnreadable, indistinguishable from a real
        outage. It must now come out exactly as the client raised it.
        """
        from bittensor._transport.errors import StorageFunctionNotFound

        class WrongItemName:
            block = 8_000

            def query(self, item, params=None, block=None):
                raise StorageFunctionNotFound(item[1])

        with self.assertRaises(StorageFunctionNotFound):
            ChainReader(WrongItemName()).rate_limit(2)


class _WithoutBittensorImportable:
    """A context manager that makes ``import bittensor`` (and any
    ``bittensor.*`` submodule) fail with ``ModuleNotFoundError``, simulating
    the dependency-free `pip install .` `_transport_failure_types` and
    `_storage_function_not_found_type`'s own docstrings describe: bittensor
    is a dev extra, not a runtime dependency, and a plain install has no
    reason to have it.

    Both the already-cached modules and any future import attempt are
    blocked, so this exercises the real `except ModuleNotFoundError:`
    branch in reader.py rather than relying on bittensor never having been
    imported yet in this process (it may already be cached from an earlier
    test in this same file).
    """

    def __enter__(self):
        import builtins
        import sys

        self._real_import = builtins.__import__
        self._cached = {
            name: module
            for name, module in sys.modules.items()
            if name.split(".")[0] == "bittensor"
        }
        for name in self._cached:
            del sys.modules[name]

        def blocked_import(name, *args, **kwargs):
            if name == "bittensor" or name.startswith("bittensor."):
                raise ModuleNotFoundError(f"No module named {name!r} (blocked for this test)")
            return self._real_import(name, *args, **kwargs)

        builtins.__import__ = blocked_import
        return self

    def __exit__(self, *exc_info):
        import builtins
        import sys

        builtins.__import__ = self._real_import
        sys.modules.update(self._cached)
        return False


class _WithBittensorExportsMissing:
    """Swaps in a stripped bittensor module tree that has NEITHER
    ChainError/RpcConnectionError/RpcPolicyError NOR
    StorageFunctionNotFound -- simulating a MISMATCHED install (bittensor
    is present, but some other build whose modules don't export these
    names), as opposed to `_WithoutBittensorImportable` above, which
    simulates bittensor being absent entirely.

    The distinction matters because the two failure shapes are different
    Python exceptions: `from bittensor.result import ChainError` against a
    module that has no such attribute raises `ImportError`, not
    `ModuleNotFoundError` -- a general CPython import-system property, not
    specific to bittensor, pinned generically (against an unrelated stdlib
    module, not bittensor itself) by
    `WithBittensorPresentButMissingTheExpectedExports`'s sibling check
    below; see reader.py's own docstring for the reasoning this class
    exists to exercise. Caught by codex adversarial review (2026-08-29): the
    narrower `except ModuleNotFoundError` reader.py shipped with left
    exactly this case free to replace whatever exception was being matched.

    `bittensor.result` is stubbed here too, empty of the same three names,
    since 2026-08-29: `_transport_failure_types` now imports
    ChainError/RpcConnectionError/RpcPolicyError from `bittensor.result`
    directly rather than from the top-level package (see that function's own
    docstring for why), so a mismatched install missing the exports has to
    be simulated at the module those names are actually imported from for
    this class to still exercise the scenario its own docstring claims.
    """

    _MODULE_NAMES = (
        "bittensor",
        "bittensor._transport",
        "bittensor._transport.errors",
        "bittensor.result",
    )

    def __enter__(self):
        import sys
        import types

        self._saved = {name: sys.modules.get(name) for name in self._MODULE_NAMES}

        fake_bittensor = types.ModuleType("bittensor")
        fake_transport = types.ModuleType("bittensor._transport")
        fake_transport_errors = types.ModuleType("bittensor._transport.errors")
        fake_result = types.ModuleType("bittensor.result")
        # Deliberately no ChainError / RpcConnectionError / RpcPolicyError /
        # StorageFunctionNotFound attributes anywhere in this fake tree.
        fake_bittensor._transport = fake_transport
        fake_bittensor.result = fake_result
        fake_transport.errors = fake_transport_errors

        sys.modules["bittensor"] = fake_bittensor
        sys.modules["bittensor._transport"] = fake_transport
        sys.modules["bittensor._transport.errors"] = fake_transport_errors
        sys.modules["bittensor.result"] = fake_result
        return self

    def __exit__(self, *exc_info):
        import sys

        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        return False


class WithoutBittensorInstalledAtAll(unittest.TestCase):
    """#510's own fix introduced a second bug, caught by codex review against
    commit 195ad2d: `_transport_failure_types` and
    `_storage_function_not_found_type` both import bittensor from inside an
    `except ... as error:` clause, which Python only evaluates once an
    exception actually needs matching -- but if bittensor cannot be
    imported at all (a plain `pip install .`, no dev extras), evaluating
    that clause raises `ModuleNotFoundError`, and an except clause's own
    expression raising REPLACES whatever exception was being matched. A
    genuine validator bug reaching one of these sites, on an installation
    that never had bittensor in the first place, would surface as a
    confusing "no module named bittensor" instead of the real bug -- worse
    than #510's original defect, not better.

    Both helpers now catch `ImportError` themselves (a superclass of
    `ModuleNotFoundError`, so both the "not installed at all" shape this
    class exercises and the "installed but missing the export" shape
    `WithBittensorPresentButMissingTheExpectedExports` below exercises are
    covered by the one clause) and return an empty tuple (`except ():`
    catches nothing -- verified directly in this class), so the ORIGINAL
    exception keeps propagating unchanged.
    """

    def test_empty_tuple_via_except_catches_nothing(self):
        # The mechanism this fix relies on, pinned directly: except () must
        # not swallow anything, or "catch nothing" would silently become
        # "catch everything" the moment Python's own behavior here changed.
        with self.assertRaises(TypeError):
            try:
                raise TypeError("must not be caught")
            except ():
                pass  # pragma: no cover -- must never be reached

    def test_a_real_bug_still_propagates_as_itself_with_bittensor_unimportable(self):
        with _WithoutBittensorImportable():
            with self.assertRaises(TypeError):
                ChainReader(Client(raises_bug=("WeightsSetRateLimit",))).rate_limit(2)

    def test_a_successful_owner_hotkey_read_needs_no_bittensor_at_all(self):
        # _owner_hotkey_or_absent's own docstring states this contract: an
        # ordinary successful read must not need bittensor importable, since
        # it is a dev extra a plain `pip install .` may not have.
        class C:
            block = 8_000

            def query(self, item, params=None, block=None):
                return "5HOTKEY"

            def query_map(self, item, params=None):
                return []

        with _WithoutBittensorImportable():
            self.assertEqual(ChainReader(C())._owner_hotkey_or_absent(2), "5HOTKEY")

    def test_a_genuinely_absent_storage_item_cannot_be_told_apart_without_bittensor(self):
        # A documented, accepted limit, not a silent gap: without bittensor
        # importable, `_storage_function_not_found_type` cannot even name
        # the type it would need to catch, so a StorageFunctionNotFound
        # raised by SOME duck-typed client in this situation propagates
        # raw rather than resolving to None. That is the same "refuse
        # rather than guess" choice `_transport_failure_types` makes, held
        # to the one case where refusing looks like a stricter answer
        # (an exception) rather than a laxer one.
        class C:
            block = 8_000

            def query(self, item, params=None, block=None):
                raise ValueError("SubnetOwnerHotkey")

            def query_map(self, item, params=None):
                return []

        with _WithoutBittensorImportable():
            with self.assertRaises(ValueError):
                ChainReader(C())._owner_hotkey_or_absent(2)


class WithBittensorPresentButMissingTheExpectedExports(unittest.TestCase):
    """The other half of the same import-failure gap, and the one codex
    adversarial review actually found live (2026-08-29): bittensor need not
    be ABSENT to trigger it. A present-but-mismatched build -- some other
    version whose `bittensor/result.py` does not export `ChainError`,
    `RpcConnectionError`, or `RpcPolicyError`, or whose
    `bittensor/_transport/errors.py` does not export
    `StorageFunctionNotFound` -- raises a plain `ImportError` from
    `from bittensor.result import ChainError`, not `ModuleNotFoundError`.
    The two helpers in reader.py originally caught only the narrower type,
    so this exact case still replaced a real bug with a confusing import
    failure even after WithoutBittensorInstalledAtAll's own scenario was
    fixed.

    The ImportError-vs-ModuleNotFoundError split itself is a general
    CPython property, not something specific to bittensor -- the sibling
    check below pins it against `json`, a harmless standard-library module,
    rather than against bittensor directly. Caught by codex review
    (2026-08-29): an earlier version of that check imported a deliberately
    nonexistent name straight from bittensor and hid the resulting
    `ast.ImportFrom` statement from the sibling worktree's signer audit by
    routing it through `exec()`, which -- even for a name that can never
    resolve to anything -- is exactly the bypass pattern that audit exists
    to catch.
    """

    def test_a_missing_export_raises_importerror_not_modulenotfounderror(self):
        # The Python behavior this whole fix rests on: an absent NAME
        # (module present, attribute missing) raises ImportError; an absent
        # MODULE raises ModuleNotFoundError. A CPython import-system
        # property, not something specific to bittensor, so it is pinned
        # here against `json` -- an ordinary standard-library module with no
        # bearing on this validator's signing boundary -- rather than
        # against bittensor itself.
        #
        # Not exec()-ed and not imported from bittensor: an earlier version
        # of this test did `exec("from bittensor import
        # ThisNameDoesNotExistInBittensor", {})`, reaching for exec()
        # specifically because a literal `from bittensor import <name>`
        # statement is a real `ast.ImportFrom` node the sibling worktree's
        # signer audit can see. That name could never resolve to anything,
        # but routing an import around that scan with exec() -- for any
        # reason, even a harmless one -- IS the bypass pattern the audit
        # exists to catch, and a test in this very suite demonstrating it is
        # a bad precedent to leave standing. Caught by codex review
        # (2026-08-29). Using an unrelated stdlib module proves the same
        # general Python behavior without touching bittensor, the audit, or
        # exec() at all.
        with self.assertRaises(ImportError) as caught:
            from json import ThisNameDoesNotExistInJson  # noqa: F401
        self.assertNotIsInstance(caught.exception, ModuleNotFoundError)

    def test_a_real_bug_still_propagates_with_a_mismatched_bittensor_present(self):
        with _WithBittensorExportsMissing():
            with self.assertRaises(TypeError):
                ChainReader(Client(raises_bug=("WeightsSetRateLimit",))).rate_limit(2)

    def test_a_successful_owner_hotkey_read_needs_no_matching_exports_either(self):
        class C:
            block = 8_000

            def query(self, item, params=None, block=None):
                return "5HOTKEY"

            def query_map(self, item, params=None):
                return []

        with _WithBittensorExportsMissing():
            self.assertEqual(ChainReader(C())._owner_hotkey_or_absent(2), "5HOTKEY")


class TheHistoryProbeAsksForStateNotForABlock(unittest.TestCase):
    """A pruned node serves old events and refuses old state.

    Asking the cheaper question is exactly how a node that cannot serve the
    history this validator needs would pass the check meant to catch it.
    """

    def test_a_node_that_answers_at_depth_passes(self):
        self.assertTrue(ChainReader(Client(block=8_000, Tempo=20)).historical_probe(300))

    def test_a_node_that_cannot_answer_at_depth_fails(self):
        self.assertFalse(ChainReader(Client(block=8_000, no_history=True)).historical_probe(300))

    def test_a_bug_in_the_probes_own_read_propagates_rather_than_reading_as_pruned(self):
        # #510's historical_probe site (reader.py's inner `except Exception:
        # return False`, ~line 158 before the fix): a real transport
        # failure here correctly reads as "cannot serve history" (tested
        # above), but a genuine code bug must not be folded into the same
        # False -- that would report a validator bug as a node limitation.
        with self.assertRaises(TypeError):
            ChainReader(Client(block=8_000, no_history_bug=True)).historical_probe(300)

    def test_a_chain_younger_than_the_depth_asked_for_passes(self):
        # Nothing is wrong with the node; the history simply does not exist yet.
        self.assertTrue(ChainReader(Client(block=10, no_history=True)).historical_probe(300))

    def test_a_node_that_answers_with_none_instead_of_raising_fails(self):
        # The exact pruned-node shape this probe exists for: the call
        # completes with no exception at all, and only the VALUE it came
        # back with says the read did not really happen.
        self.assertFalse(ChainReader(Client(block=8_000, Tempo=None)).historical_probe(300))

    def test_a_node_that_answers_with_a_default_zero_instead_of_raising_fails(self):
        self.assertFalse(ChainReader(Client(block=8_000, Tempo=0)).historical_probe(300))

    def test_a_wrapped_empty_answer_is_unwrapped_before_it_is_judged(self):
        client = Client(block=8_000, Tempo=Wrapped(None))
        self.assertFalse(ChainReader(client).historical_probe(300))


if __name__ == "__main__":
    unittest.main()


class OwnerSeatsAreFoundByEitherRoute(unittest.TestCase):
    """A seat is the owner's by its coldkey OR by being the owner hotkey.

    Two different facts, either one enough. Checking only the coldkey is the
    tempting half and it misses an owner hotkey registered under some other
    coldkey. A miss does not error; it routes emission to the one place this
    project states in absolute terms it must never go.
    """

    def chain(
        self,
        keys,
        owners,
        owner_coldkey="5OWNER",
        owner_hotkey=None,
        hotkey_read_fails=None,
        keys_read_fails=None,
    ):
        # Deferred, matching this codebase's own convention for bittensor
        # imports (noah/core/submitter.py never imports it at module level
        # either): this is the one real exception class reader.py's
        # _owner_hotkey_or_absent recognises as genuine absence, so the test
        # has to raise the SAME class, not a stand-in.
        from bittensor._transport.errors import StorageFunctionNotFound

        class C:
            block = 8_000

            def query(self, item, params=None, block=None):
                key = item[1]
                if key == "SubnetOwner":
                    return owner_coldkey
                if key == "SubnetOwnerHotkey":
                    if hotkey_read_fails is not None:
                        raise hotkey_read_fails
                    if owner_hotkey is None:
                        # Genuine absence: this runtime's metadata has no such
                        # storage item at all (an old spec version). This is
                        # the ONE signal that "no owner hotkey known" is the
                        # honest answer, verified against the installed chain
                        # client (bittensor 11.1.0,
                        # bittensor/_transport/codec.py:261): every other
                        # failure means the item exists and this read merely
                        # did not work.
                        raise StorageFunctionNotFound("SubnetOwnerHotkey")
                    return owner_hotkey
                if key == "Owner":
                    return owners[params[0]]
                raise RuntimeError(key)

            def query_map(self, item, params=None):
                if keys_read_fails is not None:
                    raise keys_read_fails
                return [(Wrapped(u), Wrapped(h)) for u, h in keys]

        return ChainReader(C())

    def test_a_seat_whose_coldkey_is_the_owners_is_found(self):
        reader = self.chain([(0, "5HK"), (1, "5MINER")], {"5HK": "5OWNER", "5MINER": "5SOMEONE"})
        self.assertEqual(reader.owner_uids(2), {0})

    def test_a_seat_whose_hotkey_is_the_owner_hotkey_is_found(self):
        """The half a coldkey-only check misses.

        Here the owner hotkey sits under a coldkey that is NOT the owner's, so
        the coldkey route says nothing and the seat is still the owner's.
        """
        reader = self.chain(
            [(0, "5HK"), (1, "5MINER")],
            {"5HK": "5ELSEWHERE", "5MINER": "5SOMEONE"},
            owner_hotkey="5HK",
        )
        self.assertEqual(reader.owner_uids(2), {0})

    def test_a_seat_satisfying_both_routes_is_counted_once(self):
        reader = self.chain([(0, "5HK")], {"5HK": "5OWNER"}, owner_hotkey="5HK")
        self.assertEqual(reader.owner_uids(2), {0})

    def test_seats_the_owner_does_not_control_are_not_swept_in(self):
        # The opposite failure: calling everything the owner's would zero the
        # whole subnet's pay while looking cautious.
        reader = self.chain(
            [(0, "5HK"), (1, "5A"), (2, "5B")], {"5HK": "5OWNER", "5A": "5X", "5B": "5Y"}
        )
        self.assertEqual(reader.owner_uids(2), {0})

    def test_a_runtime_without_the_owner_hotkey_item_still_uses_the_coldkey(self):
        reader = self.chain([(0, "5HK")], {"5HK": "5OWNER"}, owner_hotkey=None)
        self.assertEqual(reader.owner_uids(2), {0})

    def test_an_unrelated_read_failure_refuses_rather_than_assuming_absence(self):
        """A read that merely failed is not the same fact as "no such item".

        A runtime that HAS SubnetOwnerHotkey can still fail to answer it for a
        reason that has nothing to do with whether an owner hotkey is set: a
        timeout, a dropped connection, a decode error. Treating that the same
        as genuine absence risks missing a real owner hotkey registered under
        some other coldkey, which is exactly the seat this guard exists to
        catch. Here the coldkey route also finds nothing, so a version of
        this that silently fell back to "no owner hotkey" would return an
        empty set instead of refusing.
        """
        reader = self.chain([(0, "5HK")], {"5HK": "5ELSEWHERE"}, hotkey_read_fails=node_down())
        with self.assertRaises(ChainUnreadable):
            reader.owner_uids(2)

    def test_owner_uid_singular_finds_the_owners_seat_by_hotkey(self):
        # The same absence handling exists a second time, in owner_uid
        # (singular), used by the validator permit exemption
        # (startup.py:157). It has no coldkey route at all, so the hotkey
        # read is the only thing standing between it and a wrong answer.
        reader = self.chain([(0, "5HK"), (1, "5MINER")], {}, owner_hotkey="5HK")
        self.assertEqual(reader.owner_uid(2), 0)

    def test_owner_uid_singular_answers_none_when_the_runtime_has_no_such_item(self):
        reader = self.chain([(0, "5HK")], {}, owner_hotkey=None)
        self.assertIsNone(reader.owner_uid(2))

    def test_owner_uid_singular_refuses_on_an_unrelated_read_failure_too(self):
        reader = self.chain([(0, "5HK")], {}, hotkey_read_fails=node_down())
        with self.assertRaises(ChainUnreadable):
            reader.owner_uid(2)

    def test_live_seats_come_back_in_order(self):
        reader = self.chain([(2, "5C"), (0, "5A"), (1, "5B")], {})
        self.assertEqual(reader.live_uids(2), [0, 1, 2])

    def test_the_seat_map_itself_failing_refuses_rather_than_assuming_no_owner(self):
        # test_an_unrelated_read_failure_refuses_rather_than_assuming_absence
        # above covers the hotkey read failing; this is the OTHER read both
        # owner_uids and owner_uid depend on, the seat map query_map itself
        # reads to find every seat. A version that let this escape would read
        # a dropped connection as "the owner controls no seats" instead of
        # refusing.
        reader = self.chain([(0, "5HK")], {"5HK": "5OWNER"}, keys_read_fails=node_down())
        with self.assertRaises(ChainUnreadable):
            reader.owner_uids(2)

    def test_owner_uid_singular_refuses_when_the_seat_map_itself_fails(self):
        reader = self.chain([(0, "5HK")], {}, owner_hotkey="5HK", keys_read_fails=node_down())
        with self.assertRaises(ChainUnreadable):
            reader.owner_uid(2)


class WhoCommittedInAPeriod(unittest.TestCase):
    """Read from events, not from the commit storage.

    The storage is emptied by the reveal, so a settled period always reads as a
    period nobody published in. That is the answer that matters most and the one
    storage cannot give.

    Events are built with the real shape, verified against
    subtensor/pallets/subtensor/src/subnets/weights.rs:352-357 and
    macros/events.rs:260,419: the runtime emits either
    ``WeightsCommitted(hotkey, netuid, commit_hash)`` (the plain
    commit-then-manually-reveal path) or ``TimelockedWeightsCommitted(hotkey,
    netuid, commit_hash, reveal_round)`` (the drand-timelocked path this
    validator itself uses), and both are gated by the same
    CommitRevealDisabled check (weights.rs:70-73, :300-303) so either can
    appear on a subnet with sealed publication on. Both are nested the same
    way weights_set_for_seat's events are, so the netuid at attributes[1] is
    checked the same structural way rather than by pattern-matching text.
    """

    OURS = "5" + "A" * 47
    THEIRS = "5" + "B" * 47

    @staticmethod
    def committed(hotkey, netuid=2, event_id="TimelockedWeightsCommitted"):
        attributes = (
            [hotkey, netuid, "0xHASH", 7]
            if event_id == "TimelockedWeightsCommitted"
            else [hotkey, netuid, "0xHASH"]
        )
        return {"event": {"event_id": event_id, "attributes": attributes}}

    @staticmethod
    def noise():
        return {"event": {"event_id": "SomethingElse", "attributes": [1, 2]}}

    def chain(self, per_block, events_per_block=3, netuid=2, unreadable_blocks=(), buggy_blocks=()):
        outer = self

        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if item[1] != "Events":
                    raise RuntimeError(item[1])
                if block in buggy_blocks:
                    raise TypeError("committed_in_period called the client wrong")
                if block in unreadable_blocks:
                    raise node_down()
                names = per_block.get(block, [])
                filler = [outer.noise()] * events_per_block
                return filler + [outer.committed(h, netuid) for h in names]

            def query_map(self, item, params=None):
                return []

        return ChainReader(C())

    def test_a_committer_in_the_period_is_found(self):
        reader = self.chain({8_425: [self.OURS]})
        self.assertEqual(reader.committed_in_period(2, 421, 20), {self.OURS})

    def test_a_period_nobody_published_in_comes_back_empty(self):
        reader = self.chain({})
        self.assertEqual(reader.committed_in_period(2, 421, 20), set())

    def test_somebody_else_publishing_stays_distinguishable_from_nobody(self):
        # Collapsing these to a boolean is how this silently stops working the
        # day a second validator joins the subnet.
        reader = self.chain({8_425: [self.THEIRS]})
        self.assertEqual(reader.committed_in_period(2, 421, 20), {self.THEIRS})
        self.assertEqual(reader.committed_in_period(2, 421, 20, hotkey=self.OURS), set())

    def test_filtering_to_our_hotkey_finds_ours_among_others(self):
        reader = self.chain({8_425: [self.THEIRS], 8_430: [self.OURS]})
        self.assertEqual(reader.committed_in_period(2, 421, 20, hotkey=self.OURS), {self.OURS})

    def test_only_the_periods_own_blocks_are_read(self):
        # A commit one block outside must not be counted, or every period
        # inherits its neighbour's publication.
        reader = self.chain({8_419: [self.OURS], 8_440: [self.OURS]})
        self.assertEqual(reader.committed_in_period(2, 421, 20), set())

    def test_a_reader_that_saw_no_events_at_all_refuses(self):
        """A producing chain always has events. Zero means a broken reader.

        Reporting "nobody published" from a reader that reached nothing is how
        a healthy publication gets called missing, and it has happened here.
        """
        reader = self.chain({}, events_per_block=0)
        with self.assertRaises(ChainUnreadable) as caught:
            reader.committed_in_period(2, 421, 20)
        self.assertIn("reached nothing", str(caught.exception))

    def test_the_plain_commit_path_is_recognised_too_not_only_timelocked(self):
        """Two extrinsics reach this event stream, gated by the same flag.

        This validator only ever commits the timelocked way, but another
        validator on the same subnet could legitimately use the plain
        commit-then-manually-reveal path instead: both require the same
        CommitRevealDisabled check to pass, so neither implies the other is
        absent. Missing the plain event here would mean a real commit from
        somebody else reads as nobody having published.
        """
        event = self.committed(self.THEIRS, netuid=2, event_id="WeightsCommitted")

        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if item[1] != "Events":
                    raise RuntimeError(item[1])
                return [event] if block == 8_425 else []

            def query_map(self, item, params=None):
                return []

        reader = ChainReader(C())
        self.assertEqual(reader.committed_in_period(2, 421, 20), {self.THEIRS})

    def test_a_commit_on_a_different_subnet_is_not_ours(self):
        """The netuid parameter has to actually be checked against the event.

        A hotkey active on more than one subnet can commit on BOTH in the same
        block range. Without comparing the event's own netuid to the one
        asked about, a commit made on a different subnet reads as a
        publication on this one.
        """
        reader = self.chain({8_425: [self.OURS]}, netuid=7)
        self.assertEqual(reader.committed_in_period(2, 421, 20), set())

    def test_a_commit_under_a_non_zero_mechanism_is_still_this_subnets(self):
        """The event's subnet field is netuid + mechanism * 4096, not a netuid.

        Subnet 2, mechanism 1 packs to storage index 4098 (2 + 1*4096). A
        comparison against the packed field directly would read this as
        belonging to some other subnet and silently miss it as subnet 2's own
        commit, which is exactly the class of bug netuid_and_mechanism exists
        to prevent (noah/chain/emissions.py, already relied on for pay
        events).
        """
        packed = 2 + 1 * 4096  # netuid 2, mechanism 1
        reader = self.chain({8_425: [self.OURS]}, netuid=packed)
        self.assertEqual(reader.committed_in_period(2, 421, 20), {self.OURS})

    def test_a_block_that_fails_to_read_refuses_even_though_others_answered(self):
        """A partially read period must not look like a complete one.

        Block 8,425 is exactly where the commit landed, and it is the one
        block that fails here. Every OTHER block in the period answers fine,
        so events_seen never reaches zero: without treating an unread block
        as making the whole period incomplete, this would silently report
        "nobody published" from a read that never actually reached the block
        that mattered.
        """
        reader = self.chain({8_425: [self.OURS]}, unreadable_blocks={8_425})
        with self.assertRaises(ChainUnreadable) as caught:
            reader.committed_in_period(2, 421, 20)
        self.assertIn("8425", str(caught.exception).replace(",", ""))

    def test_a_below_head_read_failure_still_refuses_when_head_is_given(self):
        """`head` must not weaken the refusal above. Identical fixture to
        the unbounded test above -- a real failure on block 8,425, which the
        given head (12,000) sits far past -- proving the new parameter
        changes nothing about a block that has already been produced and
        genuinely failed to read."""
        reader = self.chain({8_425: [self.OURS]}, unreadable_blocks={8_425})
        with self.assertRaises(ChainUnreadable) as caught:
            reader.committed_in_period(2, 421, 20, head=12_000)
        self.assertIn("8425", str(caught.exception).replace(",", ""))

    def test_an_in_progress_period_extending_past_head_does_not_refuse(self):
        """The actual bug this `head` parameter fixes. Period 421 (blocks
        8,420-8,439 at tempo 20) is still in progress: only up to block
        8,430 has been produced. Querying a block past the live head on a
        real chain raises the identical transport failure a genuinely
        unreadable block does -- indistinguishable from the outside, which
        is exactly why `unreadable_blocks` (used above for a REAL failure)
        is reused here to simulate blocks 8,431-8,439 not existing yet.
        Before this fix, `committed_in_period` had no way to tell "does not
        exist yet" apart from "failed to read" and refused every time a
        caller checked the current period -- live-reproduced as a validator
        stuck re-cycling the same check for over 100 minutes. With `head`
        given, those not-yet-produced blocks are never queried at all, and
        a commit sitting in an already-produced block (8,425, below head)
        is still found."""
        reader = self.chain({8_425: [self.OURS]}, unreadable_blocks=set(range(8_431, 8_440)))
        self.assertEqual(reader.committed_in_period(2, 421, 20, head=8_430), {self.OURS})
        # The same fixture, without a head bound, reproduces the original
        # bug: those identical not-yet-produced blocks get scanned and
        # refused, exactly like the genuine failure two tests above --
        # proof this test would have failed before the fix.
        with self.assertRaises(ChainUnreadable):
            reader.committed_in_period(2, 421, 20)

    def test_a_bug_reading_one_block_propagates_rather_than_joining_unread_blocks(self):
        # #510's site in this method (reader.py's per-block `except
        # Exception as error: unread_blocks.append(...)`, ~line 304 before
        # the fix): a real transport failure on one block correctly folds
        # into "this period was read incompletely" (tested above), but a
        # genuine code bug reading that block must not quietly become one
        # more unread block among many -- it has to stop the scan and
        # surface loudly instead.
        reader = self.chain({8_425: [self.OURS]}, buggy_blocks={8_425})
        with self.assertRaises(TypeError):
            reader.committed_in_period(2, 421, 20)

    def test_a_block_answering_none_without_raising_refuses_too(self):
        """A non-exception answer that is not a list of events is not silence.

        The chain client can return None (or some other malformed answer)
        for a block WITHOUT raising -- a single real endpoint has been
        measured returning a shortened event list this same way, no error,
        for over half a day (chain/agreeing.py). The commit that matters is
        sitting in exactly the block that answers None here; a version that
        only checked `except Exception` would silently read it as an empty
        block rather than a suspicious one.
        """

        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if item[1] != "Events":
                    raise RuntimeError(item[1])
                if block == 8_425:
                    return None
                return [self.noise_event()] * 3

            @staticmethod
            def noise_event():
                return {"event": {"event_id": "SomethingElse", "attributes": [1, 2]}}

            def query_map(self, item, params=None):
                return []

        reader = ChainReader(C())
        with self.assertRaises(ChainUnreadable):
            reader.committed_in_period(2, 421, 20)


class ASubnetThatWasNeverCreated(unittest.TestCase):
    """Almost nothing else can tell you, which is what makes this dangerous.

    Measured live on 2026-08-24 against netuid 999, which does not exist on that
    chain: the rate limit, the activity factor, the deprecated cutoff and the
    sealed-publication flag all answered IDENTICALLY to a real subnet, and the
    period length answered a different but perfectly plausible number. A
    validator pointed at a mistyped subnet reads an ordinary configuration.
    """

    def reader(self, added):
        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if item[1] == "NetworksAdded":
                    return added
                # Everything else answers the same for both, which is the point.
                return {
                    "WeightsSetRateLimit": 100,
                    "CommitRevealWeightsEnabled": True,
                    "ActivityCutoffFactorMilli": 13_889,
                    "Tempo": 360,
                }[item[1]]

            def query_map(self, item, params=None):
                return []

        return ChainReader(C())

    def test_a_created_subnet_reads_as_existing(self):
        self.assertTrue(self.reader(True).subnet_exists(2))

    def test_a_truthy_answer_that_is_not_true_does_not_pass(self):
        """Truthiness is not the test here, and this is where it costs most.

        Every wire form of "missing" is a non-empty string and a non-empty
        string is truthy. An answer that arrived undecoded, a wrapper that did
        not unwrap, or a 1 that never went through a decoder are all truthy and
        none of them is True. This is the first question the gate asks, so a
        false pass means everything after it is read against a subnet that may
        not exist, and those reads all answer confidently.

        Raised by NOAH-BUILD-LEAD from the same shape in their own tree.
        """
        for answer in ("unknown", "false", 1, [True], {"value": True}):
            with self.subTest(answer=answer):
                self.assertFalse(self.reader(answer).subnet_exists(2))

    def test_a_subnet_that_was_never_created_reads_as_not_existing(self):
        self.assertFalse(self.reader(False).subnet_exists(999))

    def test_every_other_setting_answers_the_same_for_both(self):
        # The reason the existence check has to be its own read. Held as a test
        # so nobody later decides one of these could stand in for it.
        real, phantom = self.reader(True), self.reader(False)
        self.assertEqual(real.rate_limit(2), phantom.rate_limit(999))
        self.assertEqual(real.commit_reveal(2), phantom.commit_reveal(999))
        self.assertTrue(phantom.commit_reveal(999))
        self.assertEqual(
            real.activity_cutoff_factor_milli(2), phantom.activity_cutoff_factor_milli(999)
        )


class WhoseWeightsWereApplied(unittest.TestCase):
    """The seat matters, and leaving it out is invisible on a one-validator subnet.

    The runtime emits WeightsSet(netuid_index, uid), so the event names whose
    weights landed. A check that asks only whether ANY weights were set reports
    our commit as applied when somebody else's was, and the verdict built on it
    excuses a validator that did nothing.

    This is the same fault evidence.py warns about on the commit side. It was
    reproduced on the applied side and survived because this subnet has had one
    publishing validator.
    """

    def chain(self, events_by_block, unreadable_blocks=(), buggy_blocks=()):
        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if block in buggy_blocks:
                    raise TypeError("weights_set_for_seat called the client wrong")
                if block in unreadable_blocks:
                    raise node_down()
                return events_by_block.get(block, [])

            def query_map(self, item, params=None):
                return []

        return ChainReader(C())

    @staticmethod
    def weights_set(uid, netuid_index=2):
        return {"event": {"event_id": "WeightsSet", "attributes": [netuid_index, uid]}}

    @staticmethod
    def noise():
        return {"event": {"event_id": "SomethingElse", "attributes": [1, 2]}}

    def test_this_seats_application_is_found(self):
        reader = self.chain({8_463: [self.noise(), self.weights_set(0)]})
        self.assertIs(reader.weights_set_for_seat(2, 0, 8_460, 8_470), True)

    def test_another_seats_application_is_not_ours(self):
        # The bug, held closed. Without the uid check this returns True.
        reader = self.chain({8_463: [self.weights_set(7)]})
        self.assertIs(reader.weights_set_for_seat(2, 0, 8_460, 8_470), False)

    def test_ours_is_found_among_several_seats_applying(self):
        reader = self.chain({8_463: [self.weights_set(7), self.weights_set(0)]})
        self.assertIs(reader.weights_set_for_seat(2, 0, 8_460, 8_470), True)

    def test_a_window_with_no_application_reads_false(self):
        reader = self.chain({block: [self.noise()] for block in range(8_460, 8_471)})
        self.assertIs(reader.weights_set_for_seat(2, 0, 8_460, 8_470), False)

    def test_only_blocks_in_the_window_are_looked_at(self):
        """An application outside the window is not this window's.

        The in-window blocks carry ordinary traffic, because a block with no
        events at all means the reader reached nothing, and that answers
        not-known rather than not-applied. The first version of this test gave
        them nothing and asserted False, which was asserting the wrong thing.
        """
        events = {block: [self.noise()] for block in range(8_460, 8_471)}
        events[8_500] = [self.weights_set(0)]
        self.assertIs(self.chain(events).weights_set_for_seat(2, 0, 8_460, 8_470), False)

    def test_a_reader_that_saw_nothing_answers_neither_true_nor_false(self):
        """Not-known is not the same as not-applied.

        Reporting a failed read as "not applied" turns it into a declared
        validator failure, and a declared failure excuses every miner on the
        subnet for that day.
        """
        self.assertIsNone(self.chain({}).weights_set_for_seat(2, 0, 8_460, 8_470))

    def test_a_seat_matching_uid_on_a_different_subnet_is_not_ours(self):
        """The netuid in WeightsSet(netuid_index, uid) has to be checked too.

        The same uid can hold a seat on more than one subnet. Without
        checking netuid, a WeightsSet event from a DIFFERENT subnet naming
        this uid reads as this subnet's own application.
        """
        reader = self.chain({8_463: [self.weights_set(0, netuid_index=7)]})
        self.assertIs(reader.weights_set_for_seat(2, 0, 8_460, 8_470), False)

    def test_an_application_under_a_non_zero_mechanism_is_still_this_subnets(self):
        """netuid_index is netuid + mechanism * 4096, not a plain netuid.

        Subnet 2, mechanism 1 packs to storage index 4098. A comparison
        against the packed field directly would read this as some other
        subnet's application and silently miss subnet 2's own (the class of
        bug noah/chain/emissions.py's netuid_and_mechanism already exists to
        prevent, for pay events).
        """
        packed = 2 + 1 * 4096  # netuid 2, mechanism 1
        reader = self.chain({8_463: [self.weights_set(0, netuid_index=packed)]})
        self.assertIs(reader.weights_set_for_seat(2, 0, 8_460, 8_470), True)

    def test_a_block_that_fails_to_read_refuses_even_though_others_answered(self):
        """A partially read window must not look like a complete one.

        Block 8,463 is exactly where the application landed, and it is the
        one block that fails here. The rest of the window answers fine, so
        `seen` never reaches zero: without treating an unread block as
        making the whole window incomplete, this would silently report
        "not applied" from a read that never reached the block that
        mattered.
        """
        reader = self.chain({8_463: [self.weights_set(0)]}, unreadable_blocks={8_463})
        with self.assertRaises(ChainUnreadable):
            reader.weights_set_for_seat(2, 0, 8_460, 8_470)

    def test_a_bug_reading_one_block_propagates_rather_than_joining_unread_blocks(self):
        # #510's site in this method (reader.py's per-block `except
        # Exception as error: unread_blocks.append(...)`, ~line 440 before
        # the fix): see WhoCommittedInAPeriod's identical test for the full
        # rationale.
        reader = self.chain({8_463: [self.weights_set(0)]}, buggy_blocks={8_463})
        with self.assertRaises(TypeError):
            reader.weights_set_for_seat(2, 0, 8_460, 8_470)

    def test_a_block_answering_none_without_raising_refuses_too(self):
        """A non-exception answer that is not a list of events is not silence.

        See WhoCommittedInAPeriod's identical test for the full rationale: a
        real endpoint has been measured returning a shortened event list
        without ever raising, for over half a day (chain/agreeing.py), so a
        None (or otherwise malformed) answer is treated the same as a failed
        read rather than as "no application here".
        """
        reader = self.chain({8_463: None})
        with self.assertRaises(ChainUnreadable):
            reader.weights_set_for_seat(2, 0, 8_460, 8_470)


class EveryLandingBlockForThisSeat(unittest.TestCase):
    """``weights_set_blocks_for_seat`` -- issue #212. ``weights_set_for_seat``
    (above) only ever proves a match EXISTS somewhere in a range, which is
    not enough once the range is wide enough to hold more than one real
    ``WeightsSet`` for this seat (an earlier day's stale republish AND a
    later, genuinely delayed recovery, say). This is the sibling that
    returns every landing block instead, so a caller can read each one's
    own stored content and tell them apart.
    """

    def chain(self, events_by_block, unreadable_blocks=(), buggy_blocks=()):
        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if block in buggy_blocks:
                    raise TypeError("weights_set_blocks_for_seat called the client wrong")
                if block in unreadable_blocks:
                    raise node_down()
                return events_by_block.get(block, [])

            def query_map(self, item, params=None):
                return []

        return ChainReader(C())

    @staticmethod
    def weights_set(uid, netuid_index=2):
        return {"event": {"event_id": "WeightsSet", "attributes": [netuid_index, uid]}}

    @staticmethod
    def noise():
        return {"event": {"event_id": "SomethingElse", "attributes": [1, 2]}}

    def test_a_single_landing_block_is_returned(self):
        reader = self.chain({8_463: [self.weights_set(0)]})
        self.assertEqual(reader.weights_set_blocks_for_seat(2, 0, 8_460, 8_470), [8_463])

    def test_every_landing_block_is_returned_ascending_not_just_the_first(self):
        """The one real behavior difference from weights_set_for_seat: this
        does not stop at the first match, because the false-positive class
        issue #212 closes needs every candidate's own content compared,
        not just proof that one of them exists."""
        reader = self.chain({8_461: [self.weights_set(0)], 8_468: [self.weights_set(0)]})
        self.assertEqual(
            reader.weights_set_blocks_for_seat(2, 0, 8_460, 8_470), [8_461, 8_468]
        )

    def test_no_application_in_the_window_reads_as_an_empty_list_not_none(self):
        reader = self.chain({block: [self.noise()] for block in range(8_460, 8_471)})
        self.assertEqual(reader.weights_set_blocks_for_seat(2, 0, 8_460, 8_470), [])

    def test_a_reader_that_saw_nothing_answers_none_not_an_empty_list(self):
        """Identical distinction to weights_set_for_seat's own: a window
        that answered zero events at all is a reader that reached nothing,
        not a reader that confirmed absence."""
        self.assertIsNone(self.chain({}).weights_set_blocks_for_seat(2, 0, 8_460, 8_470))

    def test_another_seats_application_is_not_counted(self):
        reader = self.chain({8_463: [self.weights_set(7)]})
        self.assertEqual(reader.weights_set_blocks_for_seat(2, 0, 8_460, 8_470), [])

    def test_a_block_that_fails_to_read_refuses_even_when_matches_were_already_found(self):
        """Unlike weights_set_for_seat's own early exit, this never stops
        at a first match -- so a later block failing to read must still
        refuse, even though an earlier real match was already collected:
        the day actually being checked may be sitting in exactly the
        block that failed, not the one already found."""
        reader = self.chain(
            {8_461: [self.weights_set(0)]}, unreadable_blocks={8_468}
        )
        with self.assertRaises(ChainUnreadable):
            reader.weights_set_blocks_for_seat(2, 0, 8_460, 8_470)

    def test_a_bug_reading_one_block_propagates_rather_than_joining_unread_blocks(self):
        reader = self.chain({8_463: [self.weights_set(0)]}, buggy_blocks={8_463})
        with self.assertRaises(TypeError):
            reader.weights_set_blocks_for_seat(2, 0, 8_460, 8_470)


class RawWeightsContentAtABlock(unittest.TestCase):
    """``weights_content_for_seat`` -- issue #212. The actual, decoded
    ``(uid, weight)`` pairs the chain holds at a specific block, not just
    whether the row is non-empty (``weights_storage_nonempty_for_seat``,
    below) or whether a ``WeightsSet`` event exists there at all
    (``weights_set_for_seat``, above). This is the content-comparison
    primitive ``ChainAdapter.already_published``'s own #212 fix reads back
    to tell one day's real publication apart from another's."""

    def chain(self, answers):
        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                assert item == ("SubtensorModule", "Weights")
                key = (tuple(params), block)
                if key not in answers:
                    raise AssertionError(f"unexpected query for {key}")
                value = answers[key]
                if isinstance(value, Exception):
                    raise value
                return value

            def query_map(self, item, params=None):
                return []

        return ChainReader(C())

    def test_the_real_decoded_pairs_are_returned_not_just_whether_they_exist(self):
        reader = self.chain({((2, 0), 901): [(1, 65535), (2, 65535)]})
        self.assertEqual(reader.weights_content_for_seat(2, 0, 901), [(1, 65535), (2, 65535)])

    def test_two_different_blocks_can_answer_different_content(self):
        """The whole point: the SAME storage item, read at two different
        blocks, holds whatever was most recently written by then -- this
        is what makes it possible to tell one publication's content apart
        from another's."""
        reader = self.chain(
            {
                ((2, 0), 901): [(1, 65535)],
                ((2, 0), 9853): [(1, 64937)],
            }
        )
        self.assertEqual(reader.weights_content_for_seat(2, 0, 901), [(1, 65535)])
        self.assertEqual(reader.weights_content_for_seat(2, 0, 9853), [(1, 64937)])

    def test_an_untouched_seat_answers_its_default_empty_list(self):
        reader = self.chain({((2, 0), 1): []})
        self.assertEqual(reader.weights_content_for_seat(2, 0, 1), [])

    def test_a_wrapped_value_is_unwrapped_before_being_returned(self):
        reader = self.chain({((2, 0), 901): Wrapped([(1, 65535)])})
        self.assertEqual(reader.weights_content_for_seat(2, 0, 901), [(1, 65535)])

    def test_an_undecoded_non_list_answer_refuses_rather_than_reading_as_empty(self):
        for bad in ("unknown", 1, True, {"value": [(1, 2)]}):
            with self.subTest(bad=bad):
                reader = self.chain({((2, 0), 901): bad})
                with self.assertRaises(ChainUnreadable):
                    reader.weights_content_for_seat(2, 0, 901)

    def test_a_transport_failure_is_reported_as_chainunreadable(self):
        reader = self.chain({((2, 0), 901): node_down()})
        with self.assertRaises(ChainUnreadable):
            reader.weights_content_for_seat(2, 0, 901)


class WeightsStorageIsDecodedNotJustPresent(unittest.TestCase):
    """A ``WeightsSet`` event is a claim something was accepted into a
    block; it is not a read of the storage itself. #12's Command Center
    (settings-window-status, 2026-09-17) needs both: this class tests the
    second, independent half -- reading ``Weights[netuid][uid]`` itself,
    decoded, rather than trusting an event or a self-reported flag.
    """

    def chain(self, answers):
        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                assert item == ("SubtensorModule", "Weights")
                key = tuple(params)
                if key not in answers:
                    raise AssertionError(f"unexpected query for {key}")
                value = answers[key]
                if isinstance(value, Exception):
                    raise value
                return value

            def query_map(self, item, params=None):
                return []

        return ChainReader(C())

    def test_a_real_weight_vector_reads_as_nonempty(self):
        reader = self.chain({(2, 0): [(5, 3000), (9, 62535)]})
        self.assertIs(reader.weights_storage_nonempty_for_seat(2, 0, 12_000), True)

    def test_an_untouched_seat_answers_its_default_empty_list(self):
        # A StorageDoubleMap entry that was never written answers its
        # default (empty list) rather than erroring -- a real, meaningful
        # "nothing applied", not a failed read.
        reader = self.chain({(2, 0): []})
        self.assertIs(reader.weights_storage_nonempty_for_seat(2, 0, 12_000), False)

    def test_a_wrapped_value_is_unwrapped_before_being_judged(self):
        reader = self.chain({(2, 0): Wrapped([(5, 3000)])})
        self.assertIs(reader.weights_storage_nonempty_for_seat(2, 0, 12_000), True)

    def test_a_different_seat_on_the_same_subnet_is_asked_separately(self):
        reader = self.chain({(2, 0): [], (2, 7): [(1, 100)]})
        self.assertIs(reader.weights_storage_nonempty_for_seat(2, 0, 12_000), False)
        self.assertIs(reader.weights_storage_nonempty_for_seat(2, 7, 12_000), True)

    def test_an_undecoded_non_list_answer_refuses_rather_than_reading_as_empty(self):
        # The false-permitting shape subnet_exists's own docstring warns
        # about, re-applied to a different storage item: an answer that
        # arrived truthy-but-undecoded must not silently read as "empty" /
        # "nothing applied".
        for bad in ("unknown", 1, True, {"value": [(1, 2)]}):
            with self.subTest(bad=bad):
                reader = self.chain({(2, 0): bad})
                with self.assertRaises(ChainUnreadable):
                    reader.weights_storage_nonempty_for_seat(2, 0, 12_000)

    def test_a_transport_failure_is_reported_as_chainunreadable(self):
        reader = self.chain({(2, 0): node_down()})
        with self.assertRaises(ChainUnreadable):
            reader.weights_storage_nonempty_for_seat(2, 0, 12_000)

    def test_a_bug_reading_the_storage_item_propagates_rather_than_becoming_chainunreadable(self):
        reader = self.chain({(2, 0): TypeError("validator called client with the wrong arguments")})
        with self.assertRaises(TypeError):
            reader.weights_storage_nonempty_for_seat(2, 0, 12_000)


class PairingARevealToItsOwnCommit(unittest.TestCase):
    """The verdict needs THIS commit's outcome, not a nearby one.

    NOAH-VERIFIER found that a wide window credits a period with a LATER
    commit's reveal, which over-reports "worked" and leaves a genuinely failed
    period unexcused. They proposed narrowing to exactly one period after the
    commit. Measured on the chain, that would have broken a working case: the
    offset is not fixed.

        commit block 8,438 (period 421)  ->  reveal 8,463 (period 423)
        commit block 8,624 (period 431)  ->  reveal 8,643 (period 432)

    Same validator, same subnet, two periods and one period. So the bound is the
    chain's rate limit, which it enforces, rather than an offset it does not
    promise: a second commit from the same hotkey cannot land inside that
    window, so the only reveal from our hotkey inside it is this commit's.
    """

    OURS = "5" + "A" * 47
    THEIRS = "5" + "B" * 47

    def chain(self, revealed, netuid=2, unreadable_blocks=(), buggy_blocks=()):
        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if block in buggy_blocks:
                    raise TypeError("reveal_of_commit called the client wrong")
                if block in unreadable_blocks:
                    raise node_down()
                who = revealed.get(block)
                if who is None:
                    return [{"event": {"event_id": "Other", "attributes": []}}]
                return [
                    {
                        "event": {
                            "event_id": "TimelockedWeightsRevealed",
                            "attributes": (netuid, who),
                        }
                    }
                ]

            def query_map(self, item, params=None):
                return []

        return ChainReader(C())

    def test_a_reveal_one_period_later_is_found(self):
        self.assertEqual(
            self.chain({8_643: self.OURS}).reveal_of_commit(2, self.OURS, 8_624, 100), 8_643
        )

    def test_a_reveal_two_periods_later_is_also_found(self):
        # The case a one-period window would have missed. Both are real.
        self.assertEqual(
            self.chain({8_463: self.OURS}).reveal_of_commit(2, self.OURS, 8_438, 100), 8_463
        )

    def test_another_validators_reveal_is_not_ours(self):
        self.assertIsNone(
            self.chain({8_463: self.THEIRS}).reveal_of_commit(2, self.OURS, 8_438, 100)
        )

    def test_ours_is_found_past_another_validators(self):
        reader = self.chain({8_445: self.THEIRS, 8_463: self.OURS})
        self.assertEqual(reader.reveal_of_commit(2, self.OURS, 8_438, 100), 8_463)

    def test_the_search_stops_at_the_rate_limit(self):
        """Beyond it, a reveal could belong to a later commit of ours.

        The chain refuses a second commit from the same hotkey inside the
        window, which is what makes the pairing unambiguous. Past it that
        guarantee is gone, so the search must be too.
        """
        self.assertIsNone(self.chain({8_600: self.OURS}).reveal_of_commit(2, self.OURS, 8_438, 100))

    def test_no_reveal_at_all_answers_none(self):
        self.assertIsNone(self.chain({}).reveal_of_commit(2, self.OURS, 8_438, 100))

    def test_the_commit_block_itself_is_searched(self):
        # A reveal in the same block is still this commit's.
        self.assertEqual(
            self.chain({8_438: self.OURS}).reveal_of_commit(2, self.OURS, 8_438, 100), 8_438
        )

    def test_a_reveal_on_a_different_subnet_is_not_ours(self):
        """TimelockedWeightsRevealed(netuid, hotkey) has to be checked on netuid too.

        Same hotkey, same block, but the reveal names a different subnet.
        Without comparing netuid, this would be paired to our commit anyway.
        """
        reader = self.chain({8_643: self.OURS}, netuid=7)
        self.assertIsNone(reader.reveal_of_commit(2, self.OURS, 8_624, 100))

    def test_a_reveal_under_a_non_zero_mechanism_is_still_this_subnets(self):
        """The reveal event's subnet field is netuid + mechanism * 4096.

        Subnet 2, mechanism 1 packs to storage index 4098. A comparison
        against the packed field directly would read this reveal as some
        other subnet's and silently fail to pair it with subnet 2's own
        commit (the class of bug noah/chain/emissions.py's
        netuid_and_mechanism already exists to prevent, for pay events).
        """
        packed = 2 + 1 * 4096  # netuid 2, mechanism 1
        reader = self.chain({8_643: self.OURS}, netuid=packed)
        self.assertEqual(reader.reveal_of_commit(2, self.OURS, 8_624, 100), 8_643)

    def test_a_block_that_fails_to_read_refuses_rather_than_reporting_no_reveal(self):
        """A window read incompletely must not look like a window with no reveal.

        The reveal is sitting in exactly the block that fails here. Every
        other block in the window answers fine, so a version of this that
        only gave up when EVERY block failed would miss it and answer None,
        indistinguishable from "has not revealed yet".
        """
        reader = self.chain({8_643: self.OURS}, unreadable_blocks={8_643})
        with self.assertRaises(ChainUnreadable):
            reader.reveal_of_commit(2, self.OURS, 8_624, 100)

    def test_a_bug_reading_one_block_propagates_rather_than_joining_unread_blocks(self):
        # #510's site in this method (reader.py's per-block `except
        # Exception as error: unread_blocks.append(...)`, ~line 531 before
        # the fix): see WhoCommittedInAPeriod's identical test for the full
        # rationale.
        reader = self.chain({8_643: self.OURS}, buggy_blocks={8_643})
        with self.assertRaises(TypeError):
            reader.reveal_of_commit(2, self.OURS, 8_624, 100)

    def test_a_block_answering_none_without_raising_refuses_too(self):
        """A non-exception answer that is not a list of events is not silence.

        See WhoCommittedInAPeriod's identical test for the full rationale: a
        real endpoint has been measured returning a shortened event list
        without ever raising, for over half a day (chain/agreeing.py), so a
        None (or otherwise malformed) answer is treated the same as a failed
        read rather than as "no reveal here yet".
        """

        class C:
            block = 12_000

            def query(self, item, params=None, block=None):
                if block == 8_643:
                    return None
                return [{"event": {"event_id": "Other", "attributes": []}}]

            def query_map(self, item, params=None):
                return []

        reader = ChainReader(C())
        with self.assertRaises(ChainUnreadable):
            reader.reveal_of_commit(2, self.OURS, 8_624, 100)


class ConnectionGenesisHashSurvivesAnEmptyBlockInfo(unittest.TestCase):
    """`Connection` (this module, wired into `noah.core.chain.identify`) had
    no test coverage at all before this. Found live by codex adversarial
    review (2026-08-29): the real bittensor client's `block_info(0)` is
    documented to return None -- not raise -- when the response carries no
    header (bittensor/client.py). `genesis_hash()` dereferenced `.hash`
    unconditionally, so that None crashed with a raw AttributeError instead
    of reaching `identify()`'s existing "empty or malformed answer" check,
    already tested (`noah/core/tests/test_chain.py`) to refuse exactly a
    None genesis cleanly with ChainNotIdentified.
    """

    def test_a_normal_answer_returns_the_hash(self):
        class BlockInfo:
            hash = "0xGENESIS"

        class Client:
            def block_info(self, number):
                assert number == 0
                return BlockInfo()

        self.assertEqual(Connection(Client()).genesis_hash(), "0xGENESIS")

    def test_a_header_less_response_returns_none_rather_than_crashing(self):
        class Client:
            def block_info(self, number):
                return None

        self.assertIsNone(Connection(Client()).genesis_hash())


class TempoAcceptsAnExplicitBlockSoACallerCanPinIt(unittest.TestCase):
    """Tempo reads accept an explicit block so related observations can share
    one chain snapshot. Omitting the optional block retains client defaults."""

    def test_no_block_given_behaves_exactly_as_before(self):
        class Client:
            block = 8_000

            def query(self, item, params=None, block=None):
                assert item == ("SubtensorModule", "Tempo")
                assert block is None
                return Wrapped(360)

        self.assertEqual(ChainReader(Client()).tempo(2), 360)

    def test_an_explicit_block_is_threaded_straight_through_to_the_query(self):
        class Client:
            block = 8_000
            calls = []

            def query(self, item, params=None, block=None):
                Client.calls.append(block)
                assert item == ("SubtensorModule", "Tempo")
                return Wrapped(360)

        Client.calls = []
        result = ChainReader(Client()).tempo(2, block=7_777)
        self.assertEqual(result, 360)
        self.assertEqual(
            Client.calls, [7_777], "the real block argument must reach query(), not None"
        )


class ClosingTheUnderlyingClient(unittest.TestCase):
    """ChainReader.close delegates to the wrapped client rather than leaking it."""

    def test_close_closes_the_underlying_client(self):
        class Client:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            def close(self):
                self.closed = True
                self.close_calls += 1

        client = Client()
        reader = ChainReader(client)

        reader.close()

        self.assertTrue(client.closed)
        self.assertEqual(client.close_calls, 1)
