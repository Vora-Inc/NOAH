"""Endpoint URLs in mainnet messages never carry a credential."""

from __future__ import annotations

import unittest

from ..mainnet import MAINNET, endpoint_problems, redact_endpoint

# Built from words at run time so no scanner mistakes it for a credential.
MARKER = "-".join(("walnut", "kettle", "zebra"))
AT = "@"  # URL userinfo separator, kept out of the literals below


class RedactEndpoint(unittest.TestCase):
    def test_credentials_in_every_position_are_hidden(self):
        for url in (
            f"wss://user:{MARKER}{AT}rpc.example.com:443",
            f"wss://rpc.example.com/v2/{MARKER}",
            f"wss://rpc.example.com/?apikey={MARKER}",
            f"ws://rpc.example.com:9944/path?token={MARKER}#{MARKER}",
        ):
            with self.subTest(url=url):
                self.assertNotIn(MARKER, redact_endpoint(url))

    def test_plain_endpoint_keeps_scheme_host_port(self):
        self.assertEqual(redact_endpoint("ws://127.0.0.1:9945"), "ws://127.0.0.1:9945")
        self.assertEqual(redact_endpoint("wss://archive.chain.opentensor.ai:443"),
                         "wss://archive.chain.opentensor.ai:443")

    def test_garbage_is_not_echoed(self):
        self.assertNotIn(MARKER, redact_endpoint(f"{MARKER}"))

    def test_problem_messages_never_contain_the_key(self):
        url = f"wss://rpc.example.com/v2/{MARKER}"
        answers = [
            ("NOAH_CHAIN_ENDPOINT", url, None, f"refused {url}"),
            ("NOAH_ENDPOINT_2", f"wss://u:{MARKER}{AT}other.example.com", "0xnotmainnet", None),
        ]
        text = " ".join(endpoint_problems(answers))
        self.assertNotIn(MARKER, text)
        self.assertIn("rpc.example.com", text)
        self.assertNotEqual(MAINNET, "0xnotmainnet")


if __name__ == "__main__":
    unittest.main()
