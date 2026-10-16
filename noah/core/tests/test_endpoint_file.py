"""NOAH_ENDPOINT_2_FILE: the second endpoint read from a named file.

The file may hold a URL with an access key in it, so every failure here also
checks that no part of the file's content reaches the error text.
"""

import os
import stat
import tempfile
import unittest

from .. import settings as settings_module
from ..settings import BadSetting, Settings, SettingsError, UnknownSetting
from .test_settings import COMPLETE

KEYED_URL = "wss://host.example/CANARYKEY123"
CANARY = "CANARYKEY123"


class EndpointFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name="endpoint-2"):
        return os.path.join(self.dir.name, name)

    def write(self, data, name="endpoint-2"):
        path = self.path(name)
        with open(path, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def mapping(self, path, direct=""):
        return {**COMPLETE, "NOAH_ENDPOINT_2": direct, "NOAH_ENDPOINT_2_FILE": path}

    def refused(self, mapping):
        with self.assertRaises(SettingsError) as caught:
            Settings.from_mapping(mapping)
        message = str(caught.exception)
        self.assertNotIn(CANARY, message)
        return message

    def test_the_new_key_is_known_and_accepted(self):
        self.assertIn("NOAH_ENDPOINT_2_FILE", settings_module.KEYS)
        self.assertIsNone(Settings.from_mapping(COMPLETE).second_chain_endpoint)
        Settings.from_mapping({**COMPLETE, "NOAH_ENDPOINT_2_FILE": ""})

    def test_a_misspelled_file_key_is_still_refused(self):
        with self.assertRaises(UnknownSetting):
            Settings.from_mapping({**COMPLETE, "NOAH_ENDPOINT_2_FIL": "x"})

    def test_the_file_content_is_stripped_and_used(self):
        path = self.write(f"  {KEYED_URL}\n\n")
        settings = Settings.from_mapping(self.mapping(path))
        self.assertEqual(settings.second_chain_endpoint, KEYED_URL)

    def test_relative_path_is_resolved_by_the_working_directory(self):
        self.write(KEYED_URL + "\n")
        previous = os.getcwd()
        os.chdir(self.dir.name)
        self.addCleanup(os.chdir, previous)
        settings = Settings.from_mapping(self.mapping("endpoint-2"))
        self.assertEqual(settings.second_chain_endpoint, KEYED_URL)

    def test_the_file_is_read_on_every_build(self):
        path = self.write("wss://one.example/a\n")
        first = Settings.from_mapping(self.mapping(path))
        self.write("wss://two.example/b\n")
        second = Settings.from_mapping(self.mapping(path))
        self.assertEqual(first.second_chain_endpoint, "wss://one.example/a")
        self.assertEqual(second.second_chain_endpoint, "wss://two.example/b")

    def test_unset_file_keeps_the_direct_setting(self):
        direct = Settings.from_mapping({**COMPLETE, "NOAH_ENDPOINT_2": " wss://a.example:443 "})
        self.assertEqual(direct.second_chain_endpoint, "wss://a.example:443")
        blank = Settings.from_mapping(
            {**COMPLETE, "NOAH_ENDPOINT_2": "wss://a.example:443", "NOAH_ENDPOINT_2_FILE": "  "}
        )
        self.assertEqual(blank.second_chain_endpoint, "wss://a.example:443")

    def test_both_set_is_refused_naming_both_settings_only(self):
        path = self.write(KEYED_URL)
        message = self.refused(self.mapping(path, direct=f"wss://x.example/{CANARY}"))
        self.assertIn("NOAH_ENDPOINT_2 ", message)
        self.assertIn("NOAH_ENDPOINT_2_FILE", message)
        self.assertIn("both", message)
        self.assertNotIn("x.example", message)

    def test_missing_file_names_the_path(self):
        path = self.path("absent")
        message = self.refused(self.mapping(path))
        self.assertIn(path, message)
        self.assertIn("missing", message)

    def test_a_directory_is_not_a_file(self):
        message = self.refused(self.mapping(self.dir.name))
        self.assertIn(self.dir.name, message)
        self.assertIn("not a file", message)

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 000 file")
    def test_unreadable_file(self):
        path = self.write(KEYED_URL)
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, stat.S_IRUSR | stat.S_IWUSR)
        message = self.refused(self.mapping(path))
        self.assertIn(path, message)
        self.assertIn("unreadable", message)

    def test_empty_and_whitespace_only_files(self):
        for content in ("", "  \n\t\n"):
            path = self.write(content)
            message = self.refused(self.mapping(path))
            self.assertIn(path, message)
            self.assertIn("empty", message)

    def test_non_utf8_file_never_echoes_its_content(self):
        path = self.write(CANARY.encode() + b"\xff\xfe")
        message = self.refused(self.mapping(path))
        self.assertIn(path, message)
        self.assertIn("not text", message)

    def test_the_error_is_a_bad_setting(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(self.mapping(self.path("absent")))


if __name__ == "__main__":
    unittest.main()
