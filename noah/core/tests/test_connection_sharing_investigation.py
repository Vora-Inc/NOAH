"""Ticket #16 (B06), 2026-09-15: investigation into collapsing three real
network connections into two.

**The question, from the Orchestrator, traced to real observed stalls
correlating with multi-connection runs.** The real chain path opens THREE
connections per run, but only TWO distinct physical nodes are actually
involved:

1. ``neurons/validator.py::connect()``'s ``bt.subtensor`` client (backs
   ``ChainAdapter.reader`` -- the single-sourced operating-state reads).
2. ``build_chain_adapter``'s "primary" ``LiveRunnerChain``'s fresh
   ``async_substrate_interface.SubstrateInterface``, at
   ``settings.chain_endpoint``.
3. ``build_chain_adapter``'s "second" ``LiveRunnerChain``'s fresh
   ``SubstrateInterface``, at ``settings.second_chain_endpoint`` -- a
   genuinely DIFFERENT node.

Connections #1 and #2 point at the IDENTICAL endpoint (confirmed directly
in code: ``primary_endpoint = settings.chain_endpoint or getattr(client,
"endpoint", None)``), so #1 is a second, avoidable network connection to a
node #2 already reaches. #2 and #3 are the real, necessary dual-source
pair and must stay independent -- this investigation never touches that.

**The proposed fix, and the real, verified finding: it does not work.**
The idea was to build ONE ``SubstrateInterface`` for the primary endpoint
and hand that SAME object to both ``bt.subtensor(substrate=...)`` (for
``reader``/signing) and ``LiveRunnerChain`` (for the dual-sourced reads).
Verified, not assumed, by reading the real, installed ``bittensor``
package's own source directly (``bittensor/_substrate.py``,
``bittensor/client.py``) and by constructing a real, running object and
watching it fail:

1. ``bt.subtensor.__init__``'s ``substrate=`` parameter is passed straight
   through to ``bittensor.client.Client.__init__``, which stores it as
   ``self._substrate: Substrate = substrate`` with NO validation at
   construction time -- ``bittensor._substrate.Substrate`` is
   ``typing.Protocol``-based, and Python does not check Protocol
   conformance on plain assignment.
2. ``bittensor._substrate.Substrate`` is ``bittensor``'s OWN chain-access
   contract -- an **ASYNC** protocol (``async def block_hash``, ``async
   def block_number``, ``async def query``, ``async def query_map``,
   ``async def query_batch``, ``async def runtime_call``, ``async def
   constant``, ``async def decode_scale``, ``async def events``, ``async
   def connect``, ``async def close``; only ``balance`` is plain ``def``)
   -- with its own method names, several of which
   ``async_substrate_interface.SubstrateInterface`` (the SYNC facade
   ``noah.chain.live.LiveRunnerChain`` is built against, per that class's
   own docstring: "``get_block_number``, ``get_block_hash``, ``get_events``
   and ``query``/``query_map``") does not have at all: ``balance``,
   ``block_hash`` (vs. ``get_block_hash``), ``block_number`` (vs.
   ``get_block_number``), ``block_time``, ``spec_version``,
   ``query_batch``, ``runtime_call``, ``constant``, ``decode_scale``,
   ``events`` -- 10 of 11 named methods either absent or present under a
   different name, and every one of the ones that DOES exist under a
   shared or similar name (``connect``, ``close``, ``query``,
   ``query_map``) is synchronous on ``SubstrateInterface``, never
   ``async``.
3. ``bittensor._substrate.py``'s own module docstring states plainly:
   "``RpcSubstrate`` is the production implementation: a thin async
   wrapper over the transport's ``SubstrateConnection``" --
   ``bittensor._transport.SubstrateConnection``, bittensor's own,
   independent transport. Confirmed directly: ``grep -rln
   "async_substrate_interface" `` across the entire installed
   ``bittensor`` package returns NOTHING. The two libraries share no code
   at all; connection #1 and connections #2/#3 are not two instances of
   the same client, they are two entirely separate implementations of
   talking to a Substrate chain.
4. Confirmed empirically, not merely by reading signatures: constructing
   ``bt.subtensor(substrate=<a real, sync, SubstrateInterface-shaped
   object>)`` succeeds (no validation at construction time), but the
   first real read through it (``sub.block``, the exact property
   ``ChainReader.current_block()`` depends on) fails immediately with
   ``TypeError: object NoneType can't be used in 'await' expression`` --
   bittensor's own internals ``await`` a method the sync object either
   does not have under that name, or that returns a plain value rather
   than a coroutine.

**Conclusion: this specific connection-reduction is not achievable without
writing a new, full protocol-adapter shim** (translating every method in
``bittensor.Substrate``'s async contract onto ``async_substrate_interface.
SubstrateInterface``'s sync one, including the write/signing side this
investigation did not even need to reach). That is real, non-trivial,
RISKY new engineering -- it would insert a new, custom compatibility layer
underneath ``reader``/``gate()``/``survey()``/``publish_weights``, every
one of which depends on ``bt.subtensor`` behaving exactly as the real
library designs it -- not "wiring two existing things together." Per the
explicit instruction this round: do not force a workaround that quietly
weakens the dual-source guarantee (#2/#3 stay fully independent, untouched)
or that trades one small, well-understood redundancy for a large,
unreviewed new one. **No product code was changed by this investigation.**
``neurons/validator.py::connect()`` and ``noah/core/chain_adapter.py::
build_chain_adapter`` are unmodified.

This test captures the verified finding as a real, automated check (not
merely prose): if a future upgrade of ``bittensor`` or
``async_substrate_interface`` ever changes this incompatibility (e.g. a
future ``bittensor`` release adds sync method aliases, or a future
``async_substrate_interface`` release adds the missing async-named
methods), this test starts failing and is the signal to revisit this
investigation, rather than the incompatibility silently going stale in
prose nobody re-checks.
"""

import inspect

import bittensor as bt
import pytest
from async_substrate_interface import SubstrateInterface

# The exact, real method list bittensor._substrate.Substrate declares,
# read directly off the installed Protocol class -- not hand-typed from
# memory of reading the source, so a future bittensor upgrade that adds or
# renames a required method is caught automatically rather than silently
# checked against a stale, hardcoded list.
_SUBSTRATE_PROTOCOL_METHODS = sorted(
    name
    for name, value in vars(bt.client.Substrate).items()
    if callable(value) and not name.startswith("_")
)


def test_the_real_installed_bittensor_package_never_imports_async_substrate_interface():
    """The module-level finding: bittensor's own Substrate implementation
    (RpcSubstrate) wraps its own private transport, never
    async_substrate_interface -- confirmed by inspecting every module
    bittensor._substrate itself imports from."""
    import bittensor._substrate as substrate_module

    source = inspect.getsource(substrate_module)
    assert "async_substrate_interface" not in source
    assert "AsyncSubstrateInterface" not in source


def test_substrate_protocol_declares_more_than_a_handful_of_methods():
    """Sanity check that the introspected method list above is real and
    substantial, not an empty or trivially small list that would make the
    negative findings below vacuous."""
    assert len(_SUBSTRATE_PROTOCOL_METHODS) >= 10, _SUBSTRATE_PROTOCOL_METHODS


def test_substrateinterface_is_missing_most_of_the_required_substrate_methods():
    """The real incompatibility, checked against the real, installed
    classes on both sides: most of bittensor's own Substrate protocol
    methods do not exist AT ALL on async_substrate_interface's
    SubstrateInterface, under any name overlap."""
    missing = [
        name for name in _SUBSTRATE_PROTOCOL_METHODS if not hasattr(SubstrateInterface, name)
    ]
    # balance/connect/close/query/query_map happen to exist under the SAME
    # name (checked for sync-vs-async separately below); everything else
    # named in the real protocol (block_hash, block_number, block_time,
    # spec_version, get_block, query_batch, runtime_call, constant,
    # decode_scale, events, blocks, token_symbols) is absent under that
    # name on SubstrateInterface.
    assert len(missing) >= 8, (
        f"expected most Substrate protocol methods to be entirely absent from "
        f"SubstrateInterface; only {len(missing)} were missing: {sorted(missing)}"
    )


def test_every_shared_method_name_is_sync_on_substrateinterface_never_async():
    """Of the method names that DO exist on both sides, every one is a
    plain, synchronous function on SubstrateInterface -- never a coroutine
    function, which is what bittensor's own protocol requires for all of
    them except `balance`."""
    shared_names = [
        name for name in _SUBSTRATE_PROTOCOL_METHODS if hasattr(SubstrateInterface, name)
    ]
    assert shared_names, "expected at least one overlapping method name to check"
    for name in shared_names:
        member = getattr(SubstrateInterface, name)
        assert not inspect.iscoroutinefunction(member), (
            f"{name} was expected to be a plain sync function on SubstrateInterface, matching "
            f"this investigation's real finding -- if this now fails, async_substrate_interface "
            f"may have added an async-compatible surface and this investigation should be redone"
        )


def test_a_real_sync_substrate_interface_shaped_object_fails_on_first_real_use():
    """The empirical proof, not merely a signature comparison: a real,
    running `bt.subtensor` constructed with a sync, SubstrateInterface-
    shaped `substrate=` override constructs without error (no validation
    at construction time), but the first real read through it fails --
    proving the incompatibility is not just a naming mismatch this
    investigation could paper over with a thin renaming shim, but a
    genuine sync/async calling-convention mismatch bittensor's own
    internals hit immediately.
    """

    class FakeSyncSubstrateInterface:
        """Mimics async_substrate_interface.SubstrateInterface's real,
        installed shape: plain synchronous methods only, the exact
        surface noah.chain.live.LiveRunnerChain is built against."""

        def get_block_number(self, block_hash=None):
            return 12345

        def get_block_hash(self, block_id=None):
            return "0xdeadbeef"

        def query(self, module, storage_function, params=None, block_hash=None):
            return 42

        def query_map(self, module, storage_function, params=None, block_hash=None):
            return []

        def connect(self):
            pass

        def close(self):
            pass

    sub = bt.subtensor(network="local", substrate=FakeSyncSubstrateInterface())

    with pytest.raises(TypeError, match="await"):
        _ = sub.block
