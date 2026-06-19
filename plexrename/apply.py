#!/usr/bin/env python3
"""
Phase 2: remap the mapping onto a LOCAL library folder, detect the existing
folder structure, flag outliers, show a plan, confirm, then rename. Optionally
restructure into Jellyfin's recommended layout, and -- if requested -- migrate
watched-state into Jellyfin afterwards.

Nothing on disk changes until the user confirms. Every move is written to an
undo log so the run can be reversed.
"""

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import posixpath
import datetime
from collections import Counter

from plexrename import common
from plexrename.models import Entry
from plexrename.naming import write_mapping

# Sidecar files that belong to a video and must travel with it on rename.
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".smi", ".sup"}
METADATA_EXTS = {".nfo"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tbn", ".webp"}
# Artwork sidecars follow the Jellyfin/Kodi "<name>-<type>.ext" convention.
# "-folder"/"-backdrop" are the names step 8 (artwork.copy_artwork) writes for
# movie poster/fanart, so they must be here too or a later rename would leave
# them behind.
IMAGE_SUFFIXES = ("-poster", "-fanart", "-folder", "-backdrop", "-thumb",
                  "-banner", "-landscape", "-clearart", "-clearlogo", "-logo",
                  "-disc")


# --------------------------------------------------------------------------- #
# Sidecars
# --------------------------------------------------------------------------- #
def find_sidecar_remainders(video_path: str, dir_cache: dict | None = None) -> list[str]:
    """Find subtitle/metadata/artwork files sitting next to the video at
    `video_path` and return the part of each name that follows the video's
    stem (e.g. '.en.srt', '.nfo', '-poster.jpg'). A sidecar shares the video's
    stem, optionally followed by language/type tokens. The remainders are
    captured once, from the video's ORIGINAL on-disk location, so the same set
    can be projected onto each later move stage without re-scanning -- which is
    what lets the dry-run preview list sidecars for a restructure that follows
    a (simulated, not-yet-applied) rename.

    `dir_cache` (dir -> listing) lets the caller reuse one os.listdir per folder
    across many videos in the same directory -- the slow part on a network
    share -- instead of re-listing the folder for every file in it."""
    src_dir = os.path.dirname(video_path)
    src_stem = os.path.splitext(os.path.basename(video_path))[0]
    remainders = []
    if dir_cache is not None and src_dir in dir_cache:
        names = dir_cache[src_dir]
    else:
        try:
            names = os.listdir(src_dir)
        except OSError:
            names = []
        if dir_cache is not None:
            dir_cache[src_dir] = names
    for name in names:
        full = os.path.join(src_dir, name)
        if os.path.abspath(full) == os.path.abspath(video_path):
            continue  # the video itself
        if not os.path.isfile(full) or not name.startswith(src_stem):
            continue
        remainder = name[len(src_stem):]
        # Require a '.' or '-' boundary so 'Movie 2.mkv' isn't taken as a
        # sidecar of 'Movie'.
        if not remainder or remainder[0] not in ".-":
            continue
        ext = os.path.splitext(name)[1].lower()
        is_sidecar = (
            ext in SUBTITLE_EXTS
            or ext in METADATA_EXTS
            or (ext in IMAGE_EXTS and remainder[0] == "-"
                and any(remainder.lower().startswith(s) for s in IMAGE_SUFFIXES))
        )
        if is_sidecar:
            remainders.append(remainder)
    return remainders


def sidecar_pairs(remainders: list[str], src: str, dst: str) -> list[tuple[str, str]]:
    """Project a video's sidecar `remainders` onto a single move stage: the
    video goes `src` -> `dst`, so each sidecar goes from `<src stem><remainder>`
    (next to the video's current location) to `<dst stem><remainder>` (next to
    its destination). Works identically for the real and dry-run paths because
    it derives both sides from the stems, not from a fresh directory scan."""
    src_dir = os.path.dirname(src)
    src_stem = os.path.splitext(os.path.basename(src))[0]
    dst_dir = os.path.dirname(dst)
    dst_stem = os.path.splitext(os.path.basename(dst))[0]
    return [(os.path.join(src_dir, src_stem + rem),
             os.path.join(dst_dir, dst_stem + rem)) for rem in remainders]


# --------------------------------------------------------------------------- #
# Path remapping
# --------------------------------------------------------------------------- #
def detect_recorded_root(old_paths):
    """The library root as recorded in the mapping (server-side). Recorded paths
    are always normalised to forward slashes (see naming.video_parts /
    read_mapping), so this uses posixpath rather than os.path -- otherwise a
    Windows host would split these '/'-paths on '\\' and mis-detect the root."""
    if len(old_paths) == 1:
        return posixpath.dirname(old_paths[0])
    try:
        return posixpath.commonpath(old_paths)
    except ValueError:
        print("Couldn't determine a common library root from the recorded "
              "paths -- they don't share a common base (e.g. the library is "
              "spread across multiple drives, or mixes absolute and relative "
              "paths). Exiting to avoid mistakes.")
        sys.exit(1)


def relative_components(old_path, recorded_root):
    op = old_path.rstrip("/")
    root = recorded_root.rstrip("/")
    if op.startswith(root + "/"):
        rel = op[len(root) + 1:]
    else:
        rel = posixpath.basename(op)
    return rel.split("/")


def _find_renamed(original_local_path: str, new_name: str,
                  media_type: str | None, library_folder: str) -> str:
    """When a file is not found at its original local path (because a previous
    run already renamed or restructured it), try the locations it would have
    landed in.  Returns the first path that exists on disk, or the original
    path unchanged if nothing is found (so the item shows as missing)."""
    # 1. Same directory, new filename — rename-only, no restructure.
    same_dir = os.path.join(os.path.dirname(original_local_path), new_name)
    if os.path.exists(same_dir):
        return same_dir
    # 2. Jellyfin-recommended layout — rename + restructure.
    jf = jellyfin_target(new_name, media_type, library_folder)
    if jf and os.path.exists(jf):
        return jf
    # 3. Flat in the library root — outlier or single-level layout.
    flat = os.path.join(library_folder, new_name)
    if os.path.exists(flat):
        return flat
    return original_local_path


def _path_levels(path: str, library_folder: str, fallback: int) -> int:
    """Number of directory components between library_folder and path.
    Falls back to `fallback` if path is outside the library root."""
    lib = os.path.abspath(library_folder)
    p = os.path.abspath(path)
    if p.startswith(lib + os.sep):
        return len(p[len(lib) + 1:].split(os.sep)) - 1
    return fallback


def build_items(entries: list[Entry], library_folder: str) -> tuple[list[dict], str]:
    """Map every mapping entry (a rich dict) onto the local filesystem. This is
    the slow phase on a network share: each item gets an existence check plus a
    sidecar scan, so it shows progress as it goes. A per-folder listing cache
    means a directory holding many items is only listed once.

    When the file is not found at the path derived from the Plex server record,
    _find_renamed tries the locations a previous rename/restructure run would
    have placed it, so a second standalone run on an already-renamed library
    still matches correctly."""
    old_paths = [e["old_path"] for e in entries]
    recorded_root = detect_recorded_root(old_paths)

    items = []
    dir_cache: dict[str, list[str]] = {}
    progress = common.make_progress("Scanning local file", len(entries))
    on_progress_line = False
    for n, entry in enumerate(entries, start=1):
        on_progress_line = progress(n)
        old_path = entry["old_path"]
        comps = relative_components(old_path, recorded_root)
        local_path = os.path.join(library_folder, *comps)

        if not os.path.exists(local_path):
            local_path = _find_renamed(
                local_path, entry["new_name"],
                entry.get("media_type"), library_folder)

        items.append({
            "old_recorded": old_path,
            "rel_comps": comps,
            # Use the depth of the *actual* found path so outlier analysis
            # reflects where files really are now, not where Plex recorded them.
            "levels": _path_levels(local_path, library_folder, len(comps) - 1),
            "current_path": local_path,
            "current_dir": os.path.dirname(local_path),
            "new_name": entry["new_name"],
            "media_type": entry.get("media_type"),  # "movie"/"tv" or None
            "leave_alone": False,
            "result_path": local_path,         # updated as we apply changes
            # Existence recorded here so the caller doesn't need a second slow
            # pass of stat() calls over the network just to count matches.
            "exists": os.path.exists(local_path),
            # Sidecars captured once, here, while the files are still at their
            # original location; projected onto each move stage by sidecar_pairs.
            "sidecar_remainders": find_sidecar_remainders(local_path, dir_cache),
        })
    if on_progress_line:
        print()
    return items, recorded_root


# --------------------------------------------------------------------------- #
# Structure analysis + outlier handling
# --------------------------------------------------------------------------- #
LEVEL_DESC = {
    0: "loose in the library root (no per-item folder)",
    1: "each item in its own folder",
    2: "nested two folders deep (e.g. Show / Season)",
}


def describe_levels(n):
    return LEVEL_DESC.get(n, f"nested {n} folders deep")


def analyze_and_handle_outliers(items):
    counts = Counter(it["levels"] for it in items)
    majority_levels = counts.most_common(1)[0][0]

    print("\nDetected folder structure:")
    for lvl, c in sorted(counts.items()):
        flag = "  <-- majority" if lvl == majority_levels else ""
        print(f"  {c:>4} item(s): {describe_levels(lvl)}{flag}")

    outliers = [it for it in items if it["levels"] != majority_levels]
    if not outliers:
        print("All items follow the same pattern.")
        return majority_levels

    print(f"\n{len(outliers)} item(s) do NOT match the majority pattern "
          f"({describe_levels(majority_levels)}):")

    # With several outliers, offer to handle them all at once before falling
    # back to the per-item prompt for the mixed case.
    if len(outliers) > 1:
        if common.ask_yes_no(f"\nBring ALL {len(outliers)} outlier(s) into line with "
                             "the majority?", default="n"):
            for it in outliers:
                it["leave_alone"] = False
            return majority_levels
        if common.ask_yes_no(f"Leave ALL {len(outliers)} outlier(s) completely alone "
                             "(not renamed)?", default="y"):
            for it in outliers:
                it["leave_alone"] = True
            print("  -> All outliers will be left alone.")
            return majority_levels
        print("Deciding each outlier individually:")

    for it in outliers:
        print(f"\n  File : {it['current_path']}")
        print(f"  This : {describe_levels(it['levels'])}")
        if common.ask_yes_no("  Change it to match the rest?", default="n"):
            it["leave_alone"] = False
        else:
            it["leave_alone"] = True
            print("  -> Will be left completely alone (not renamed).")

    return majority_levels


def normalized_dir(it, majority_levels, library_folder):
    """Where an outlier should live to match the majority pattern."""
    base = os.path.splitext(it["new_name"])[0]
    if majority_levels == 0:
        return library_folder
    if majority_levels == 1:
        return os.path.join(library_folder, base)
    # Deeper nesting can't be reconstructed reliably; keep where it is.
    return it["current_dir"]


# --------------------------------------------------------------------------- #
# Plan building + execution
# --------------------------------------------------------------------------- #
def sanitize_under_root(path, root):
    """Strip filesystem-invalid characters ('<>:"/\\|?*') from every path
    component the script adds *below* `root`, leaving the library root prefix
    (which the user supplied and must stay intact) untouched. Applied to a
    destination right before it's used so neither the new filename nor any new
    folder it lands in can carry an illegal character. The '/' is handled by
    splitting on the path separator first, so it's only ever stripped from
    within a component, never as a separator."""
    root = os.path.abspath(root)
    full = os.path.abspath(path)
    if full == root or not full.startswith(root + os.sep):
        return path  # outside the library root; leave it alone
    rel = full[len(root) + 1:]
    parts = [common.sanitize(p) for p in rel.split(os.sep)]
    return os.path.join(root, *parts)


def build_rename_plan(items, majority_levels, library_folder):
    """Return list of (item, src, dst, sidecars) for everything that needs to
    move. `sidecars` is the list of (src, dst) pairs for the subtitle/.nfo/
    artwork files that travel with the video; it's computed here so the preview
    reflects every file that will actually move, not just the videos."""
    plan = []
    for it in items:
        if it["leave_alone"]:
            continue
        src = it["current_path"]
        if it["levels"] != majority_levels:
            target_dir = normalized_dir(it, majority_levels, library_folder)
        else:
            target_dir = it["current_dir"]
        dst = sanitize_under_root(os.path.join(target_dir, it["new_name"]),
                                  library_folder)
        it["planned_dst"] = dst
        if os.path.abspath(src) != os.path.abspath(dst):
            plan.append((it, src, dst,
                         sidecar_pairs(it["sidecar_remainders"], src, dst)))
        else:
            it["result_path"] = dst
    return plan


def build_jellyfin_plan(items, library_folder):
    """Return list of (item, src, dst, sidecars), like build_rename_plan, for
    moving each video into Jellyfin's recommended folder layout. Items whose
    name can't be parsed into a target are reported and skipped."""
    plan = []
    for it in items:
        if it["leave_alone"]:
            continue
        src = it["result_path"]
        name = os.path.basename(src)
        dst = jellyfin_target(name, it["media_type"], library_folder)
        if dst is None:
            print(f"  Could not parse for restructure, skipping: {name}")
            continue
        dst = sanitize_under_root(dst, library_folder)
        if os.path.abspath(src) != os.path.abspath(dst):
            plan.append((it, src, dst,
                         sidecar_pairs(it["sidecar_remainders"], src, dst)))
    return plan


def jellyfin_target(filename: str, media_type, library_folder: str) -> str | None:
    base, ext = os.path.splitext(filename)
    if media_type == "movie":
        # Library/Title (Year)/Title (Year).ext
        # Multiple editions ("- [Director's Cut]") and stacked parts ("- part2")
        # share one movie folder, so strip those tags from the folder name only.
        folder = re.sub(r"\s*-\s*part\d+\s*$", "", base)
        folder = re.sub(r"\s*-\s*\[[^\]]*\]\s*$", "", folder).strip()
        return os.path.join(library_folder, folder, filename)
    if media_type == "tv":
        # Library/Series (Year)/Season NN/<filename>
        m = re.search(r"^(.*?)\s*-\s*S(\d{1,4})E\d{1,4}", base)
        if not m:
            return None
        series = m.group(1).strip()
        season = int(m.group(2))
        return os.path.join(library_folder, series,
                            f"Season {season:02d}", filename)
    return None


def preview_and_confirm(plan, title, dry_run=False, assume_yes=False):
    if not plan:
        print(f"\n{title}: nothing to do.")
        return False
    sidecar_total = sum(len(sidecars) for _, _, _, sidecars in plan)
    print(f"\n{title} ({len(plan)} video(s)"
          f"{f' + {sidecar_total} sidecar(s)' if sidecar_total else ''}):")
    for _, src, dst, sidecars in plan:
        print(f"  {src}")
        print(f"    -> {dst}")
        for s, d in sidecars:
            print(f"    + sidecar: {s}")
            print(f"        -> {d}")
    if dry_run:
        print("\n[DRY RUN] no changes will be made.")
        return True
    if assume_yes:
        # --yes: the plan was still printed above; just don't pause for input.
        print(f"\n[--yes] applying these {len(plan)} video(s) without prompting.")
        return True
    return common.ask_yes_no(f"\nApply these {len(plan)} video(s)"
                             f"{f' (plus {sidecar_total} sidecar file(s))' if sidecar_total else ''}?",
                             default="n")


def safe_move(src, dst):
    """Create the destination folder and move src -> dst, retrying once on a
    transient OSError (common on network shares/NAS). Returns None on success,
    or an error-detail string on failure (the caller logs it). Both the makedirs
    and the move are guarded so a permission/IO error skips just this file
    instead of aborting the whole run."""
    for attempt in range(2):
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            return None
        except OSError as e:
            if attempt == 0:
                time.sleep(0.5)
                continue
            return f"moving {src}: {e}"


def execute_plan(plan, undo_log, run_log, dry_run=False, label="Processing"):
    done = 0
    total = len(plan)

    # Live progress (updates in place on a tty, throttled newline lines off it).
    # Any detail message (a skip or a sidecar move) first breaks off the progress
    # line via detail() so the two never clobber each other; the next iteration
    # redraws the progress line fresh.
    progress = common.make_progress(label, total)
    on_progress_line = False

    def detail(text):
        nonlocal on_progress_line
        if on_progress_line:
            print()
            on_progress_line = False
        print(text)

    def log_skip(category, target):
        # run_log.skip prints to stdout too, so close the progress line first.
        nonlocal on_progress_line
        if on_progress_line:
            print()
            on_progress_line = False
        run_log.skip(category, target)

    for n, (it, src, dst, sidecars) in enumerate(plan, start=1):
        # The video plus any subtitle/.nfo/artwork files that travel with it.
        moves = [(src, dst)] + sidecars

        on_progress_line = progress(n)

        if dry_run:
            # Pure simulation: don't probe the disk -- the full list of moves was
            # already shown in the preview above, so here we just tick the
            # progress counter. A later stage (restructure) moves files this
            # stage would have created, so on-disk checks would give false
            # MISSING/TARGET-EXISTS results in a dry run; advancing result_path
            # lets that stage build on the simulated rename. The pre-flight
            # "N of M files found" check already flags genuinely absent files.
            it["result_path"] = dst
            done += 1
            continue

        if not os.path.exists(src):
            log_skip("MISSING", src)
            continue
        if os.path.exists(dst):
            log_skip("TARGET EXISTS", dst)
            continue

        video_moved = False
        for s, d in moves:
            if not os.path.exists(s):
                continue
            if os.path.exists(d):
                log_skip("SIDECAR TARGET EXISTS" if s != src
                         else "TARGET EXISTS", d)
                continue
            err = safe_move(s, d)
            if err is not None:
                log_skip("ERROR", err)
                continue
            undo_log.write(f"{d}{common.SEP}{s}\n")
            undo_log.flush()
            if s == src:
                video_moved = True
            else:
                detail(f"  + sidecar: {os.path.basename(s)} "
                       f"-> {os.path.basename(d)}")

        # Only advance result_path (which the restructure step builds on) when
        # the video itself actually moved -- not when a sidecar moved but the
        # video was skipped (e.g. its target already existed).
        if video_moved:
            it["result_path"] = dst
            done += 1
        # Empty source folders are removed once at the end by cleanup_empty_dirs,
        # which records each removal in the undo log so it can be recreated.

    if on_progress_line:
        print()  # finish the last progress line
    if dry_run:
        print(f"  [DRY RUN] would apply {done} change(s).")
    else:
        print(f"  Applied {done} change(s).")
    return done


def attach_result_paths(items, entries):
    """Copy each item's final on-disk result_path back onto its source entry
    (joined by old_path), so a saved mapping can later drive standalone step 7
    by path. Entries with no matching item keep whatever they had."""
    by_old = {it["old_recorded"]: it["result_path"] for it in items}
    for e in entries:
        rp = by_old.get(e.get("old_path"))
        if rp:
            e["result_path"] = rp


def migrate_watched_inline(entries, undo_log, run_log, log_dir, force,
                           assume_yes=False):
    """Inline step 7 right after a restructure: explain the scan-first rule,
    connect to Jellyfin, wait for the user's scan to finish, then migrate. The
    Jellyfin module is imported lazily so steps 1-6 never depend on it."""
    from plexrename.jellyfin import (
        connect_jellyfin, choose_jellyfin_user, migrate_watched, MigratedLog,
    )
    print("\n--- Optional step 7: migrate watched-state into Jellyfin ---")
    print("Jellyfin can only carry watched-state for files it has already")
    print("SCANNED. The files were just moved, so trigger a library scan in")
    print("Jellyfin (Dashboard -> Scan All Libraries) and let it finish.")
    if not assume_yes and not common.ask_yes_no(
            "Migrate watched-state into Jellyfin now?", default="n"):
        return
    client = connect_jellyfin()
    user_id = choose_jellyfin_user(client)
    if not user_id:
        print("  No Jellyfin user available; skipping watched-state migration.")
        return
    # Scan-wait: now that we're connected, let the user run + finish the scan so
    # the library index we build next actually sees the just-moved items.
    common.ask("  Start a Jellyfin library scan, let it FINISH, then press Enter... ")
    migrated_log = MigratedLog(os.path.join(log_dir, "plex_jf_migrated.json"))
    migrate_watched(entries, client, user_id, dry_run=False,
                    undo_log=undo_log, run_log=run_log,
                    migrated_log=migrated_log, force=force)


def apply_mapping(entries: list[Entry], library_folder: str, dry_run: bool,
                  log_dir: str | None = None, force: bool = False,
                  assume_yes: bool = False, skip_step7: bool = False,
                  skip_step8: bool = False) -> None:
    """Phase 2: remap onto the local folder, plan, confirm, apply, restructure,
    and -- if step 6 ran and skip_step7 is False -- offer step 7 (migrate
    watched-state into Jellyfin). Undo/skip logs are written to log_dir
    (defaults to ~/Downloads)."""
    log_dir = common.ensure_writable_dir(log_dir or common.DOWNLOADS)
    items, recorded_root = build_items(entries, library_folder)
    print(f"Plex stores these files under:   {recorded_root}")
    print(f"Looking for them on this PC in:  {library_folder}")

    # Sanity check: how many of the source files actually exist locally?
    found = sum(1 for it in items if it["exists"])
    print(f"\nMatched {found} of {len(items)} files in that folder.")
    if found == 0:
        print("Couldn't find ANY of the files there. The folder you entered is "
              "probably wrong, or the drive/network share isn't connected. "
              "Stopping so nothing gets changed by mistake.")
        return
    if found < len(items):
        print("Some files weren't found and will be skipped (left untouched).")
        if not assume_yes and not common.ask_yes_no(
                "Keep going with the ones that were found?", default="n"):
            return

    majority_levels = analyze_and_handle_outliers(items)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log = common.RunLog(os.path.join(log_dir, f"plex_rename_skipped_{stamp}.txt"),
                            header="Items skipped or failed during apply")
    if dry_run:
        undo_log = None
        undo_path = None
    else:
        undo_path = os.path.join(log_dir, f"plex_rename_undo_{stamp}.txt")
        undo_log = common.UndoLog(undo_path)

    jellyfin_ran = False  # set True only if the step-6 restructure executes
    try:
        plan = build_rename_plan(items, majority_levels, library_folder)
        if preview_and_confirm(plan, "RENAME PLAN", dry_run, assume_yes):
            execute_plan(plan, undo_log, run_log, dry_run, label="Renaming")
        else:
            print("Rename step skipped.")

        # Build the Jellyfin plan before asking so we can skip the prompt
        # entirely when everything is already in the recommended layout.
        active = [it for it in items if not it["leave_alone"]]
        needs_type_prompt = active and not all(it["media_type"] for it in active)
        # If media type is unknown we can't evaluate the plan yet; treat it as
        # non-empty so the prompt is shown and the user can supply the type.
        jplan = [] if needs_type_prompt else build_jellyfin_plan(items, library_folder)

        if not jplan and not needs_type_prompt:
            print("\nFiles are already in Jellyfin's recommended layout; "
                  "skipping restructure.")
            jellyfin_ran = True  # layout is ready; still offer step 7
        else:
            print("\n--- Optional: organize into Jellyfin's recommended folders ---")
            print("This puts each movie/show in its own folder the way Jellyfin")
            print("likes best, e.g. 'Heat (1995)/Heat (1995).mkv'.")
            if assume_yes or common.ask_yes_no("Organize the files into these folders too?",
                                               default="n"):
                # Older mapping files don't carry media_type; ask once and fill in.
                if needs_type_prompt:
                    mt = common.ask_choice("Media type wasn't recorded in the mapping. "
                                           "What type of library is this?",
                                           [("movie", "Movies  -> Title (Year)/Title (Year).ext"),
                                            ("tv", "TV Shows -> Series (Year)/Season NN/episode.ext")])
                    media_type = "movie" if mt == "movie" else "tv"
                    for it in active:
                        if not it["media_type"]:
                            it["media_type"] = media_type
                    jplan = build_jellyfin_plan(items, library_folder)

                if preview_and_confirm(jplan, "JELLYFIN RESTRUCTURE PLAN", dry_run, assume_yes):
                    execute_plan(jplan, undo_log, run_log, dry_run, label="Organizing")
                    jellyfin_ran = True
                elif jplan:
                    print("Restructure skipped.")

        # Clean up any folders left empty by the moves above (logged to undo).
        removed = common.cleanup_empty_dirs(library_folder, undo_log=undo_log,
                                            dry_run=dry_run)
        if removed:
            label = "Would remove" if dry_run else "Removed"
            print(f"\n{label} {len(removed)} empty folder(s):")
            for d in removed:
                print(f"  {d}")

        # Step 7: offered whenever step 6 ran and watched-state was captured.
        # Gated on step 6 so result_path reflects the post-restructure location.
        # result_path is also persisted back into a saved mapping so the
        # standalone --migrate-watched / --copy-artwork modes can reuse it later.
        if jellyfin_ran and not dry_run \
                and any(e.get("watched_state") for e in entries):
            applied_path = os.path.join(log_dir, f"plex_rename_applied_{stamp}.json")
            attach_result_paths(items, entries)
            write_mapping(entries, applied_path)
            print(f"Saved a post-restructure mapping (for later --migrate-watched"
                  f" / --copy-artwork):\n  {applied_path}")
            if not skip_step7:
                migrate_watched_inline(entries, undo_log, run_log, log_dir, force,
                                       assume_yes)

        # Step 8: copy Plex artwork.  Only offered when step 7 has been run --
        # either this session or in a previous run (detected by the presence of
        # plex_jf_migrated.json).  This covers both --skip-step7 re-runs and
        # the "already in Jellyfin layout" path where step 7 ran previously.
        migrated_log_path = os.path.join(log_dir, "plex_jf_migrated.json")
        if not skip_step8 and os.path.isfile(migrated_log_path):
            from plexrename.artwork import copy_artwork_inline
            copy_artwork_inline(entries, dry_run, run_log, assume_yes,
                                undo_log=undo_log)
    finally:
        if undo_log is not None:
            undo_log.close()
        run_log.close()

    print("\nDone." if not dry_run else "\nDone (dry run — nothing changed).")
    if run_log.created:
        print(f"Some items were skipped/failed. See:\n  {run_log.path}")
    if undo_log is not None and undo_log.created:
        print(f"To reverse changes, the undo log maps new -> original at:\n  {undo_path}")
