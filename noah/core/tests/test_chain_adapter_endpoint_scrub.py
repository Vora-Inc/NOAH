"""``build_chain_adapter`` connects in the SubstrateInterface constructor, and
that library echoes the URL it was given in its error. A keyed URL must not
survive into the refusal text or its printed traceback.
"""

import traceback
import unittest
from unittest import mock

from noah.core.chain_adapter import ChainAdapterRefused, build_chain_adapter

SECRET = "SECRET" + "KEY123"


class _Settings:
    chain_endpoint = f"http://127.0.0.1:1/{SECRET}"
    second_chain_endpoint = "wss://other.example/ok"
    netuid = 2
    settle_blocks = 1


class _InvalidURI(Exception):
    pass


class ConnectionFailureIsScrubbed(unittest.TestCase):
    def _build(self, settings):
        class Failing:
            def __init__(self, url, *, auto_discover=True):
                raise _InvalidURI(f"{url} isn't a valid URI: scheme isn't ws or wss")

        with mock.patch("async_substrate_interface.SubstrateInterface", Failing):
            with self.assertRaises(ChainAdapterRefused) as caught:
                build_chain_adapter(
                    client=object(), settings=settings, reader=object(), wallet=object(), uid=0
                )
        return caught.exception

    def test_primary_endpoint_key_is_absent_from_text_and_traceback(self):
        error = self._build(_Settings())
        printed = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, printed)
        self.assertIn("http://127.0.0.1:1", str(error))

    def test_secondary_endpoint_key_is_absent_too(self):
        class Both(_Settings):
            chain_endpoint = "wss://fine.example"
            second_chain_endpoint = f"http://127.0.0.1:2/{SECRET}"

        class Selective:
            def __init__(self, url, *, auto_discover=True):
                if SECRET in url:
                    raise _InvalidURI(f"{url} isn't a valid URI")
                self.url = url

            def close(self):
                pass

        with mock.patch("async_substrate_interface.SubstrateInterface", Selective):
            with self.assertRaises(ChainAdapterRefused) as caught:
                build_chain_adapter(
                    client=object(), settings=Both(), reader=object(), wallet=object(), uid=0
                )
        printed = "".join(
            traceback.format_exception(
                type(caught.exception), caught.exception, caught.exception.__traceback__
            )
        )
        self.assertNotIn(SECRET, printed)


if __name__ == "__main__":
    unittest.main()
