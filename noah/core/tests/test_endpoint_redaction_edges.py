"""Edge cases of the endpoint redaction: a NUL in the second-endpoint file path, an encoded
separator in a key, and the test harness's default for the secret backstop."""

import sys

import pytest

from noah import secretscrub
from noah.chain.sources import scrub_endpoint_text
from noah.core.settings import BadSetting, Settings
from noah.core.tests.test_settings import COMPLETE


def test_a_nul_in_the_second_endpoint_file_path_is_a_clean_refusal(tmp_path):
    path = str(tmp_path / "ep") + "\x00tail"
    with pytest.raises(BadSetting) as caught:
        Settings.from_mapping({**COMPLETE, "NOAH_ENDPOINT_2": "", "NOAH_ENDPOINT_2_FILE": path})
    assert "NOAH_ENDPOINT_2_FILE" in str(caught.value)
    assert "unreadable" in str(caught.value)


def test_an_encoded_separator_in_a_key_does_not_leak_decoded():
    url = "wss://h.ex/rpc/ab%2Fcd12ZZ9"
    for text in (f"bad uri {url}", "bad uri wss://h.ex/rpc/ab/cd12ZZ9", "saw ab/cd12ZZ9 here"):
        out = scrub_endpoint_text(text, url)
        assert "cd12ZZ9" not in out and "ab/cd12" not in out, out


def test_the_backstop_is_off_by_default_under_the_test_harness():
    # conftest.py switches activate() off so site tests exercise the site fixes alone.
    assert secretscrub._enabled is False
    secretscrub.activate()
    assert not isinstance(sys.stdout, secretscrub.ScrubbingStream)
    assert secretscrub._installed is False


@pytest.mark.usefixtures("backstop")
def test_the_backstop_fixture_turns_it_on():
    assert secretscrub._enabled is True
