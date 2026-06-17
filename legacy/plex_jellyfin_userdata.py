#!/usr/bin/env python3
"""Backwards-compatible shim. The real code now lives in plexrename.jellyfin.

Importing this module returns the plexrename.jellyfin module object itself, so
existing `import plex_jellyfin_userdata as jf` keeps working AND patching
attributes on it affects the real module the package uses."""

import sys
from plexrename import jellyfin as _jellyfin

sys.modules[__name__] = _jellyfin
