#!/usr/bin/env python3
"""
Plex -> Jellyfin rename tool.

This file is the historical entry point and a backwards-compatible facade: the
implementation now lives in the `plexrename` package, split into focused
modules (common, naming, connect, apply, jellyfin, undo, options, cli). The
public names are re-exported here so older imports such as
`import plex_rename as pr` continue to work, and running this file directly
(`python3 plex_rename.py ...`) still launches the tool.

See the package docstrings and the README for the full description of how the
two phases (export from Plex, apply onto a local folder) and the optional
Jellyfin restructure + watched-state migration (step 7) fit together.

Requires: pip install plexapi
"""

from plexrename import __version__  # noqa: F401

# Re-export the public API for backwards compatibility. Each module's public
# names become attributes of this module so existing `pr.<name>` references
# keep resolving. (Tests that *patch* a name should patch it on the module that
# defines it -- e.g. plexrename.common for the prompt helpers -- since that's
# where the code looks it up.)
from plexrename.common import *      # noqa: F401,F403
from plexrename.naming import *      # noqa: F401,F403
from plexrename.connect import *     # noqa: F401,F403
from plexrename.apply import *       # noqa: F401,F403
from plexrename.options import *     # noqa: F401,F403
from plexrename.cli import *         # noqa: F401,F403

from plexrename.cli import entrypoint


if __name__ == "__main__":
    entrypoint()
