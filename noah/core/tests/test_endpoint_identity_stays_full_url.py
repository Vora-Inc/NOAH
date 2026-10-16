"""Two keyed URLs on one host are two different endpoints. Labels (scheme://host) are for
display only: every identity comparison keeps using the full URL."""

import tempfile
import unittest
from unittest import mock

from noah.core.chain_adapter import build_chain_adapter
from noah.core.mainnet import endpoint_problems, setting_problems
from noah.core.settings import Settings

from ._adapter_helpers import (
    ARK,
    DIALS,
    FLOORED_ANCHOR,
    NETUID,
    VALIDATOR_HOTKEY,
    FakeReader,
    TrivialHotkey,
)
from ._mainnet_helpers import good_mapping, write_profile

A, B = "wss://rpc.example/KEYA", "wss://rpc.example/KEYB"


class TheMainnetIndependenceChecksKeepTheFullUrl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"NOAH_PARAMETERS_JSON": write_profile(self.tmp.name)}

    def problems(self, first, second):
        mapping = {**good_mapping(), "NOAH_CHAIN_ENDPOINT": first, "NOAH_ENDPOINT_2": second}
        return "\n".join(setting_problems(Settings.from_mapping(mapping), self.env))

    def test_setting_problems_treats_two_keys_on_one_host_as_two_endpoints(self):
        self.assertNotIn("same endpoint under two spellings", self.problems(A, B))
        self.assertIn("same endpoint under two spellings", self.problems(A, A))

    def test_endpoint_problems_treats_two_keys_on_one_host_as_two_endpoints(self):
        from noah.core.chain import MAINNET

        two = endpoint_problems(
            [("NOAH_CHAIN_ENDPOINT", A, MAINNET, None), ("NOAH_ENDPOINT_2", B, MAINNET, None)]
        )
        self.assertEqual(two, [])
        one = endpoint_problems(
            [("NOAH_CHAIN_ENDPOINT", A, MAINNET, None), ("NOAH_ENDPOINT_2", A, MAINNET, None)]
        )
        self.assertIn("fewer than two independent endpoints were reached.", one)


class TheAdapterKeepsTheFullUrlForIdentity(unittest.TestCase):
    def build(self, first, second):
        class Fake:
            def __init__(self, url, *, auto_discover=True):
                self.url = url

            def close(self):
                pass

        class Cfg:
            chain_endpoint = first
            second_chain_endpoint = second
            netuid = NETUID
            settle_blocks = 1

        class Wallet:
            hotkey = TrivialHotkey()

        with mock.patch("async_substrate_interface.SubstrateInterface", Fake):
            return build_chain_adapter(
                client=object(),
                settings=Cfg(),
                reader=FakeReader(),
                wallet=Wallet(),
                uid=0,
                day_anchor_ts=FLOORED_ANCHOR,
                ark_coldkey=ARK,
                designated_validator_hotkey=VALIDATOR_HOTKEY,
                dials=DIALS,
                store_factory=lambda: None,
                holding_stride=1,
                entry_deposit_rao=100_000_000_000,
            )

    def test_two_keys_on_one_host_stay_distinct_in_the_agreeing_chain(self):
        endpoints = self.build(A, B).runner_chain.endpoints
        self.assertNotEqual(endpoints["primary"], endpoints["secondary"])
        self.assertIn("KEYA", endpoints["primary"])  # the identity is the full URL (functional)

    def test_the_same_url_twice_collapses_to_one_identity(self):
        endpoints = self.build(A, A).runner_chain.endpoints
        self.assertEqual(endpoints["primary"], endpoints["secondary"])


if __name__ == "__main__":
    unittest.main()
