#!/usr/bin/env python
"""Check a vertical's config files hang together. No database, no network.

    python scripts/validate_vertical.py <vertical> [<vertical> ...]
    python scripts/validate_vertical.py --all

Exit code 1 when any vertical named has a failing check.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def main(argv) -> int:
    from utils.vertical_registry import SUPPORTED_VERTICALS
    from utils.vertical_validation import render, validate_vertical
    names = sorted(SUPPORTED_VERTICALS) if argv == ['--all'] else argv
    if not names:
        print(__doc__)
        return 2
    ok = True
    for name in names:
        report = validate_vertical(name)
        print(render(report))
        ok = ok and report['ok']
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
