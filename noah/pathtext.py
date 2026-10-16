"""How an operator-typed path may appear in a message.

A path setting (NOAH_HISTORY_DB, NOAH_DIALS_JSON, NOAH_PARAMETERS_JSON, the state and ledger paths,
NOAH_ENDPOINT_2_FILE ...) is typed by a person and may hold an endpoint URL with an access key by
mistake, in any shape (``wss:/h/KEY``, ``//h/KEY``, ``h/KEY``, a bare key). A message repeats it
only when it is plainly an absolute filesystem path; anything else is replaced by a fixed text.
"""

import re

NOT_SHOWN = "(a value that is not shown)"
_PLAIN = re.compile(r"/[A-Za-z0-9_./~+-]*")


def plain_path_text(path):
    """``path`` if it is plainly one absolute filesystem path, else ``NOT_SHOWN``."""
    text = str(path)
    if _PLAIN.fullmatch(text) and "//" not in text and ":/" not in text:
        return text
    return NOT_SHOWN


_PLAIN_NAME = re.compile(r"NOAH_[A-Z0-9_]{1,60}")


def plain_name_text(name):
    """An environment variable name as it may appear in a message: only if it is a NOAH_ name."""
    text = str(name)
    return text if _PLAIN_NAME.fullmatch(text) else "(a name that is not shown)"


def derived_path_text(path):
    """Like ``plain_path_text`` for a file whose NAME Noah chose (``<dir>/day-N.json``):
    a directory that is not plainly an absolute path is replaced, but the file name is kept."""
    shown = plain_path_text(path)
    if shown != NOT_SHOWN:
        return shown
    from pathlib import PurePath

    name = PurePath(str(path)).name
    return f"(output directory)/{name}" if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else NOT_SHOWN
