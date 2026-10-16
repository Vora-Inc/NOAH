"""CLI: ``python3 -m noah.secret_scan [options] <file-or-dir> ...``

Exit 0 clean, 3 hit or an unexpected refusal, 2 error or nothing scanned. Prints
``path:line: kind`` (names masked) and a summary line; never the matched text.
Options: ``--release`` (release extensions), ``--json`` (machine-readable summary on
stdout only), ``--strict`` (an expected-refused name that is absent also fails),
``--excluded PATH`` and ``--expected-refused NAME`` (repeatable), ``--config FILE`` (JSON
with ``excluded``, ``expected_refused``, ``scope``, ``strict``).
"""

import json
import sys

from . import EXIT_CLEAN, EXIT_ERROR, ScanError, scan


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    scope, as_json, strict = "base", False, False
    excluded, expected, paths = [], [], []
    i = 0
    try:
        while i < len(argv):
            a = argv[i]
            if a == "--release":
                scope = "release"
            elif a == "--json":
                as_json = True
            elif a == "--strict":
                strict = True
            elif a in ("--excluded", "--expected-refused", "--config"):
                i += 1
                if i >= len(argv):
                    raise ScanError(f"{a} needs a value")
                if a == "--excluded":
                    excluded.append(argv[i])
                elif a == "--expected-refused":
                    expected.append(argv[i])
                else:
                    with open(argv[i], encoding="utf-8") as fh:
                        cfg = json.load(fh)
                    excluded += cfg.get("excluded", [])
                    expected += cfg.get("expected_refused", [])
                    scope = cfg.get("scope", scope)
                    strict = bool(cfg.get("strict", strict))
            elif a.startswith("--"):
                raise ScanError(f"unknown option {a}")
            else:
                paths.append(a)
            i += 1
        if not paths:
            raise ScanError("usage: python3 -m noah.secret_scan [options] <file-or-dir> ...")
        report = scan(paths, scope=scope, excluded=excluded, expected_refused=expected, strict=strict)
    except (ScanError, OSError, ValueError) as why:
        print(f"SECRET SCAN ERROR: {why if isinstance(why, ScanError) else type(why).__name__}", file=sys.stderr)
        return EXIT_ERROR
    code = report.exit_code
    if as_json:
        print(json.dumps(report.as_dict(), indent=1, sort_keys=True))
        return code
    if report.scanned == 0:
        print("SECRET SCAN ERROR: nothing was scanned", file=sys.stderr)
    elif code == EXIT_CLEAN:
        print("SECRET SCAN: clean")
    else:
        print(f"SECRET SCAN: REFUSED ({len(report.findings)} finding(s); matched text is never shown)")
    for f in report.findings:
        print(f"  {f}")
    for name in report.expected_missing:
        print(f"  note: expected-refused name not present: {name}")
    print(report.summary_line())
    return code


if __name__ == "__main__":
    sys.exit(main())
