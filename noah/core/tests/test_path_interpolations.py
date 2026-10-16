"""A static guard: in product code, a path-typed value is interpolated into a message only through
``plain_path_text`` / ``derived_path_text`` (or is a fixed, Noah-chosen name). A new bare
``{path}``, or removing the wrapper from an existing one, fails here. This complements the
behavioural tests in test_wrapper_sites.py and test_path_echo_sweep.py, which drive real
branches."""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATHISH = {
    "path",
    "_path",
    "self._path",
    "self.path",
    "keys_dir",
    "keys_path",
    "store_path",
    "args.store_path",
    "dials_path",
    "history_db",
    "scenario_path",
    "mnemonics_path",
    "out_path",
    "score_path",
    "fixture_path",
    "existing_path",
    "score_file",
    "state_path",
    "lock_path",
    "hold.path",
    "backoff.path",
    "args.compare_with",
    "args.config",
    "mnemonics_path",
    "db_path",
    "self._publish_intent.path",
    "config.scenario_path",
    "config.keys_dir",
    "self._commit_ledger.path",
    "self._record_dir",
}
PATH_NAME = re.compile(r"(path|paths|dir|file|root|ledger|db|store|hold|intent)$", re.IGNORECASE)
WRAPPERS = {"plain_path_text", "derived_path_text", "_shown", "_shown_path"}
# Files and the (line-free) reasons a bare name there is not an operator-typed path.
SKIP_FILES = {
    "scripts/tests",
    "noah/core/tests",
    "noah/chain/tests",
    "tests",
}
# Names that match the pattern but are not an operator-typed path: a Noah-chosen temporary file,
# the secret scanner's own report of the files it was given, a ledger OBJECT in a type message.
EXEMPT = {
    ("noah/ark/registration.py", "ledger"): "an object named in a type error",
    ("noah/deposits/scenario_keys.py", "generated_keys"): "not a path",
    # (file, name): why
    ("noah/core/ts_engine.py", "out_path"): "a temporary file Noah itself creates",
    ("noah/secret_scan/__init__.py", "self.path"): "the scanner's report of the files it was given",
}


def is_pathish(name):
    if not name:
        return False
    return name in PATHISH or bool(PATH_NAME.search(name.split(".")[-1]))


def name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = name_of(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def product_files():
    for base in ("noah", "scripts", "neurons"):
        for path in sorted((ROOT / base).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in relative or relative.startswith("tests/") or ".venv" in relative:
                continue
            yield path, relative


class PathsAreWrappedInMessages(unittest.TestCase):
    def test_no_bare_path_in_an_f_string_message(self):
        offenders = []
        for path, relative in product_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr):
                    continue
                first = node.values[0] if node.values else None
                if isinstance(first, ast.Constant) and str(first.value).startswith("file:"):
                    continue  # a sqlite URI: functional, never printed
                for part in node.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    value = part.value
                    if (
                        isinstance(value, ast.Call)
                        and name_of(value.func) in {"str", "repr", "os.fspath"}
                        and value.args
                    ):
                        value = value.args[0]  # {str(path)} is as bare as {path}
                    name = name_of(value)
                    if is_pathish(name) and (relative, name) not in EXEMPT:
                        offenders.append(f"{relative}:{node.lineno} {{{name}}}")
        self.assertEqual(offenders, [], "bare path interpolated; wrap it in plain_path_text")

    def test_no_bare_path_handed_to_a_logger_or_print(self):
        offenders = []
        for path, relative in product_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = name_of(node.func) or ""
                if func.split(".")[-1] not in {"info", "warning", "error", "debug", "print"}:
                    continue
                if func.startswith(("os.", "self._conn")):
                    continue
                for argument in node.args[1:] if func != "print" else node.args:
                    value = argument
                    if (
                        isinstance(value, ast.Call)
                        and name_of(value.func) in {"str", "repr"}
                        and value.args
                    ):
                        value = value.args[0]
                    if is_pathish(name_of(value)) and (relative, name_of(value)) not in EXEMPT:
                        offenders.append(f"{relative}:{node.lineno} {name_of(value)}")
        self.assertEqual(offenders, [], "bare path handed to a logger or print")


class TheGuardRunsBeforeLoggingIsConfigured(unittest.TestCase):
    def test_every_basicconfig_is_preceded_by_the_guard_in_the_same_block(self):
        """A handler bound to the raw sys.stderr before the guard wraps it would bypass the stream
        wrapper (the log scrub still covers the records, but the order is the primary fix)."""
        offenders = []

        def has_guard(node):
            return any(
                isinstance(c, ast.Call) and name_of(c.func) == "guard_endpoint_logging"
                for c in ast.walk(node)
            )

        def has_basicconfig(node):
            return any(
                isinstance(c, ast.Call) and (name_of(c.func) or "").endswith("basicConfig")
                for c in ast.walk(node)
            )

        for path, relative in product_files():
            text = path.read_text()
            if "basicConfig" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                for field in ("body", "orelse", "finalbody"):
                    block = getattr(node, field, None)
                    if not isinstance(block, list):
                        continue
                    for index, statement in enumerate(block):
                        if isinstance(statement, ast.Expr) and has_basicconfig(statement):
                            if not any(
                                isinstance(earlier, ast.Expr) and has_guard(earlier)
                                for earlier in block[:index]
                            ):
                                offenders.append(f"{relative}:{statement.lineno}")
        self.assertEqual(offenders, [], "basicConfig without a guard earlier in its block")


if __name__ == "__main__":
    unittest.main()
