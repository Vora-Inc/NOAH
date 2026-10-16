import sys
import traceback

from .cli import finish, main

try:
    _code = main()
except SystemExit as _exit:  # argparse usage errors and the like
    _code = _exit.code
except BaseException:
    traceback.print_exc()
    _code = 1
finish(_code)
