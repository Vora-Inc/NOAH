"""noah.chain must not import noah.core, so its fixed mainnet genesis hash is
spelled a second time. This keeps the two spellings the same chain, and keeps
any chain the validator is forbidden to touch out of the table unnoticed."""

import unittest

from noah.chain.genesis_times import BLOCK_ONE_TIME_BY_GENESIS_HASH, MAINNET_GENESIS_HASH

from ..chain import KNOWN_CHAINS


class GenesisTimesNameKnownChains(unittest.TestCase):
    def test_the_mainnet_hash_is_the_one_the_validator_knows_as_mainnet(self):
        self.assertEqual(KNOWN_CHAINS[MAINNET_GENESIS_HASH], "MAINNET")

    def test_every_fixed_time_belongs_to_a_chain_the_validator_knows_by_name(self):
        for genesis in BLOCK_ONE_TIME_BY_GENESIS_HASH:
            with self.subTest(genesis=genesis):
                self.assertIn(genesis, KNOWN_CHAINS)


if __name__ == "__main__":
    unittest.main()
