"""Exercise actual pinned SDK request/retry methods with offline socket frames."""

import json
from unittest.mock import patch

import pytest
from async_substrate_interface.sync_substrate import SubstrateInterface

from noah.chain.read_connection import ReadSubstrateInterface


class Socket:
    close_code = None

    def __init__(self, head, failures=0, close_error=None):
        self.head, self.failures, self.close_error = head, failures, close_error
        self.sent = []
        self.closes = 0

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, **_kwargs):
        if self.failures:
            self.failures -= 1
            raise TimeoutError("offline timeout")
        return json.dumps({"jsonrpc": "2.0", "id": self.sent[-1]["id"], "result": self.head}).encode()

    def close(self):
        self.closes += 1
        if self.close_error:
            raise self.close_error
        self.close_code = 1000


def client(kind, socket):
    obj = kind(url="ws://offline.invalid", _mock=True, max_retries=3, retry_timeout=1)
    obj.ws = socket
    return obj


def test_original_sdk_retry_returns_to_old_socket_and_leaves_replacement_unowned():
    old, new = Socket("old", 1), Socket("new")
    c = client(SubstrateInterface, old)
    with patch("async_substrate_interface.sync_substrate.connect", return_value=new):
        assert c.get_chain_finalised_head() == "new"
        assert c.get_chain_finalised_head() == "old"
        c.close()
    assert c.ws is old and old.closes == 1 and new.closes == 0


def test_retry_owns_replacement_for_next_request_and_close():
    old, new = Socket("old", 1), Socket("new")
    c = client(ReadSubstrateInterface, old)
    with patch("async_substrate_interface.sync_substrate.connect", return_value=new):
        assert c.get_chain_finalised_head() == "new"
        assert c.get_chain_finalised_head() == "new"
        assert old.closes == 1 and len(old.sent) == 1
        assert c.ws is new
        c.close()
    assert new.closes == 1 and len(new.sent) == 2


def test_repeated_retry_replaces_every_previous_socket():
    sockets = [Socket("old", 1), Socket("middle", 1), Socket("current")]
    c = client(ReadSubstrateInterface, sockets[0])
    with patch("async_substrate_interface.sync_substrate.connect", side_effect=sockets[1:]):
        assert c.get_chain_finalised_head() == "current"
        assert c.ws is sockets[-1]
        c.close()
    assert [s.closes for s in sockets] == [1, 1, 1]


def test_initial_connection_and_failed_replacement_preserve_ownership():
    c = object.__new__(ReadSubstrateInterface)
    c.chain_endpoint, c.ws_max_size = "ws://offline.invalid", 1024
    initial = Socket("initial")
    with patch("async_substrate_interface.sync_substrate.connect", return_value=initial):
        assert c.connect(init=True) is initial and c.ws is initial
    with patch("async_substrate_interface.sync_substrate.connect", side_effect=OSError("offline failure")):
        with pytest.raises(OSError):
            c.connect(init=True)
    assert c.ws is initial and initial.closes == 0
    c.close()


def test_cleanup_failure_still_owns_replacement_and_refuses_request():
    old, new = Socket("old", 1, OSError("cleanup failed")), Socket("new")
    c = client(ReadSubstrateInterface, old)
    with patch("async_substrate_interface.sync_substrate.connect", return_value=new):
        with pytest.raises(OSError, match="cleanup failed"):
            c.get_chain_finalised_head()
    assert c.ws is new and new.sent == []
    c.close()
    assert new.closes == 1


def test_closed_socket_reconnect_owns_new_socket_without_forced_retry():
    old, new = Socket("old"), Socket("new")
    c = client(ReadSubstrateInterface, old)
    old.close()
    with patch("async_substrate_interface.sync_substrate.connect", return_value=new):
        assert c.get_chain_finalised_head() == "new"
        assert c.ws is new and not old.sent
    c.close()
    assert new.closes == 1
