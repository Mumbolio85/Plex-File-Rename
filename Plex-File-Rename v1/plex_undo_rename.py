#!/usr/bin/env python3
"""
Reverse the changes made by plex_rename.py (apply phase).

It reads an undo log (lines of "<current path> ––––– <original path>") and moves
each file back to where it was. Processes lines in reverse order so that moves
which happened last are undone first.

Shows a full plan and changes nothing on disk until you confirm. Run with
--dry-run to preview every change without touching any files. Items that are
skipped or fail are recorded in a skip log in ~/Downloads.

This stays a separate tool on purpose: it operates on a log produced by a past
run, so it doesn't belong in the combined export/apply script.

Usage
-----
    plex_undo_rename.py [undo_log.txt] [--dry-run]
"""

import os
import sys
import shutil
import argparse
import datetime

from plex_rename_common import (
    SEP_RE, MKDIR_SENTINEL, DOWNLOADS, RunLog, ask, ask_path, ask_yes_no,
    cleanup_empty_dirs,
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Reverse the changes recorded in a plex_rename undo log.")
    p.add_argument("log", nargs="?", help="Path to the undo log .txt file")
    p.add_argument("--dry-run", action="store_true",
                   help="Show every change without touching any files.")
    return p.parse_args()


def main(args):
    dry_run = args.dry_run
    if dry_run:
        print(">>> DRY RUN: no files will be changed. <<<\n")
    if args.log is not None:
        log = os.path.expanduser(args.log.strip().strip('"').strip("'"))
        if not os.path.isfile(log):
            print(f"Not a file: {log}")
            sys.exit(1)
    else:
        log = ask_path("Path to the undo log: ", must_be_file=True)

    # actions: ("move", current, original) or ("mkdir", folder)
    actions = []
    with open(log, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            parts = SEP_RE.split(line, maxsplit=1)
            if len(parts) != 2:
                continue
            left, right = parts[0].strip(), parts[1].strip()
            if right == MKDIR_SENTINEL:
                actions.append(("mkdir", left))
            else:
                actions.append(("move", left, right))

    if not actions:
        print("No usable entries in the log. Nothing to undo.")
        return

    actions.reverse()  # undo most-recent changes first

    print(f"\nUNDO PLAN ({len(actions)} change(s)):")
    for act in actions:
        if act[0] == "mkdir":
            print(f"  recreate folder: {act[1]}")
        else:
            print(f"  {act[1]}")
            print(f"    -> {act[2]}")

    if dry_run:
        print("\n[DRY RUN] no changes will be made.")
    elif not ask_yes_no(f"\nReverse these {len(actions)} change(s)?"):
        print("Cancelled.")
        return

    run_log = RunLog(
        os.path.join(DOWNLOADS,
                     f"plex_undo_skipped_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"),
        header="Items skipped or failed during undo")
    done = 0
    for act in actions:
        if act[0] == "mkdir":
            folder = act[1]
            if dry_run:
                print(f"  [DRY RUN] would recreate folder: {folder}")
                done += 1
                continue
            try:
                os.makedirs(folder, exist_ok=True)
                done += 1
            except OSError as e:
                run_log.skip("ERROR", f"recreating {folder}: {e}")
            continue

        _, cur, orig = act
        if not os.path.exists(cur):
            run_log.skip("MISSING", cur)
            continue
        if os.path.exists(orig):
            run_log.skip("ORIGINAL EXISTS", orig)
            continue
        if dry_run:
            print(f"  [DRY RUN] {cur}")
            print(f"      -> {orig}")
            done += 1
            continue
        try:
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            shutil.move(cur, orig)
            done += 1
        except OSError as e:
            run_log.skip("ERROR", f"{cur}: {e}")
            continue
        # Folders left empty by these moves are removed once at the end by
        # cleanup_empty_dirs (which also records each removal), so there's no
        # per-file rmdir here.

    print(f"\n{'Would reverse' if dry_run else 'Reversed'} {done} change(s).")

    # Clean up any folders left empty by the moves above.
    dirs = []
    for act in actions:
        if act[0] == "mkdir":
            dirs.append(act[1])
        else:
            dirs.append(os.path.dirname(act[1]))
            dirs.append(os.path.dirname(act[2]))
    dirs = [d for d in dirs if d]
    recreated = [act[1] for act in actions if act[0] == "mkdir"]
    if dirs:
        base = os.path.commonpath(dirs) if len(dirs) > 1 else dirs[0]
        removed = cleanup_empty_dirs(base, keep=recreated, dry_run=dry_run)
        if removed:
            label = "Would remove" if dry_run else "Removed"
            print(f"\n{label} {len(removed)} empty folder(s):")
            for d in removed:
                print(f"  {d}")

    if run_log.created:
        print(f"\nSome items were skipped/failed. See:\n  {run_log.path}")
    run_log.close()


if __name__ == "__main__":
    try:
        main(parse_args())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
