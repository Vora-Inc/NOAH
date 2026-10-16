"""A connection error's text can carry the endpoint URL (a wrong-scheme address is echoed
by the library). The refusals built from it must not keep it as the chained cause."""

import traceback
import unittest

from bittensor.result import RpcConnectionError

from noah.core.chain import ChainNotIdentified, identify
from noah.core.mainnet import RpcGenesis
from noah.core.reader import ChainReader, ChainUnreadable

CANARY = "CANARY" + "KEY123"
URL = f"http://127.0.0.1:1/{CANARY}"


def formatted(error):
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


class Echo(RpcConnectionError):
    pass


class TheRefusalsDoNotChainTheUrl(unittest.TestCase):
    def test_rpc_genesis_refusal(self):
        genesis = RpcGenesis(URL)
        # the real websockets client rejects a wrong scheme by echoing the URL
        with self.assertRaises(ChainNotIdentified) as caught:
            genesis.genesis_hash()
        error = caught.exception
        self.assertNotIn(CANARY, formatted(error))
        self.assertTrue(error.__suppress_context__)

    def test_identify_refusal(self):
        class Connection:
            def genesis_hash(self):
                raise Echo(f"{URL} isn't a valid URI")

        with self.assertRaises(ChainNotIdentified) as caught:
            identify(Connection())
        self.assertNotIn(CANARY, formatted(caught.exception))

    def test_reader_refusals(self):
        class Client:
            def query(self, *args, **kwargs):
                raise Echo(f"{URL} isn't a valid URI")

            query_map = query

            @property
            def block(self):
                raise Echo(f"{URL} isn't a valid URI")

        reader = ChainReader(Client())
        with self.assertRaises(ChainUnreadable) as caught:
            reader.current_block()
        self.assertNotIn(CANARY, formatted(caught.exception))
        with self.assertRaises(ChainUnreadable) as caught:
            reader._query("Item")
        self.assertNotIn(CANARY, formatted(caught.exception))
        with self.assertRaises(ChainUnreadable) as caught:
            reader._query_map("Item")
        self.assertNotIn(CANARY, formatted(caught.exception))

    def test_archive_capability_refusal(self):
        from noah.core.startup import NotArchiveCapable, check_reads_history

        class Chain:
            def historical_probe(self, depth):
                raise ChainUnreadable(f"could not read at {URL} ({URL} isn't a valid URI)")

        with self.assertRaises(NotArchiveCapable) as caught:
            check_reads_history(Chain(), 100)
        self.assertNotIn(CANARY, formatted(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
