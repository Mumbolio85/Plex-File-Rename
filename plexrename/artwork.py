#!/usr/bin/env python3
"""
Step 8: copy Plex artwork (poster, fanart) into the Jellyfin media tree.

Each entry in the mapping carries plex_thumb_url / plex_art_url (movies) or
plex_show_thumb_url / plex_show_art_url (TV shows) — full auth-embedded Plex
URLs written during Phase 1.  This step downloads them and places them as
standard folder-level images that Jellyfin's Local Images fetcher picks up
on the next scan, regardless of whether "Save metadata to media folders" is
enabled in Jellyfin:

  Movies:   {MovieName (Year)}-poster.jpg, {MovieName (Year)}-fanart.jpg
            placed alongside the movie file (same folder, same base name)
  TV shows: poster.jpg, fanart.jpg  inside the series folder

With overwrite=False (the default) an image that is already present is left
alone — Jellyfin's own copy wins.  Passing overwrite=True replaces it with
the version Plex had.

No new dependencies: uses urllib from the stdlib.

Standalone use (--copy-artwork --from-mapping) works as long as the Plex
server is still reachable on the same URL and the auth token has not been
revoked; the token is embedded in the URLs captured during Phase 1.
"""

from __future__ import annotations

import os
import ssl
import urllib.request
import urllib.error

from plexrename import common

# Plex servers accessed by local IP use *.plex.direct certificates, which
# Python's default SSL context rejects when the URL is a raw IP address.
# The Plex token embedded in every URL already authenticates the request, so
# skipping certificate verification is safe for these local-network downloads.
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_ssl_ctx))


def _download_to(url: str, dest: str, run_log, dry_run: bool = False,
                 overwrite: bool = False, undo_log=None) -> bool:
    """Download url to dest.

    With overwrite=False, skips silently when dest already exists so a
    re-run never clobbers custom artwork.  With overwrite=True, replaces any
    existing file.  Returns True when the file is present after the call.

    When a NEW file is written (dest did not exist before), a DELETE_SENTINEL
    entry is appended to undo_log so the download can be reversed.  Overwrites
    of pre-existing images are not logged because the prior content cannot be
    restored."""
    existed = os.path.exists(dest)
    if dry_run:
        if existed and not overwrite:
            print(f"    [DRY RUN] would skip (already exists): "
                  f"{os.path.basename(dest)}")
        else:
            action = "overwrite" if existed else "save"
            print(f"    [DRY RUN] would {action}: {os.path.basename(dest)}")
        return True
    if existed and not overwrite:
        return True
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except OSError as e:
        run_log.skip("ARTWORK ERROR", f"creating dir for {dest}: {e}")
        return False
    try:
        with _opener.open(url, timeout=30) as resp:
            data = resp.read()
        with open(dest, "wb") as f:
            f.write(data)
        if undo_log is not None and not existed:
            undo_log.write(f"{dest}{common.SEP}{common.DELETE_SENTINEL}\n")
            undo_log.flush()
        return True
    except urllib.error.URLError as e:
        run_log.skip("ARTWORK ERROR", f"downloading {url}: {e}")
        return False
    except OSError as e:
        run_log.skip("ARTWORK ERROR", f"writing {dest}: {e}")
        return False


def _remove_stale_posters(item_dir: str, keep_path: str, run_log,
                          dry_run: bool = False) -> None:
    """Delete any poster-named files in item_dir except keep_path.

    Matches: poster.*, *-poster.*, folder.*, *-folder.*
    Called before placing a new poster so Jellyfin never sees two competing
    poster files in the same folder."""
    try:
        names = os.listdir(item_dir)
    except OSError:
        return
    keep = os.path.abspath(keep_path)
    for name in names:
        nl = name.lower()
        dot = nl.rfind(".")
        stem = nl[:dot] if dot != -1 else nl
        if stem not in ("poster", "folder") and \
                not stem.endswith("-poster") and \
                not stem.endswith("-folder"):
            continue
        path = os.path.join(item_dir, name)
        if os.path.abspath(path) == keep:
            continue
        if dry_run:
            print(f"    [DRY RUN] would remove stale poster: {name}")
            continue
        try:
            os.remove(path)
        except OSError as e:
            run_log.skip("ARTWORK WARNING",
                         f"couldn't remove stale poster {name}: {e}")


def copy_artwork(entries: list, dry_run: bool, run_log,
                 overwrite: bool = False, undo_log=None) -> int:
    """Download Plex artwork URLs from entries and save as folder-level images.

    Movies: {base}-folder.jpg / {base}-backdrop.jpg placed alongside the movie
            file (where base is the video filename without extension).
    TV:     poster.jpg / fanart.jpg placed in the series folder (parent of the
            Season NN folder, as produced by the Jellyfin restructure).

    Images that already exist are skipped unless overwrite=True.  New files
    are recorded in undo_log (if provided) so plex_undo_rename can delete them.
    Returns the number of folders for which at least one image was saved."""
    progress = common.make_progress("Copying artwork for item", len(entries))
    on_progress_line = False
    done = 0
    movie_dirs_done: set[str] = set()
    series_dirs_done: set[str] = set()

    for n, entry in enumerate(entries, start=1):
        on_progress_line = progress(n)
        result_path = entry.get("result_path")
        if not result_path:
            continue
        if not dry_run and not os.path.exists(result_path):
            continue

        item_dir = os.path.dirname(result_path)
        media_type = entry.get("media_type")
        any_written = False

        if media_type == "movie":
            if item_dir in movie_dirs_done:
                continue
            base = os.path.splitext(os.path.basename(result_path))[0]
            thumb = entry.get("plex_thumb_url")
            art = entry.get("plex_art_url")
            poster_dest = os.path.join(item_dir, f"{base}-folder.jpg")
            if overwrite:
                _remove_stale_posters(item_dir, poster_dest, run_log, dry_run)
            if thumb:
                any_written |= _download_to(
                    thumb, poster_dest,
                    run_log, dry_run, overwrite, undo_log)
            if art:
                any_written |= _download_to(
                    art, os.path.join(item_dir, f"{base}-backdrop.jpg"),
                    run_log, dry_run, overwrite, undo_log)
            movie_dirs_done.add(item_dir)

        elif media_type == "tv":
            # One poster per series folder.  After the Jellyfin restructure the
            # path is .../Series (Year)/Season NN/episode.mkv, so the series
            # folder is the parent of the season folder.
            series_dir = os.path.dirname(item_dir)
            if series_dir in series_dirs_done:
                continue
            thumb = entry.get("plex_show_thumb_url")
            art = entry.get("plex_show_art_url")
            if thumb:
                _download_to(
                    thumb, os.path.join(series_dir, "poster.jpg"),
                    run_log, dry_run, overwrite, undo_log)
            if art:
                _download_to(
                    art, os.path.join(series_dir, "fanart.jpg"),
                    run_log, dry_run, overwrite, undo_log)
            series_dirs_done.add(series_dir)
            any_written = True

        if any_written:
            done += 1

    if on_progress_line:
        print()
    label = "would copy" if dry_run else "copied"
    print(f"  Artwork: {label} for {done} folder(s).")
    return done


def copy_artwork_inline(entries: list, dry_run: bool, run_log,
                        assume_yes: bool = False, undo_log=None) -> None:
    """Step 8, offered at the end of a normal rename / restructure run.

    Asks whether to copy artwork at all, then (if yes) whether to overwrite
    images Jellyfin has already placed.  With --yes the safe defaults are used
    (copy, but do not overwrite).  New files are recorded in undo_log so
    plex_undo_rename can delete them."""
    print("\n--- Optional step 8: copy Plex artwork ---")
    print("Downloads the poster and fanart you have set in Plex and places")
    print("them in the media folders so Jellyfin picks them up on next scan.")
    print("  Movies: {Title (Year)}-folder.jpg alongside the video file.")
    print("  TV:     poster.jpg / fanart.jpg in the series folder.")
    if not any(e.get("plex_thumb_url") or e.get("plex_show_thumb_url")
               for e in entries):
        print("  No artwork URLs found in this mapping.  Run a fresh Plex scan")
        print("  (not --from-mapping with an old export) to capture them.")
        return

    if assume_yes:
        copy_artwork(entries, dry_run, run_log, overwrite=False,
                     undo_log=undo_log)
        return

    choice = common.ask_choice(
        "\nCopy Plex artwork?",
        [("1", "Yes — skip images Jellyfin has already placed (safe default)"),
         ("2", "Yes — overwrite existing images with the Plex versions"),
         ("3", "No — skip this step")])
    if choice == "3":
        return
    overwrite = (choice == "2")
    copy_artwork(entries, dry_run, run_log, overwrite=overwrite,
                 undo_log=undo_log)
