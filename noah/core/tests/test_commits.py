"""Where commits are filed, and why an empty answer needs checking.

The bug this prevents has already happened here: a read went to a storage map
that exists, is queryable, and is not where this runtime files commits. It
returned empty, and a healthy publication was reported missing.

Every test below is about telling two things apart that look identical from the
outside: a chain holding no commits, and a reader looking in the wrong place.
"""

import unittest

from ..commits import (
    LEGACY_MAPS,
    LIVE_MAP,
    CommitsFiledElsewhere,
    CommitStorageUnreadable,
    read_stored_commits,
)


class Chain:
    """A chain where each storage map can be set independently.

    They have to be independent. A fake that answered every map from one place
    could not express the case this module exists for, which is one map holding
    something while another reads empty.
    """

    def __init__(self, **maps):
        self.maps = maps
        self.asked = []

    def read_map(self, name, netuid):
        self.asked.append(name)
        value = self.maps.get(name, [])
        if isinstance(value, Exception):
            raise value
        return value


class TheLiveMapIsTheOneTheRuntimeUses(unittest.TestCase):
    def test_commits_come_from_the_live_map(self):
        chain = Chain(**{LIVE_MAP: ["c1", "c2"]})
        self.assertEqual(read_stored_commits(chain, 2), ["c1", "c2"])

    def test_a_legacy_map_holding_something_is_not_read_as_our_commits(self):
        # If it read any map that answered, this would return the legacy rows.
        chain = Chain(**{LIVE_MAP: ["ours"], LEGACY_MAPS[0]: ["theirs", "theirs2"]})
        self.assertEqual(read_stored_commits(chain, 2), ["ours"])

    def test_a_populated_live_map_is_not_second_guessed(self):
        # The legacy maps are only consulted to explain an empty answer, so a
        # non-empty one must not spend reads on them.
        chain = Chain(**{LIVE_MAP: ["c1"]})
        read_stored_commits(chain, 2)
        self.assertEqual(chain.asked, [LIVE_MAP])


class AnEmptyAnswerIsCheckedBeforeItIsBelieved(unittest.TestCase):
    def test_everything_empty_really_does_mean_no_commits(self):
        chain = Chain()
        self.assertEqual(read_stored_commits(chain, 2), [])
        # And it looked, rather than assuming.
        for name in LEGACY_MAPS:
            self.assertIn(name, chain.asked)

    def test_a_legacy_map_holding_commits_refuses_instead_of_reporting_none(self):
        """The exact bug that already happened, held closed.

        Reading the wrong map returns empty, which is identical to a correct
        read of a chain with no commits. One of them means a publication is
        missing and the other does not.
        """
        chain = Chain(**{LEGACY_MAPS[1]: ["a", "b", "c"]})
        with self.assertRaises(CommitsFiledElsewhere) as caught:
            read_stored_commits(chain, 2)
        message = str(caught.exception)
        self.assertIn(LEGACY_MAPS[1], message)
        self.assertIn("3", message)

    def test_the_refusal_names_both_explanations_rather_than_picking_one(self):
        chain = Chain(**{LEGACY_MAPS[0]: ["a"]})
        with self.assertRaises(CommitsFiledElsewhere) as caught:
            read_stored_commits(chain, 2)
        message = str(caught.exception)
        self.assertIn("runtime has moved", message)
        self.assertIn("wrong map", message)

    def test_several_legacy_maps_holding_things_are_all_named(self):
        chain = Chain(**{LEGACY_MAPS[0]: ["a"], LEGACY_MAPS[2]: ["b", "c"]})
        with self.assertRaises(CommitsFiledElsewhere) as caught:
            read_stored_commits(chain, 2)
        message = str(caught.exception)
        self.assertIn(LEGACY_MAPS[0], message)
        self.assertIn(LEGACY_MAPS[2], message)


class WhatItRefusesToConclude(unittest.TestCase):
    def test_an_unreadable_live_map_concludes_nothing(self):
        chain = Chain(**{LIVE_MAP: RuntimeError("node down")})
        with self.assertRaises(CommitStorageUnreadable):
            read_stored_commits(chain, 2)

    def test_an_unconfirmable_empty_refuses_rather_than_reporting_none(self):
        # Live map empty and every cross-check unreadable. The empty answer is
        # not confirmed, and an unconfirmed empty is the whole shape of the bug.
        chain = Chain(**{name: RuntimeError("down") for name in LEGACY_MAPS})
        with self.assertRaises(CommitStorageUnreadable):
            read_stored_commits(chain, 2)

    def test_one_unreadable_cross_check_does_not_block_a_confirmed_empty(self):
        # Two of three answered and held nothing, which is enough to say the
        # emptiness is real rather than a lost reader.
        chain = Chain(**{LEGACY_MAPS[0]: RuntimeError("down")})
        self.assertEqual(read_stored_commits(chain, 2), [])


if __name__ == "__main__":
    unittest.main()
