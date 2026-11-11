"""Own replacement sockets on the pinned synchronous read-only SDK path.

async-substrate-interface 2.2.1 retries with connect(init=True), which otherwise
returns an unowned socket and leaves the next request on the original session.
This adapter changes socket ownership only; request/retry budgets and all chain
checks remain the caller's responsibility. It is not used for publication.
"""

from async_substrate_interface import SubstrateInterface


class ReadSubstrateInterface(SubstrateInterface):
    def connect(self, init=False):
        previous = getattr(self, "ws", None)
        # A failed dial leaves the previous connection owned and closeable.
        replacement = super().connect(init=init)
        if replacement is not previous:
            # Own the new socket before cleanup, so even cleanup failure cannot
            # strand it outside client.close(). Initial construction has no old ws.
            self.ws = replacement
            if previous is not None:
                previous.close()
        return replacement
