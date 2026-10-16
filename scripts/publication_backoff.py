#!/usr/bin/env python3
"""Show or reset the proxy route's failed-send limit (issue #16, proxy publication).

    publication_backoff.py status
    publication_backoff.py reset --evidence-note "<what the failure was and what was fixed>"

After repeated failed sends (each may cost the proxy signer a fee) the validator stops sending and
reports PUBLICATION FAILURE LIMIT. A person reads the failure first (the validator log, the
delegation, the signer balance), then `reset` archives the record (never deletes it) with the note.
The file is `publish-backoff.json` in the validator's state directory (`NOAH_PUBLISH_BACKOFF_PATH`
overrides it), the same place the validator and `day_close_pipeline.py --publish` read.
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
    from neurons.validator import _default_state_dir, _publish_backoff_path
    from noah.chain.sources import QuietArgumentParser
    from noah.core.proxy_route import PublishFailureBackoff
    from noah.pathtext import plain_path_text

    parser = QuietArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("reset").add_argument("--evidence-note", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    backoff = PublishFailureBackoff(_publish_backoff_path(_default_state_dir()))
    try:
        if args.command == "status":
            doc = backoff.load()
            print("no failed sends recorded" if doc is None else f"FAILURES RECORDED ({plain_path_text(backoff.path)}): {doc}")
            return 0 if doc is None else 3
        print(f"failure record archived at {plain_path_text(backoff.reset(args.evidence_note))}")
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
