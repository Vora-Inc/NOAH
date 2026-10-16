"""A keyed RPC URL must not reach the assistant's output when the connection
library fails in its constructor, whatever the exception type."""

import sys
import traceback
import types

import pytest

from noah.deposits.assistant import cli
from noah.deposits.assistant.cli import ConnectionFailed, build_services

SECRET = "CANARY" + "KEY123"
URL = f"http://127.0.0.1:1/{SECRET}"


class InvalidURI(Exception):
    pass


class Unmatched(Exception):  # a type the old connection-failure handler did not recognise
    pass


def _fake_library(monkeypatch, exc_type):
    module = types.ModuleType("async_substrate_interface")

    def ctor(url, **kwargs):
        raise exc_type(f"{url} isn't a valid URI: scheme isn't ws or wss")

    module.SubstrateInterface = ctor
    monkeypatch.setitem(sys.modules, "async_substrate_interface", module)


@pytest.mark.parametrize("exc_type", [InvalidURI, Unmatched, ValueError])
def test_build_services_failure_has_no_url_in_text_or_traceback(monkeypatch, exc_type):
    _fake_library(monkeypatch, exc_type)
    config = types.SimpleNamespace(endpoint=URL)
    with pytest.raises(ConnectionFailed) as caught:
        build_services(config)
    error = caught.value
    printed = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert SECRET not in printed
    assert exc_type.__name__ in str(error)


def test_main_prints_the_existing_clean_message_without_the_key(monkeypatch, capsys, tmp_path):
    _fake_library(monkeypatch, InvalidURI)
    config = types.SimpleNamespace(endpoint=URL)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    code = cli.main(
        ["--config", str(tmp_path / "c.ini"), "status"], services_factory=build_services
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "cannot reach the chain node at http://127.0.0.1:1" in out
    assert SECRET not in out
