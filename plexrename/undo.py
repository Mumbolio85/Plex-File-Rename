#!/usr/bin/env python3
"""
Reverse the changes made by the apply phase.

It reads an undo log (lines of "<current path> ––––– <original path>") and moves
each file back to where it was. Processes lines in reverse order so that moves
which happened last are undone first.

A step-7 run also records watched-state writes in the same log
("<url>|<user>|<item> ––––– [[USERDATA]] {prior UserData}"). When undo meets one
of those it connects to that Jellyfin server (once, prompting only then) and
writes the prior UserData back. A file-only log never prompts for Jellyfin.

Shows a full plan and changes nothing on disk until you confirm. Run with
--dry-run to preview every change without touching any files. Items that are
skipped or fail are recorded in a skip log in ~/Downloads.

Usage
-----
    plex_undo_rename.py [undo_log.txt] [--dry-run]
"""

import os
import sys
import json
import shutil
import argparse
import datetime

from plexrename.common import (
    SEP_RE, MKDIR_SENTINEL, USERDATA_SENTINEL, DOWNLOADS, RunLog, ask, ask_path,
    ask_yes_no, cleanup_empty_dirs, ensure_writable_dir,
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

    # actions: ("move", current, original), ("mkdir", folder), or
    # ("userdata", "<url>|<user>|<item>", prior_userdata_dict)
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
            elif right.startswith(USERDATA_SENTINEL):
                try:
                    payload = json.loads(right[len(USERDATA_SENTINEL):].strip())
                except ValueError:
                    continue
                actions.append(("userdata", left, payload))
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
        elif act[0] == "userdata":
            print(f"  restore Jellyfin watched-state: {act[1]}")
        else:
            print(f"  {act[1]}")
            print(f"    -> {act[2]}")

    if dry_run:
        print("\n[DRY RUN] no changes will be made.")
    elif not ask_yes_no(f"\nReverse these {len(actions)} change(s)?"):
        print("Cancelled.")
        return

    run_log = RunLog(
        os.path.join(ensure_writable_dir(DOWNLOADS),
                     f"plex_undo_skipped_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"),
        header="Items skipped or failed during undo")

    # Watched-state records (step 7) are restored over Jellyfin's REST API. We
    # only connect when such a record is actually encountered, and cache one
    # client per server URL, so a file-only undo log never prompts for Jellyfin.
    jf_clients = {}

    def jf_client_for(server_url):
        if server_url not in jf_clients:
            from plexrename.jellyfin import connect_jellyfin
            print(f"\nThis undo restores Jellyfin watched-state on {server_url}.")
            print("Connect to that Jellyfin server to continue.")
            jf_clients[server_url] = connect_jellyfin()
        return jf_clients[server_url]

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

        if act[0] == "userdata":
            _, ident, payload = act
            ident_parts = ident.rsplit("|", 2)
            if len(ident_parts) != 3:
                run_log.skip("ERROR", f"unparseable watched-state record: {ident}")
                continue
            server_url, user_id, item_id = ident_parts
            if dry_run:
                print(f"  [DRY RUN] would restore watched-state for item "
                      f"{item_id} on {server_url}")
                done += 1
                continue
            try:
                client = jf_client_for(server_url)
                client.set_user_data(user_id, item_id, payload)
                done += 1
            except Exception as e:
                run_log.skip("ERROR", f"restoring watched-state {item_id}: {e}")
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
        elif act[0] == "move":
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


def entrypoint():
    """Console-script / `python -m` entry point."""
    try:
        main(parse_args())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
