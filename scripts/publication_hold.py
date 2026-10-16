#!/usr/bin/env python3
"""Set, show or release the publication hold (issue #291/#235).

    publication_hold.py set --reason "restored from backup of <date>"
    publication_hold.py status
    publication_hold.py release --evidence-note "<what the chain scan showed>"

The restore procedure runs `set`. Nothing creates or removes the hold automatically, and
`release` archives it (never deletes). The file is `publish-hold.json` in the validator's state
directory (`NOAH_PUBLISH_HOLD_PATH` overrides it), the same place the validator and
`day_close_pipeline.py --publish` read.
"""

import argparse
import sys
from pathlib import Path

_SOFTWARE_NOAH = Path(__file__).resolve().parents[1]
if str(_SOFTWARE_NOAH) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_NOAH))


def main(argv=None):
    from noah.chain.sources import guard_endpoint_logging

    guard_endpoint_logging()
    from neurons.validator import _default_state_dir, _publish_hold_path
    from noah.chain.sources import QuietArgumentParser
    from noah.core.publish_hold import PublishHold
    from noah.pathtext import plain_path_text

    parser = QuietArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("set").add_argument("--reason", required=True)
    sub.add_parser("release").add_argument("--evidence-note", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    hold = PublishHold(_publish_hold_path(_default_state_dir()))
    try:
        if args.command == "status":
            doc = hold.load()
            print("no hold" if doc is None else f"ON HOLD ({plain_path_text(hold.path)}): {doc}")
            return 0 if doc is None else 3
        if args.command == "set":
            print(f"hold set at {plain_path_text(hold.path)}: {hold.set(args.reason)}")
            return 0
        print(f"hold archived at {plain_path_text(hold.release(args.evidence_note))}")
        return 0
    except (ValueError, FileNotFoundError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        # An OS error's text repeats the typed path.
        print(f"REFUSED: the file could not be used ({type(error).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
