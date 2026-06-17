#!/usr/bin/env python3
"""Backwards-compatible launcher / shim for the undo tool.

The implementation lives in plexrename.undo. Run this file directly
(`python3 plex_undo_rename.py ...`) or import it (`import plex_undo_rename`);
in the import case this name resolves to the real plexrename.undo module so
attribute patching in tests reaches the code under test."""

import sys
from plexrename import undo as _undo

if __name__ == "__main__":
    try:
        _undo.main(_undo.parse_args())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
else:
    sys.modules[__name__] = _undo
