#!/usr/bin/env python3
"""
Shared helpers for the Plex rename tools.

Used across the package (apply, undo, jellyfin) so the apply and undo steps
always agree on the mapping/undo-log format (the separator, the MKDIR sentinel)
and behave the same way for prompts and empty-folder cleanup.
"""

import os
import re
import sys
import shutil
import datetime
import tempfile


# --------------------------------------------------------------------------- #
# Progress reporting
# --------------------------------------------------------------------------- #
def make_progress(label, total):
    """Return a callable `progress(n)` for showing 'label n of total' while a
    long loop runs.

    On a real terminal it updates a single line in place (carriage return). When
    stdout is NOT a tty (an IDE 'Run' output pane, a pipe, a log file), a bare
    carriage return isn't rendered live -- the line gets buffered and only shows
    up once a newline is finally written, i.e. after the work is done. So in that
    case we print throttled, newline-terminated lines instead, which appear
    incrementally everywhere.

    `progress(n)` returns True if it left the cursor mid-line (tty case), so the
    caller can finish the line / break before printing other messages."""
    is_tty = sys.stdout.isatty()
    step = max(1, total // 50)  # at most ~50 updates in the non-tty case

    def progress(n):
        if is_tty:
            print(f"\r  {label} {n} of {total}...   ", end="", flush=True)
            return True
        if n == 1 or n == total or n % step == 0:
            print(f"  {label} {n} of {total}...", flush=True)
        return False

    return progress

# --------------------------------------------------------------------------- #
# Filename sanitising
# --------------------------------------------------------------------------- #
INVALID_CHARS = '<>:"/\\|?*'

# Strips the filesystem-illegal characters above plus ASCII control characters
# (U+0000-U+001F), which are also illegal in filenames on Windows and can break
# tooling on other platforms.
_STRIP_TABLE = {ord(ch): None for ch in INVALID_CHARS}
_STRIP_TABLE.update({c: None for c in range(0x20)})


def sanitize(name):
    return name.translate(_STRIP_TABLE).strip()


# --------------------------------------------------------------------------- #
# Mapping / undo-log format
# --------------------------------------------------------------------------- #
# What we WRITE between fields.
SEP = " ––––– "

# What we ACCEPT when reading back: tolerant of dash type/count and whitespace
# so hand-edited files still parse.
SEP_RE = re.compile(r"\s+[–—\-]{3,}\s+")

# Right-hand sentinel in the undo log marking a folder that was removed during
# apply and should be recreated on undo.
MKDIR_SENTINEL = "[[MKDIR]]"

# Right-hand sentinel in the undo log marking a step-7 watched-state write. The
# left field is "<server_url>|<user_id>|<item_id>" and the JSON of the item's
# PRIOR Jellyfin UserData follows the sentinel, so undo can restore it.
USERDATA_SENTINEL = "[[USERDATA]]"

# Right-hand sentinel for a file created by step 8 (artwork download). On undo
# the file at the left-hand path is deleted if it still exists. Only new files
# are recorded; overwrites of pre-existing images are not, because the prior
# content cannot be restored.
DELETE_SENTINEL = "[[DELETE]]"

DOWNLOADS = os.path.expanduser("~/Downloads")

# Filenames that don't count as "real" contents when deciding whether a folder
# is empty (so a folder holding only OS/NAS cruft is still treated as empty and
# cleaned up). Compared case-insensitively.
JUNK_FILENAMES = {".ds_store", "thumbs.db", "desktop.ini", ".directory"}
# Directory names (e.g. Synology's per-folder index) treated likewise.
JUNK_DIRNAMES = {"@eadir"}


def ensure_writable_dir(path):
    """Return a directory that can be written to for logs/output.

    Prefers `path`; if it doesn't exist, tries to create it; if that fails,
    falls back to the system temp directory (with a printed warning) so a run
    never crashes just because, say, ~/Downloads is missing on this machine."""
    path = os.path.expanduser(path or "")
    if os.path.isdir(path):
        return path
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        fallback = tempfile.gettempdir()
        print(f"  Note: '{path}' isn't writable; writing logs to {fallback} "
              "instead. Use --log-dir to choose another location.")
        return fallback


# --------------------------------------------------------------------------- #
# Run log (skipped / failed items)
# --------------------------------------------------------------------------- #
class RunLog:
    """Records skipped/failed items to stdout and, lazily, to a log file in
    ~/Downloads. The file is only created if something is actually skipped."""

    def __init__(self, path, header="Items skipped or failed"):
        self.path = path
        self.header = header
        self.fh = None

    def skip(self, category, detail):
        print(f"  {category}, skipped: {detail}")
        if self.fh is None:
            self.fh = open(self.path, "w", encoding="utf-8")
            self.fh.write(f"# {self.header} "
                          f"({datetime.datetime.now():%Y-%m-%d %H:%M:%S})\n")
        self.fh.write(f"[{category}] {detail}\n")
        self.fh.flush()

    @property
    def created(self):
        return self.fh is not None

    def close(self):
        if self.fh is not None:
            self.fh.close()


class UndoLog:
    """Lazy undo log: the file is only created when the first entry is written,
    so an empty undo log is never left on disk after a run where nothing moved."""

    def __init__(self, path):
        self.path = path
        self.fh = None

    def write(self, text):
        if self.fh is None:
            self.fh = open(self.path, "w", encoding="utf-8")
        self.fh.write(text)

    def flush(self):
        if self.fh is not None:
            self.fh.flush()

    @property
    def created(self):
        return self.fh is not None

    def close(self):
        if self.fh is not None:
            self.fh.close()


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #
def ask(prompt):
    return input(prompt).strip()


def clean_path_input(p):
    """Normalise a pasted path: trim whitespace and any surrounding quotes or
    backticks (common shell copy/paste artifacts), then expand a leading '~'."""
    p = p.strip().strip('"').strip("'").strip("`").strip()
    return os.path.expanduser(p)


def ask_path(prompt, must_be_file=False, must_be_dir=False):
    while True:
        p = clean_path_input(ask(prompt))
        if not p:
            print("  Please enter a path.")
            continue
        if must_be_file and not os.path.isfile(p):
            print(f"  Not a file: {p}")
            continue
        if must_be_dir and not os.path.isdir(p):
            print(f"  Not a folder: {p}")
            continue
        return p


def ask_yes_no(prompt, default="n"):
    # Capitalise whichever answer is the default so pressing Enter is clear.
    suffix = " [Y/n]: " if default.lower() in ("y", "yes") else " [y/N]: "
    while True:
        a = ask(prompt + suffix).lower()
        if not a:
            a = default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_choice(prompt, options):
    """options: list of (key, label). Returns the chosen key."""
    print(prompt)
    for key, label in options:
        print(f"  [{key}] {label}")
    keys = {k.lower() for k, _ in options}
    while True:
        a = ask("Choice: ").lower()
        if a in keys:
            return a
        print("  Invalid choice, try again.")


def ask_multichoice(prompt, options):
    """Like ask_choice, but lets the user pick zero or more options by number
    (comma/space separated). options: list of (key, label). Returns the chosen
    keys as a list in option order, with duplicates and out-of-range numbers
    ignored; a blank answer returns an empty list."""
    print(prompt)
    for i, (_, label) in enumerate(options, start=1):
        print(f"  [{i}] {label}")
    raw = ask("Enter the numbers you want (comma/space separated), "
              "or blank for none: ")
    chosen = []
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(options):
            key = options[int(tok) - 1][0]
            if key not in chosen:
                chosen.append(key)
    return chosen


# --------------------------------------------------------------------------- #
# Empty-folder cleanup (shared by apply and undo)
# --------------------------------------------------------------------------- #
def _is_effectively_empty(contents):
    """True if a folder's listing holds nothing but OS/NAS junk files/dirs."""
    for name in contents:
        if name.lower() in JUNK_FILENAMES:
            continue
        if name.lower() in JUNK_DIRNAMES:
            continue
        return False
    return True


def cleanup_empty_dirs(root, undo_log=None, keep=None, dry_run=False):
    """Remove empty folders under root (bottom-up). A folder holding only junk
    (a stray .DS_Store, Thumbs.db, @eaDir, ...) counts as empty. The root itself
    is never removed, and any folder in `keep` (e.g. folders just recreated on
    undo) is preserved.

    If `undo_log` is given, each removed folder is recorded with the MKDIR
    sentinel so it can be recreated later."""
    removed = []
    keep = {os.path.abspath(p) for p in (keep or [])}
    root = os.path.abspath(root)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        abs_dir = os.path.abspath(dirpath)
        if abs_dir == root or abs_dir in keep:
            continue
        try:
            contents = os.listdir(dirpath)
        except OSError:
            continue
        if contents and _is_effectively_empty(contents):
            if dry_run:
                contents = []
            else:
                # Remove the junk so the directory can actually be rmdir'd.
                for name in list(contents):
                    try:
                        junk = os.path.join(dirpath, name)
                        if os.path.isdir(junk):
                            shutil.rmtree(junk)
                        else:
                            os.remove(junk)
                    except OSError:
                        pass
                try:
                    contents = os.listdir(dirpath)
                except OSError:
                    continue
        if not contents:
            if dry_run:
                print(f"  [DRY RUN] would remove empty folder: {dirpath}")
                removed.append(dirpath)
                continue
            try:
                os.rmdir(dirpath)
                removed.append(dirpath)
                if undo_log is not None:
                    undo_log.write(f"{dirpath}{SEP}{MKDIR_SENTINEL}\n")
                    undo_log.flush()
            except OSError:
                pass
    return removed
