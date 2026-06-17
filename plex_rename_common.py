#!/usr/bin/env python3
"""Backwards-compatible shim. The real code now lives in plexrename.common.

Importing this module returns the plexrename.common module object itself, so
existing `import plex_rename_common as prc` keeps working AND patching
attributes on it (e.g. in tests) affects the real module the package uses."""

import sys
from plexrename import common as _common

sys.modules[__name__] = _common
