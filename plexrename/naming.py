#!/usr/bin/env python3
"""
Phase 1 building blocks: capture metadata from plexapi objects, build clean
"Title (Year)" filenames, disambiguate colliding names, and read/write the
mapping JSON. None of this touches the local filesystem or needs a live Plex
connection beyond the already-fetched objects, which is what makes it easy to
unit-test with fake Plex objects.
"""

from __future__ import annotations

import os
import re
import json
import datetime
from collections import Counter, defaultdict

from plexrename.common import sanitize, make_progress


# --------------------------------------------------------------------------- #
# Metadata capture
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


def iso_or_none(value):
    """ISO-format a datetime for JSON, pass through anything else (incl. None)."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def plex_guids(obj):
    """Return the provider GUIDs of a plexapi object as scalar strings, e.g.
    ['imdb://tt0113277', 'tmdb://603']. plex_attrs() can't capture these -- they
    live in obj.guids as nested Guid objects, not scalars -- so step 7's
    provider-ID matching needs this explicit extraction. Best-effort: a server
    that didn't return guids just yields an empty list."""
    out = []
    for g in getattr(obj, "guids", None) or []:
        gid = getattr(g, "id", None)
        if isinstance(gid, str) and gid:
            out.append(gid)
    return out


def watched_state(obj):
    """Snapshot the Plex user-data fields step 7 migrates into Jellyfin. Kept as
    first-class normalized keys (not buried in the raw plex blob) so the shape is
    stable across tool versions and matching/merging never has to dig."""
    return {
        "view_count": getattr(obj, "viewCount", 0) or 0,
        "view_offset_ms": getattr(obj, "viewOffset", 0) or 0,
        "last_viewed_at": iso_or_none(getattr(obj, "lastViewedAt", None)),
        "user_rating": getattr(obj, "userRating", None),
    }


# --------------------------------------------------------------------------- #
# Building names
# --------------------------------------------------------------------------- #
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

# Each marker is matched with word boundaries (and against the filename only,
# not the directory path) so a short tag like "OU" or "4K" can't fire inside an
# unrelated word such as "GROUP" or a folder name. Order is preserved so the
# longer/more specific marker still wins (e.g. "HDR10" before "HDR").
_EDITION_MARKER_RES = [
    (marker, re.compile(r"\b" + re.escape(marker) + r"\b", re.IGNORECASE))
    for marker in EDITION_MARKERS
]

# Matches a multi-angle tag regardless of separator/casing. The leading
# boundary keeps it from firing inside words like "Triangle" or "Angles".
ANGLE_RE = re.compile(r"\bangle[\s_\-]*(\d+)\b", re.IGNORECASE)


def path_edition_hint(path):
    """Look for a well-known edition marker (e.g. '3D', 'Angle 2') in the file's
    NAME (not its directory, so a marker-looking folder can't cause a false
    positive). Multi-angle tags are matched flexibly and normalised to
    'Angle N'; the rest are matched with word boundaries as listed in
    EDITION_MARKERS."""
    name = os.path.basename(path or "")
    m = ANGLE_RE.search(name)
    if m:
        return f"Angle {int(m.group(1))}"
    for marker, regex in _EDITION_MARKER_RES:
        if regex.search(name):
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
                # Step-7 (watched-state migration) fields, captured here while
                # we're connected to Plex; absent/ignored by steps 1-6.
                "watched_state": watched_state(item),
                "provider_ids": plex_guids(item),
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
                # Step-7 fields. provider_ids are the episode's own; the show's
                # are kept separately for the series + season/episode fallback.
                "watched_state": watched_state(episode),
                "provider_ids": plex_guids(episode),
                "show_provider_ids": plex_guids(show),
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


# --------------------------------------------------------------------------- #
# Mapping JSON IO
# --------------------------------------------------------------------------- #
def write_mapping(entries, out_path):
    """Write the full mapping as JSON: a list of per-file objects holding the
    proposed name plus all the metadata captured from plexapi. JSON keeps every
    field explicitly labelled and survives any character in a path or name,
    unlike the old separator-delimited text format."""
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(entries)} entries to {out_path}")


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
