"""Plex -> Jellyfin rename tool (package).

The user-facing entry points are the thin scripts plex_rename.py and
plex_undo_rename.py at the repo root, or the console scripts `plex-rename` /
`plex-undo-rename` declared in pyproject.toml. The actual implementation lives
here, split into focused modules:

    common.py    shared helpers (prompts, logs, sanitize, cleanup)
    naming.py    metadata capture + filename building + mapping JSON IO
    connect.py   Plex connection / onboarding
    apply.py     remap onto a local folder, plan, confirm, execute
    jellyfin.py  step 7: migrate watched-state into Jellyfin
    undo.py      reverse a previous apply
    options.py   single source of truth for CLI flags + interactive menu
    models.py    typed records (Entry / LocalItem helpers)
    cli.py       argument parsing + main()
"""

# Single source of truth for the version (was duplicated across modules).
__version__ = "2.0.0"
