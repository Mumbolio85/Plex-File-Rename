#!/usr/bin/env python3
"""
Command-line entry point: parse options, optionally run the interactive
settings picker, obtain the mapping (from Plex or a saved file), and drive the
apply / export / standalone-migrate flows.
"""

from __future__ import annotations

import os
import sys

from plexrename import common
from plexrename.options import (
    parse_args, configure_interactively, default_export_path,
    reset_for_next_library,
)
from plexrename.connect import connect, choose_library
from plexrename.naming import collect_entries, write_mapping, read_mapping
from plexrename.apply import apply_mapping


def resolve_input_path(value, prompt, must_be_file=False, must_be_dir=False):
    """Use a path passed on the command line if given (validated), otherwise
    fall back to the interactive prompt."""
    if value is None:
        return common.ask_path(prompt, must_be_file=must_be_file,
                               must_be_dir=must_be_dir)
    p = common.clean_path_input(value)
    if must_be_file and not os.path.isfile(p):
        print(f"Not a file: {p}")
        sys.exit(1)
    if must_be_dir and not os.path.isdir(p):
        print(f"Not a folder: {p}")
        sys.exit(1)
    return p


def ensure_plexapi():
    """Fail fast (before any prompts) if plexapi isn't installed, so the user
    doesn't answer connection questions only to hit an ImportError later."""
    try:
        import plexapi  # noqa: F401
    except ImportError:
        print("This step needs the 'plexapi' package, which isn't installed.\n"
              "Install it with:\n  pip install plexapi")
        sys.exit(1)


def confirm_unattended(assume_yes, dry_run):
    """The single 'are you sure?' gate for a --yes run that will actually change
    files. Returns True to proceed. A dry run never needs it; without --yes the
    per-step prompts do the confirming."""
    if not assume_yes or dry_run:
        return True
    print("\n!!! --yes given: the per-step confirmation prompts will be SKIPPED. !!!")
    print("Files on disk WILL be renamed/moved (and watched-state written) "
          "according to the plan shown for each step.")
    print("If you haven't already, cancel now and do a --dry-run first.")
    return common.ask_yes_no("Proceed without per-step confirmations?", default="n")


def run_apply_phase(entries, args, dry_run, log_dir):
    """Phase 2 driver: warn on a single-item mapping, ask where the files live
    locally, and apply."""
    if len(entries) == 1:
        print("\nWARNING: the mapping has only ONE item. The folder structure "
              "is inferred by comparing the paths of multiple items, so with a "
              "single item it can't be detected reliably and the file may not "
              "be matched on disk. Use a library with more than one item for "
              "dependable results.")
    print("\nNow tell the script where these same files can be accessed from this computer.\n")
    print("This is the folder you'd open in Finder/Explorer to see the actual")
    print("movie or show files -- e.g. /Volumes/Media/Movies or D:\\Media\\Movies.")
    library_folder = resolve_input_path(
        args.library,
        "Folder on this computer that contains your media files: ",
        must_be_dir=True)
    apply_mapping(entries, library_folder, dry_run, log_dir,
                  force=getattr(args, "force", False),
                  assume_yes=getattr(args, "yes", False),
                  skip_step7=getattr(args, "skip_step7", False),
                  skip_step8=getattr(args, "skip_step8", False))


def run_standalone_artwork(args, dry_run, log_dir):
    """Step 8 standalone (--copy-artwork): read a saved mapping and download
    Plex artwork into the media folders. The artwork URLs (with auth token) are
    embedded in the mapping, so no Plex reconnection is needed. Kept separate
    from apply so it never touches media files."""
    import datetime
    from plexrename.artwork import copy_artwork

    mapping_file = resolve_input_path(
        args.from_mapping, "Path to the saved mapping (.json): ",
        must_be_file=True)
    entries = read_mapping(mapping_file)
    if not entries:
        print("No usable entries found in the mapping file. Exiting.")
        return
    if not any(e.get("plex_thumb_url") or e.get("plex_show_thumb_url")
               for e in entries):
        print("This mapping has no captured artwork URLs. Export a fresh "
              "mapping with this version (v2.1+), which records artwork URLs "
              "during the Plex scan.")
        sys.exit(1)

    log_dir = common.ensure_writable_dir(log_dir or common.DOWNLOADS)
    migrated_log_path = os.path.join(log_dir, "plex_jf_migrated.json")
    if not os.path.isfile(migrated_log_path):
        print("Step 8 (copy artwork) requires step 7 (migrate watched-state) "
              "to have been run first.\n"
              f"No migration log found at: {migrated_log_path}\n"
              "Run --migrate-watched first, then retry --copy-artwork.")
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log = common.RunLog(
        os.path.join(log_dir, f"plex_artwork_skipped_{stamp}.txt"),
        header="Items skipped/failed during artwork copy")
    try:
        copy_artwork(entries, dry_run, run_log, overwrite=False)
    finally:
        run_log.close()
    if run_log.created:
        print(f"Some items were skipped/failed. See:\n  {run_log.path}")


def run_standalone_migrate(args, dry_run, log_dir):
    """Step 7 on its own (--migrate-watched): read a saved mapping, connect to
    Jellyfin, and migrate watched-state. Matching is provider-ID-first with a
    filename fallback, so it works even when this machine's recorded paths don't
    line up with the Jellyfin server's. Kept separate from apply so it never
    touches files. Logs/undo mirror the apply phase's conventions."""
    import datetime
    from plexrename.jellyfin import (
        connect_jellyfin, choose_jellyfin_user, migrate_watched, MigratedLog,
    )
    if not args.from_mapping:
        print("--migrate-watched needs --from-mapping (a saved mapping JSON, "
              "e.g. an export or a plex_rename_applied_*.json).")
        sys.exit(1)
    mapping_file = resolve_input_path(
        args.from_mapping, "Path to the saved mapping (.json): ",
        must_be_file=True)
    entries = read_mapping(mapping_file)
    if not entries:
        print("No usable entries found in the mapping file. Exiting.")
        return
    if not any(e.get("watched_state") for e in entries):
        print("This mapping has no captured watched-state, so there's nothing "
              "to migrate. Export a fresh mapping with this version (v2.0+), "
              "which records watched-state during the Plex scan.")
        sys.exit(1)

    client = connect_jellyfin()
    user_id = choose_jellyfin_user(client)
    if not user_id:
        print("No Jellyfin user available. Exiting.")
        return

    log_dir = common.ensure_writable_dir(log_dir or common.DOWNLOADS)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log = common.RunLog(
        os.path.join(log_dir, f"plex_migrate_skipped_{stamp}.txt"),
        header="Items skipped/failed during watched-state migration")
    if dry_run:
        undo_log, undo_path = None, None
    else:
        undo_path = os.path.join(log_dir, f"plex_migrate_undo_{stamp}.txt")
        undo_log = common.UndoLog(undo_path)
    migrated_log = MigratedLog(os.path.join(log_dir, "plex_jf_migrated.json"))
    try:
        migrate_watched(entries, client, user_id, dry_run=dry_run,
                        undo_log=undo_log, run_log=run_log,
                        migrated_log=migrated_log, force=args.force,
                        provider_first=True)
    finally:
        if undo_log is not None:
            undo_log.close()
        run_log.close()
    if run_log.created:
        print(f"Some items were skipped/failed. See:\n  {run_log.path}")
    if undo_log is not None and undo_log.created:
        print(f"To reverse, run plex_undo_rename.py on:\n  {undo_path}")


def main(args):
    print("=== Plex -> Jellyfin rename tool ===")
    # With no flags given, offer the interactive settings picker first.
    if not (args.dry_run or args.export_only or args.export_file
            or args.from_mapping or getattr(args, "migrate_watched", False)
            or getattr(args, "copy_artwork", False)):
        configure_interactively(args)

    dry_run = args.dry_run
    if dry_run:
        print(">>> DRY RUN: no files will be changed. <<<")
    print()

    # Where undo/skip logs go (validated up front so a bad path fails early).
    log_dir = getattr(args, "log_dir", None)
    if log_dir:
        log_dir = os.path.expanduser(log_dir.strip().strip('"').strip("'"))
        if not os.path.isdir(log_dir):
            print(f"--log-dir is not a folder: {log_dir}")
            sys.exit(1)

    # Single confirmation gate for an unattended (--yes) run that will change
    # files; a dry run is exempt.
    if not confirm_unattended(getattr(args, "yes", False), dry_run):
        print("Cancelled.")
        return

    # Step 7 standalone: migrate watched-state from a saved mapping, no files.
    if getattr(args, "migrate_watched", False):
        run_standalone_migrate(args, dry_run, log_dir)
        return

    # Step 8 standalone: copy Plex artwork from a saved mapping, no files moved.
    if getattr(args, "copy_artwork", False):
        run_standalone_artwork(args, dry_run, log_dir)
        return

    # --- Obtain the mapping (entries) ---
    if args.from_mapping:
        # Old "apply a pre-made list" path: skip Plex entirely.
        mapping_file = resolve_input_path(
            args.from_mapping, "Path to the saved mapping (.json) file: ", must_be_file=True)
        entries = read_mapping(mapping_file)
        if not entries:
            print("No usable entries found in the mapping file. Exiting.")
            return
        print(f"Loaded {len(entries)} entries from {mapping_file}.")
        run_apply_phase(entries, args, dry_run, log_dir)
        return

    # Phase 1: connect to Plex and build the mapping in memory.
    ensure_plexapi()
    plex = connect()
    while True:
        section = choose_library(plex)
        print(f"\nScanning '{section.title}'...")
        entries = collect_entries(section)
        if not entries:
            print("No movies or shows found in that library.")
        else:
            print(f"Built mapping for {len(entries)} item(s).")

            # Optionally (or, with --export-only/--export-file, definitely) write
            # the mapping out as a reviewable/resumable artifact.
            if args.export_only or args.export_file:
                out_path = args.export_file or default_export_path()
                write_mapping(entries, os.path.expanduser(out_path))
            elif common.ask_yes_no("Save the mapping (with all Plex metadata) to a JSON "
                                   "file first (optional)?", default="n"):
                write_mapping(entries, default_export_path())

            if args.export_only:
                print("\nExport-only mode: done with this library.")
            else:
                run_apply_phase(entries, args, dry_run, log_dir)

        if not common.ask_yes_no("\nProcess another Plex library on this server?",
                                 default="n"):
            break
        # Each further library prompts fresh for its own folder/export path.
        reset_for_next_library(args)


def entrypoint():
    """Console-script / `python -m` entry point."""
    try:
        main(parse_args())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
