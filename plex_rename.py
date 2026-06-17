#!/usr/bin/env python3
"""
Plex -> Jellyfin rename tool (export + apply combined).

What it does
------------
Phase 1 (export):  Connect to a Plex server, pick a library, and build a mapping
                   of each item's current file path -> a clean "Title (Year)"
                   filename. The mapping is kept in memory; writing it to a JSON
                   file is optional (handy as a reviewable/resumable artifact,
                   and it captures all the metadata plexapi reported).

Phase 2 (apply):   Remap that mapping onto a LOCAL library folder, detect the
                   existing folder structure, flag outliers, show a plan,
                   confirm, then rename. Optionally restructure into Jellyfin's
                   recommended layout. Sidecar files (subtitles, .nfo, artwork)
                   travel with each video.

Both phases run back-to-back by default, so the mapping never has to hit disk.

Connecting to Plex
------------------
You'll be guided through it. The easiest route: in Plex web, click the (...) on
any item -> Get Info -> View XML, then paste that browser URL when asked -- the
server address and token are pulled out of it automatically. You can also enter
them separately, or log in with your Plex account to auto-discover servers.

Safety
------
Nothing on disk changes until you type "yes" at a confirmation prompt. Every
move is written to an undo log in ~/Downloads (reverse it with
plex_undo_rename.py). Run with --dry-run to preview every change without
touching any files.

Requires: pip install plexapi

Usage
-----
    plex_rename.py [library_folder] [--dry-run]
                   [--export-only] [--export-file PATH]
                   [--from-mapping PATH]

    library_folder     Local path to the library folder (Phase 2). Prompted for
                       if omitted.
    --dry-run          Show every change without touching any files.
    --export-only      Phase 1 only: build the mapping, write it to a file, stop.
    --export-file PATH Where to write the mapping JSON (implies it gets written).
    --from-mapping PATH  Skip Plex entirely and apply a previously exported
                         mapping JSON file (the old "apply a pre-made list" path).
"""

from __future__ import annotations

import os
import re
import sys
import json
import shutil
import getpass
import argparse
import datetime
from collections import Counter, defaultdict

from plex_rename_common import (
    sanitize, SEP, DOWNLOADS,
    RunLog, ask, ask_path, ask_yes_no, ask_choice, ask_multichoice,
    cleanup_empty_dirs, clean_path_input, make_progress,
)

__version__ = "1.0.0"

# Sidecar files that belong to a video and must travel with it on rename.
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".smi", ".sup"}
METADATA_EXTS = {".nfo"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tbn", ".webp"}
# Artwork sidecars follow the Jellyfin/Kodi "<name>-<type>.ext" convention.
IMAGE_SUFFIXES = ("-poster", "-fanart", "-thumb", "-banner", "-landscape",
                  "-clearart", "-clearlogo", "-logo", "-disc", "-backdrop")


# =========================================================================== #
# PHASE 1: connect to Plex + build the mapping
# =========================================================================== #

# --------------------------------------------------------------------------- #
# Connection / onboarding
# --------------------------------------------------------------------------- #
def extract_from_xml_url(url):
    """Pull (baseurl, token) out of a Plex 'View XML' browser URL, e.g.
    http://127.0.0.1:32400/library/metadata/123?...&X-Plex-Token=abc123
    -> ("http://127.0.0.1:32400", "abc123")."""
    url = url.strip()
    token_m = re.search(r"X-Plex-Token=([^&\s]+)", url)
    server_m = re.match(r"(https?://[^/?\s]+)", url)
    baseurl = server_m.group(1) if server_m else None
    token = token_m.group(1) if token_m else None
    return baseurl, token


def try_connect(baseurl, token):
    """Connect and force a request so bad input fails here, not later."""
    from plexapi.server import PlexServer
    plex = PlexServer(baseurl, token)
    _ = plex.friendlyName
    return plex


def connect_with_feedback(baseurl, token):
    """Try to connect with the given credentials, printing a helpful message on
    failure. Returns a connected PlexServer, or None if the connection failed."""
    try:
        return try_connect(baseurl, token)
    except Exception as e:
        print(f"\nCouldn't connect to Plex: {e}")
        print("Check that the address is reachable and the token is current, "
              "then try again.")
        return None


def connect_via_xml_url():
    """Returns a connected PlexServer, or None to go back."""
    print("\nIn Plex web: click the (...) on any item -> Get Info -> View XML.")
    print("Copy the URL from the browser tab that opens and paste it here.")
    while True:
        url = ask("\nPaste the View XML URL (blank to go back): ")
        if not url:
            return None
        baseurl, token = extract_from_xml_url(url)
        if not baseurl or not token:
            print("  Couldn't find a server address and X-Plex-Token in that URL.")
            print("  Make sure you copied the whole URL. Try again, or leave blank.")
            continue
        print(f"  Server: {baseurl}")
        print(f"  Token:  {token[:4]}...{token[-4:]}")
        plex = connect_with_feedback(baseurl, token)
        if plex is None:
            continue  # let them paste a different URL
        return plex


def connect_via_separate():
    """Returns a connected PlexServer, or None to go back."""
    baseurl = ask("Plex server URL (e.g. http://127.0.0.1:32400): ")
    token = ask("Plex token: ")
    if not baseurl or not token:
        print("  Both a server URL and a token are required.")
        return None
    return connect_with_feedback(baseurl, token)


def connect_via_account():
    """Log in with a plex.tv account and pick a discovered server. Returns a
    connected PlexServer (account login yields the connection directly), or
    None to go back."""
    try:
        from plexapi.myplex import MyPlexAccount
    except ImportError:
        print("  plexapi is required for account login (pip install plexapi).")
        return None

    username = ask("Plex.tv username or email (blank to go back): ")
    if not username:
        return None
    password = getpass.getpass("Plex.tv password: ")
    code = ask("Two-factor code (blank if none): ")
    try:
        account = MyPlexAccount(username, password, code=code or None)
    except Exception as e:
        print(f"  Login failed: {e}")
        return None

    servers = [r for r in account.resources() if "server" in (r.provides or "")]
    if not servers:
        print("  No servers found on this account.")
        return None

    print("\nServers on your account:")
    for i, r in enumerate(servers):
        print(f"  [{i}] {r.name}")
    while True:
        choice = ask("Choose a server by number (blank to go back): ")
        if not choice:
            return None
        if choice.isdigit() and 0 <= int(choice) < len(servers):
            print("  Connecting (this can take a moment)...")
            try:
                return servers[int(choice)].connect()
            except Exception as e:
                print(f"  Couldn't connect to that server: {e}")
                return None
        print("  Invalid choice, try again.")


def connect():
    """Guided connection flow with retries and three entry points (paste XML
    URL / separate fields / account login). Each entry point returns a connected
    PlexServer (or None to go back), so the dispatch here is uniform."""
    entry_points = {
        "1": connect_via_xml_url,
        "2": connect_via_separate,
        "3": connect_via_account,
    }
    while True:
        method = ask_choice(
            "\nHow would you like to connect to Plex?",
            [("1", "Paste a 'View XML' URL (easiest)"),
             ("2", "Enter server address and token separately"),
             ("3", "Log in with your Plex account (auto-discovers servers)")])
        plex = entry_points[method]()
        if plex is None:
            continue
        return plex


def choose_library(plex):
    sections = plex.library.sections()
    print("\nWhich library do you want to rename? (your Plex libraries:)")
    for i, section in enumerate(sections):
        print(f"  [{i}] {section.title} ({section.type})")

    while True:
        choice = ask("\nType the number of the library: ")
        if choice.isdigit() and 0 <= int(choice) < len(sections):
            return sections[int(choice)]
        print("That isn't one of the numbers above. Try again.")


# --------------------------------------------------------------------------- #
# Building names
# --------------------------------------------------------------------------- #
def plex_attrs(obj):
    """Best-effort snapshot of a plexapi object's metadata: every public
    attribute whose value is a JSON-serialisable scalar (or list of scalars).
    Private attributes (the server handle, caches) and nested live objects are
    skipped; datetimes are stored as ISO strings. This lets the JSON export hold
    all the information plexapi gave us for each item without dragging in
    anything that can't be serialised."""
    out = {}
    try:
        attrs = vars(obj).items()
    except TypeError:
        return out
    for key, val in attrs:
        if key.startswith("_"):
            continue
        if val is None or isinstance(val, (str, int, float, bool)):
            out[key] = val
        elif isinstance(val, datetime.datetime):
            out[key] = val.isoformat()
        elif isinstance(val, (list, tuple)):
            scalars = [v for v in val if isinstance(v, (str, int, float, bool))]
            if scalars:
                out[key] = scalars
    return out


def build_new_name(title, year, ext, part_index=None, total_parts=1, edition=None):
    base = f"{title} ({year})" if year else title
    if edition:
        base = f"{base} - [{edition}]"
    if total_parts > 1:
        base = f"{base} - part{part_index}"
    return sanitize(base) + ext


def media_edition_label(media, index):
    """Best-effort label to tell two versions of the same movie apart. Prefers
    Plex's edition/version title, then a known marker found in the file path
    (e.g. 'Angle 1', '3D', 'IMAX') so a version identified only by its filename
    keeps that name, then the resolution, then a generic 'version N'."""
    title = (getattr(media, "title", None) or "").strip()
    if title:
        return title
    for part in getattr(media, "parts", None) or []:
        hint = path_edition_hint(getattr(part, "file", "") or "")
        if hint:
            return hint
    res = (getattr(media, "videoResolution", None) or "").strip().lower()
    if res:
        return f"{res}p" if res.isdigit() else res  # 1080 -> 1080p, 4k stays
    return f"version {index}"


# Tags commonly found in folder/file names that distinguish separate editions.
# Order matters: path_edition_hint() returns the FIRST match, so longer/more
# specific markers must come before any shorter marker they contain (e.g.
# "Full-SBS" before "SBS", "HDR10" before "HDR", "Extended Cut" before
# "Extended").
EDITION_MARKERS = [
    # 3D / stereoscopic layouts
    "3D", "MVC", "Anaglyph",
    "Half-OU", "Half-SBS", "Full-OU", "Full-SBS",
    "HSBS", "FSBS", "HOU", "FOU", "SBS", "OU",
    # Picture format / dynamic range / resolution
    "IMAX", "Open Matte", "Dolby Vision", "HDR10", "HDR", "SDR",
    "4K", "2160p", "1080p", "720p",
    # Cuts / editions (multi-word variants before the single words they contain)
    "Director's Cut", "Directors Cut", "Final Cut", "International Cut",
    "Theatrical Cut", "Extended Cut", "Unrated Cut", "TV Cut",
    "Special Edition", "Ultimate Edition", "Collector's Edition",
    "Collectors Edition", "Limited Edition", "Anniversary Edition",
    "Deluxe Edition", "Criterion",
    "Extended", "Theatrical", "Unrated", "Uncut", "Uncensored",
    "Remastered", "Restored", "Remux", "Redux", "Recut", "Workprint",
    # NOTE: multi-angle discs are handled by ANGLE_RE below, not this list, so
    # any separator/casing is matched ("angle1", "Angle 2", "angle-3").
]

# Matches a multi-angle tag regardless of separator/casing. The leading
# boundary keeps it from firing inside words like "Triangle" or "Angles".
ANGLE_RE = re.compile(r"\bangle[\s_\-]*(\d+)\b", re.IGNORECASE)


def path_edition_hint(path):
    """Look for a well-known edition marker (e.g. '3D', 'Angle 2') in the file
    path. Multi-angle tags are matched flexibly and normalised to 'Angle N';
    the rest are matched as listed in EDITION_MARKERS."""
    m = ANGLE_RE.search(path)
    if m:
        return f"Angle {int(m.group(1))}"
    upper = path.upper()
    for marker in EDITION_MARKERS:
        if marker.upper() in upper:
            return marker
    return None


def video_parts(media):
    """Yield (part_index, total_parts, old_path, ext, part) for each file of a
    media version. Plex server paths are normalised to forward slashes here --
    the single place both collectors capture a file path -- so a Windows Plex
    server's backslash paths remap correctly in the apply phase (which splits
    on '/'), matching what read_mapping() does for exported files."""
    parts = media.parts
    total = len(parts)
    for i, part in enumerate(parts, start=1):
        old_path = (part.file or "").replace("\\", "/")
        ext = os.path.splitext(old_path)[1]
        yield i, total, old_path, ext, part


def collect_movie_entries(item):
    """Returns one rich dict per video file. The disambiguation pass may later
    adjust 'new_name'. Each dict carries the operational fields the apply phase
    needs (old_path/new_name/media_type) plus all the plexapi metadata for the
    movie, its media version, and the file part, under 'plex'."""
    entries = []
    medias = item.media
    multi = len(medias) > 1
    used_labels = set()
    item_edition = (getattr(item, "editionTitle", None) or "").strip()
    item_meta = plex_attrs(item)
    for m_index, media in enumerate(medias, start=1):
        edition = None
        if multi:
            edition = sanitize(media_edition_label(media, m_index))
            # Guarantee the editions are distinct even if two share a label.
            if edition in used_labels:
                edition = f"{edition} ({m_index})"
            used_labels.add(edition)
        res = (getattr(media, "videoResolution", None) or "").strip().lower()
        res = f"{res}p" if res.isdigit() else res
        media_meta = plex_attrs(media)
        for i, total_parts, old_path, ext, part in video_parts(media):
            new_name = build_new_name(item.title, item.year, ext,
                                      part_index=i, total_parts=total_parts,
                                      edition=edition)
            entries.append({
                "old_path": old_path,
                "new_name": new_name,
                "media_type": "movie",
                "title": item.title,
                "year": item.year,
                "edition": edition,
                "edition_title": item_edition,
                "video_resolution": res,
                "part_index": i,
                "total_parts": total_parts,
                "plex": {"item": item_meta,
                         "media": media_meta,
                         "part": plex_attrs(part)},
            })
    return entries


def disambiguate_movies(movie_entries):
    """When separate Plex items map to the same filename, append an edition tag
    so they no longer collide. Tries the item's edition title, then a marker
    found in the path (e.g. 3D/IMAX), then resolution, then 'version N'."""
    groups = defaultdict(list)
    for d in movie_entries:
        groups[d["new_name"].lower()].append(d)

    for group in groups.values():
        if len({d["old_path"] for d in group}) <= 1:
            continue  # not a real collision
        used = set()
        for idx, d in enumerate(group, start=1):
            label = (d["edition_title"]
                     or path_edition_hint(d["old_path"])
                     or d["video_resolution"]
                     or f"version {idx}")
            label = sanitize(label)
            if label.lower() in used:
                label = f"{label} ({idx})"
            used.add(label.lower())
            base, ext = os.path.splitext(d["new_name"])
            d["new_name"] = f"{base} - [{label}]{ext}"


def collect_episode_entries(show, episode):
    """Like collect_movie_entries, but for TV: one rich dict per episode file
    carrying the show/episode/media/part plexapi metadata under 'plex'."""
    entries = []
    season = episode.parentIndex
    ep_num = episode.index
    show_meta = plex_attrs(show)
    episode_meta = plex_attrs(episode)
    for media in episode.media:
        media_meta = plex_attrs(media)
        for i, total_parts, old_path, ext, part in video_parts(media):
            show_part = f"{show.title} ({show.year})" if show.year else show.title
            base = f"{show_part} - S{season:02d}E{ep_num:02d} - {episode.title}"
            if total_parts > 1:
                base = f"{base} - part{i}"
            new_name = sanitize(base) + ext
            entries.append({
                "old_path": old_path,
                "new_name": new_name,
                "media_type": "tv",
                "show_title": show.title,
                "show_year": show.year,
                "season": season,
                "episode": ep_num,
                "episode_title": episode.title,
                "part_index": i,
                "total_parts": total_parts,
                "plex": {"show": show_meta,
                         "episode": episode_meta,
                         "media": media_meta,
                         "part": plex_attrs(part)},
            })
    return entries


def collect_entries(section):
    """Build the in-memory mapping as a list of rich per-file dicts. Every dict
    carries 'old_path', 'new_name', and 'media_type' (all the apply phase needs)
    plus the full plexapi metadata, so a mixed (movies + TV) library restructures
    correctly without asking and the optional JSON export is complete."""
    movie_entries = []
    other_entries = []
    skipped_types = Counter()
    all_items = section.all()
    total = len(all_items)
    progress = make_progress("Scanning item", total)
    on_progress_line = False
    for n, item in enumerate(all_items, start=1):
        # Live progress so a large library doesn't look frozen mid-scan.
        on_progress_line = progress(n)
        if item.type == "movie":
            movie_entries.extend(collect_movie_entries(item))
        elif item.type == "show":
            for episode in item.episodes():
                other_entries.extend(collect_episode_entries(item, episode))
        else:
            skipped_types[item.type] += 1
    if on_progress_line:
        print()  # finish the progress line

    if skipped_types:
        summary = ", ".join(f"{n} {t}" for t, n in skipped_types.items())
        print(f"  Skipped unsupported item type(s): {summary}.")

    disambiguate_movies(movie_entries)
    return movie_entries + other_entries


def write_mapping(entries, out_path):
    """Write the full mapping as JSON: a list of per-file objects holding the
    proposed name plus all the metadata captured from plexapi. JSON keeps every
    field explicitly labelled and survives any character in a path or name,
    unlike the old separator-delimited text format."""
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(entries)} entries to {out_path}")


# =========================================================================== #
# PHASE 2: apply the mapping to a local library folder
# =========================================================================== #

# --------------------------------------------------------------------------- #
# Sidecars
# --------------------------------------------------------------------------- #
def find_sidecar_remainders(video_path: str) -> list[str]:
    """Find subtitle/metadata/artwork files sitting next to the video at
    `video_path` and return the part of each name that follows the video's
    stem (e.g. '.en.srt', '.nfo', '-poster.jpg'). A sidecar shares the video's
    stem, optionally followed by language/type tokens. The remainders are
    captured once, from the video's ORIGINAL on-disk location, so the same set
    can be projected onto each later move stage without re-scanning -- which is
    what lets the dry-run preview list sidecars for a restructure that follows
    a (simulated, not-yet-applied) rename."""
    src_dir = os.path.dirname(video_path)
    src_stem = os.path.splitext(os.path.basename(video_path))[0]
    remainders = []
    try:
        names = os.listdir(src_dir)
    except OSError:
        return remainders
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
# Parsing & path remapping
# --------------------------------------------------------------------------- #
def read_mapping(path):
    """Load a mapping JSON file (the list of per-file objects written by
    write_mapping) back into entry dicts. Only 'old_path', 'new_name', and
    'media_type' are required by the apply phase; any extra plexapi metadata is
    carried along untouched. A missing/invalid media_type is normalised to None
    so the restructure step falls back to asking."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  Couldn't read mapping JSON ({e}).")
        return []
    if not isinstance(data, list):
        print("  Mapping JSON should be a list of entries.")
        return []
    entries = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        old_path = str(obj.get("old_path") or "").strip().replace("\\", "/")
        new_name = str(obj.get("new_name") or "").strip()
        media_type = obj.get("media_type")
        if isinstance(media_type, str):
            media_type = media_type.strip().lower()
            if media_type not in ("movie", "tv"):
                media_type = None
        else:
            media_type = None
        if old_path and new_name:
            obj["old_path"] = old_path
            obj["new_name"] = new_name
            obj["media_type"] = media_type
            entries.append(obj)
    return entries


def detect_recorded_root(old_paths):
    """The library root as recorded in the mapping (server-side)."""
    if len(old_paths) == 1:
        return os.path.dirname(old_paths[0])
    try:
        return os.path.commonpath(old_paths)
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
        rel = os.path.basename(op)
    return rel.split("/")


def build_items(entries: list[dict], library_folder: str) -> tuple[list[dict], str]:
    """Map every mapping entry (a rich dict) onto the local filesystem. This is
    the slow phase on a network share: each item gets an existence check plus a
    sidecar scan (an os.listdir), so it shows progress as it goes."""
    old_paths = [e["old_path"] for e in entries]
    recorded_root = detect_recorded_root(old_paths)

    items = []
    progress = make_progress("Scanning local file", len(entries))
    on_progress_line = False
    for n, entry in enumerate(entries, start=1):
        on_progress_line = progress(n)
        old_path = entry["old_path"]
        comps = relative_components(old_path, recorded_root)
        local_path = os.path.join(library_folder, *comps)
        items.append({
            "old_recorded": old_path,
            "rel_comps": comps,
            "levels": len(comps) - 1,          # folders between root and file
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
            "sidecar_remainders": find_sidecar_remainders(local_path),
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
        if ask_yes_no(f"\nBring ALL {len(outliers)} outlier(s) into line with "
                      "the majority?", default="n"):
            for it in outliers:
                it["leave_alone"] = False
            return majority_levels
        if ask_yes_no(f"Leave ALL {len(outliers)} outlier(s) completely alone "
                      "(not renamed)?", default="y"):
            for it in outliers:
                it["leave_alone"] = True
            print("  -> All outliers will be left alone.")
            return majority_levels
        print("Deciding each outlier individually:")

    for it in outliers:
        print(f"\n  File : {it['current_path']}")
        print(f"  This : {describe_levels(it['levels'])}")
        if ask_yes_no("  Change it to match the rest?", default="n"):
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
    parts = [sanitize(p) for p in rel.split(os.sep)]
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


def preview_and_confirm(plan, title, dry_run=False):
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
    return ask_yes_no(f"\nApply these {len(plan)} video(s)"
                      f"{f' (plus {sidecar_total} sidecar file(s))' if sidecar_total else ''}?",
                      default="n")


def safe_move(src, dst):
    """Create the destination folder and move src -> dst, retrying once on a
    transient OSError (common on network shares/NAS). Returns None on success,
    or an error-detail string on failure (the caller logs it). Both the makedirs
    and the move are guarded so a permission/IO error skips just this file
    instead of aborting the whole run."""
    import time
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
    progress = make_progress(label, total)
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
            undo_log.write(f"{d}{SEP}{s}\n")
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


def apply_mapping(entries, library_folder, dry_run, log_dir=None):
    """Phase 2: remap onto the local folder, plan, confirm, apply, restructure.
    Undo/skip logs are written to log_dir (defaults to ~/Downloads)."""
    log_dir = log_dir or DOWNLOADS
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
        if not ask_yes_no("Keep going with the ones that were found?", default="n"):
            return

    majority_levels = analyze_and_handle_outliers(items)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log = RunLog(os.path.join(log_dir, f"plex_rename_skipped_{stamp}.txt"),
                     header="Items skipped or failed during apply")
    if dry_run:
        undo_log = None
        undo_path = None
    else:
        undo_path = os.path.join(log_dir, f"plex_rename_undo_{stamp}.txt")
        undo_log = open(undo_path, "w", encoding="utf-8")
        print(f"\nUndo log: {undo_path}")

    try:
        plan = build_rename_plan(items, majority_levels, library_folder)
        if preview_and_confirm(plan, "RENAME PLAN", dry_run):
            execute_plan(plan, undo_log, run_log, dry_run, label="Renaming")
        else:
            print("Rename step skipped.")

        print("\n--- Optional: organize into Jellyfin's recommended folders ---")
        print("This puts each movie/show in its own folder the way Jellyfin")
        print("likes best, e.g. 'Heat (1995)/Heat (1995).mkv'.")
        if ask_yes_no("Organize the files into these folders too?", default="n"):
            # The mapping records each item's type (movie/tv), so a mixed library
            # restructures correctly without asking. Older mapping files don't
            # carry the type; for those, ask once and apply it to every item that
            # is missing one.
            active = [it for it in items if not it["leave_alone"]]
            if active and not all(it["media_type"] for it in active):
                mt = ask_choice("Media type wasn't recorded in the mapping. "
                                "What type of library is this?",
                                [("movie", "Movies  -> Title (Year)/Title (Year).ext"),
                                 ("tv", "TV Shows -> Series (Year)/Season NN/episode.ext")])
                media_type = "movie" if mt == "movie" else "tv"
                for it in active:
                    if not it["media_type"]:
                        it["media_type"] = media_type

            # An empty plan means everything already matches the recommended
            # layout; preview_and_confirm reports that as "nothing to do".
            jplan = build_jellyfin_plan(items, library_folder)
            if preview_and_confirm(jplan, "JELLYFIN RESTRUCTURE PLAN", dry_run):
                execute_plan(jplan, undo_log, run_log, dry_run, label="Organizing")
            elif jplan:
                print("Restructure skipped.")

        # Clean up any folders left empty by the moves above (logged to undo).
        removed = cleanup_empty_dirs(library_folder, undo_log=undo_log, dry_run=dry_run)
        if removed:
            label = "Would remove" if dry_run else "Removed"
            print(f"\n{label} {len(removed)} empty folder(s):")
            for d in removed:
                print(f"  {d}")
    finally:
        if undo_log is not None:
            undo_log.close()
        run_log.close()

    print("\nDone." if not dry_run else "\nDone (dry run — nothing changed).")
    if run_log.created:
        print(f"Some items were skipped/failed. See:\n  {run_log.path}")
    if undo_path is not None:
        print(f"To reverse changes, the undo log maps new -> original at:\n  {undo_path}")


# =========================================================================== #
# Main
# =========================================================================== #
def parse_args():
    p = argparse.ArgumentParser(
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
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    return p.parse_args()


def resolve_input_path(value, prompt, must_be_file=False, must_be_dir=False):
    """Use a path passed on the command line if given (validated), otherwise
    fall back to the interactive prompt."""
    if value is None:
        return ask_path(prompt, must_be_file=must_be_file, must_be_dir=must_be_dir)
    p = clean_path_input(value)
    if must_be_file and not os.path.isfile(p):
        print(f"Not a file: {p}")
        sys.exit(1)
    if must_be_dir and not os.path.isdir(p):
        print(f"Not a folder: {p}")
        sys.exit(1)
    return p


def default_export_path():
    # Timestamped so a second export never silently overwrites the first.
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(DOWNLOADS, f"plex_rename_list_{stamp}.json")


def configure_interactively(args):
    """Run only when the script is launched with no flags. Offers to turn the
    optional settings on before the normal run. The user can decline, or pick
    any combination from the list; selecting nothing just continues as normal.
    Mutates `args` in place to reflect the choices."""
    if not ask_yes_no("\nView/change script settings before starting?",
                      default="n"):
        return

    # key -> human label. These mirror the command-line flags (except --version,
    # which just prints the version and exits, so it isn't a toggleable setting).
    options = [
        ("dry-run",      "Dry run — preview every change, touch nothing"),
        ("export",       "Save the Plex mapping (with all metadata) to a JSON file"),
        ("export-only",  "Export only — build the mapping, then stop (no apply)"),
        ("from-mapping", "Apply from an existing mapping file (skip Plex)"),
        ("log-dir",      "Choose where undo/skip logs are written (default: ~/Downloads)"),
    ]
    chosen = set(ask_multichoice("\nAvailable settings — choose any "
                                 "combination:", options))

    if not chosen:
        print("No settings selected; continuing normally.")
        return

    if "dry-run" in chosen:
        args.dry_run = True
    if "export-only" in chosen:
        args.export_only = True
    if "export" in chosen:
        p = ask(f"Export file path (blank for {default_export_path()}): ")
        args.export_file = p or default_export_path()
    if "from-mapping" in chosen:
        args.from_mapping = ask_path("Path to the saved mapping (.json) file: ",
                                     must_be_file=True)
        if "export" in chosen or "export-only" in chosen:
            print("Note: applying from an existing mapping skips Plex, so the "
                  "export settings will be ignored.")
    if "log-dir" in chosen:
        args.log_dir = ask_path("Folder to write undo/skip logs into: ",
                                must_be_dir=True)

    enabled = []
    if args.dry_run:
        enabled.append("dry run")
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


def ensure_plexapi():
    """Fail fast (before any prompts) if plexapi isn't installed, so the user
    doesn't answer connection questions only to hit an ImportError later."""
    try:
        import plexapi  # noqa: F401
    except ImportError:
        print("This step needs the 'plexapi' package, which isn't installed.\n"
              "Install it with:\n  pip install plexapi")
        sys.exit(1)


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
    apply_mapping(entries, library_folder, dry_run, log_dir)


def main(args):
    print("=== Plex -> Jellyfin rename tool ===")
    # With no flags given, offer the interactive settings picker first.
    if not (args.dry_run or args.export_only or args.export_file
            or args.from_mapping):
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
            elif ask_yes_no("Save the mapping (with all Plex metadata) to a JSON "
                            "file first (optional)?", default="n"):
                write_mapping(entries, default_export_path())

            if args.export_only:
                print("\nExport-only mode: done with this library.")
            else:
                run_apply_phase(entries, args, dry_run, log_dir)

        if not ask_yes_no("\nProcess another Plex library on this server?",
                          default="n"):
            break
        # Each further library prompts fresh for its own folder/export path.
        args.library = None
        args.export_file = None


if __name__ == "__main__":
    try:
        main(parse_args())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
