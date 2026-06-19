#!/usr/bin/env python3
"""
Single source of truth for the tool's options.

Both the command-line flags (argparse) and the interactive settings menu are
defined here, side by side, so they can't drift apart. The per-library reset
used by the "process another library" loop also lives here, so adding a new
option means touching exactly one file.
"""

import os
import datetime

from plexrename import common, __version__

# --------------------------------------------------------------------------- #
# The interactive menu. Each entry mirrors a command-line flag. `key` matches
# the argparse dest; `label` is shown in the menu. Options that take a value
# (export file, mapping path, log dir) are handled in apply_menu_choices below.
# --------------------------------------------------------------------------- #
MENU_OPTIONS = [
    ("dry-run",          "Dry run — preview every change, touch nothing"),
    ("export",           "Save the Plex mapping (with all metadata) to a JSON file"),
    ("export-only",      "Export only — build the mapping, then stop (no apply)"),
    ("from-mapping",     "Apply from an existing mapping file (skip Plex) — also select this to run step 7 or step 8 standalone"),
    ("log-dir",          "Choose where undo/skip logs are written (default: ~/Downloads)"),
    ("skip-step7",       "Skip step 7 — don't offer to migrate Plex watched-state into Jellyfin after organizing"),
    ("skip-step8",       "Skip step 8 — don't offer to copy Plex artwork into the media folders"),
]


def default_export_path():
    # Timestamped so a second export never silently overwrites the first.
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(common.DOWNLOADS, f"plex_rename_list_{stamp}.json")


def parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        prog="plex-rename",
        description="Export a Plex library's rename mapping and apply it to a "
                    "local Jellyfin library (combined tool).")
    p.add_argument("library", nargs="?",
                   help="Local path to the library folder (Phase 2).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show every change without touching any files.")
    p.add_argument("--export-only", action="store_true",
                   help="Phase 1 only: build the mapping, write it, and stop.")
    p.add_argument("--export-file",
                   help="Write the mapping to this path (otherwise you're asked).")
    p.add_argument("--from-mapping",
                   help="Skip Plex; apply a previously exported mapping JSON file.")
    p.add_argument("--log-dir",
                   help="Where to write the undo/skip logs (default: ~/Downloads).")
    p.add_argument("--migrate-watched", action="store_true",
                   help="Step 7 (standalone): migrate Plex watched-state into "
                        "Jellyfin using a post-restructure mapping (needs "
                        "--from-mapping).")
    p.add_argument("--copy-artwork", action="store_true",
                   help="Step 8 (standalone): copy Plex artwork (poster/fanart) "
                        "into the media folders using a post-restructure mapping "
                        "(needs --from-mapping).")
    p.add_argument("--skip-step8", action="store_true",
                   help="Don't offer step 8 (copy Plex artwork) after organizing.")
    p.add_argument("--force", action="store_true",
                   help="With --migrate-watched, re-add play counts to items "
                        "already in the migration log (double-counts).")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the per-step confirmation prompts (the plan is "
                        "still printed). Intended for repeat runs after you've "
                        "previewed with --dry-run; a single 'are you sure?' gate "
                        "still guards a non-dry run.")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    _validate(args, p)
    return args


def _validate(args, parser):
    """Reject contradictory flag combinations up front (#6) so the user finds
    out immediately rather than part-way through a run."""
    if args.from_mapping and args.export_only:
        parser.error("--from-mapping and --export-only can't be combined: one "
                     "applies an existing mapping, the other builds a new one.")
    if args.from_mapping and args.export_file:
        parser.error("--from-mapping and --export-file can't be combined: "
                     "applying an existing mapping doesn't produce a new export.")
    if args.migrate_watched and not args.from_mapping:
        parser.error("--migrate-watched needs --from-mapping (a saved mapping "
                     "JSON, e.g. an export or a plex_rename_applied_*.json).")
    if args.copy_artwork and not args.from_mapping:
        parser.error("--copy-artwork needs --from-mapping (a saved mapping "
                     "JSON, e.g. a plex_rename_applied_*.json).")


def configure_interactively(args):
    """Run only when the script is launched with no flags. Offers to turn the
    optional settings on before the normal run. The user can decline, or pick
    any combination from the list; selecting nothing just continues as normal.
    Mutates `args` in place to reflect the choices."""
    if not common.ask_yes_no("\nView/change script settings before starting?",
                             default="n"):
        return

    chosen = set(common.ask_multichoice(
        "\nAvailable settings — choose any combination:", MENU_OPTIONS))

    if not chosen:
        print("No settings selected; continuing normally.")
        return

    if "dry-run" in chosen:
        args.dry_run = True
    if "export-only" in chosen:
        args.export_only = True
    if "export" in chosen:
        p = common.ask(f"Export file path (blank for {default_export_path()}): ")
        args.export_file = p or default_export_path()
    if "from-mapping" in chosen:
        args.from_mapping = common.ask_path(
            "Path to the saved mapping (.json) file: ", must_be_file=True)
        if "export" in chosen or "export-only" in chosen:
            print("Note: applying from an existing mapping skips Plex, so the "
                  "export settings will be ignored.")
    if "log-dir" in chosen:
        args.log_dir = common.ask_path("Folder to write undo/skip logs into: ",
                                       must_be_dir=True)
    if "skip-step7" in chosen:
        args.skip_step7 = True
    if "skip-step8" in chosen:
        args.skip_step8 = True

    enabled = []
    if args.dry_run:
        enabled.append("dry run")
    if getattr(args, "skip_step7", False):
        enabled.append("step 7 skipped")
    if getattr(args, "skip_step8", False):
        enabled.append("step 8 skipped")
    if args.from_mapping:
        enabled.append(f"apply from {args.from_mapping}")
    else:
        if args.export_file:
            enabled.append(f"export to {args.export_file}")
        if args.export_only:
            enabled.append("export only")
    if getattr(args, "log_dir", None):
        enabled.append(f"logs to {args.log_dir}")
    print("Enabled: " + (", ".join(enabled) if enabled else "none") + "\n")


def reset_for_next_library(args):
    """Clear the per-library state before looping to another Plex library, in
    one place so it can't fall out of sync as options grow (#28). Connection,
    dry-run, and global toggles persist; anything tied to a specific library
    (its folder, its export destination) is cleared so the next library prompts
    fresh."""
    args.library = None
    args.export_file = None
