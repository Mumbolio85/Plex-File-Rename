#!/usr/bin/env python3
"""
TypedDict definitions for the per-file entry dicts that flow through the
pipeline.  These are plain dicts at runtime — zero migration cost — but a type
checker (mypy / pyright / Pylance) validates key names and gives autocomplete.

Two concrete shapes exist because movies and TV episodes carry different
metadata fields:

    MovieEntry  — one physical file that belongs to a Plex movie item
    TVEntry     — one physical file that belongs to a Plex TV episode

Both extend _EntryBase, which holds the fields every downstream phase
(apply, jellyfin, undo) needs regardless of media type.

The ``plex`` sub-dict is left as ``dict[str, object]`` intentionally: it is a
best-effort snapshot of arbitrary plexapi attributes captured by
``plex_attrs()``, and its exact shape depends on the Plex server version.  A
strict sub-model would be fragile and provide little value.
"""

from __future__ import annotations

from typing import TypedDict


class WatchedState(TypedDict):
    """Snapshot of the Plex user-data fields migrated into Jellyfin."""
    view_count: int
    view_offset_ms: int
    last_viewed_at: str | None
    user_rating: float | None


class _EntryBase(TypedDict, total=False):
    """Fields present on every entry regardless of media type.

    ``total=False`` because the JSON round-trip path (``read_mapping``) creates
    entries from user-supplied files that may legally omit optional fields, and
    TypedDict inheritance would make everything required by default.
    """
    # ---- required by every downstream phase ----
    old_path: str            # server-recorded path (posix slashes)
    new_name: str            # just the filename, no directory
    media_type: str | None   # "movie", "tv", or None

    # ---- step-7 (watched-state migration) ----
    watched_state: WatchedState
    provider_ids: list[str]  # e.g. ["imdb://tt0113277", "tmdb://603"]

    # ---- apply-phase result (written back after mapping) ----
    result_path: str         # absolute path after the rename

    # ---- raw plexapi snapshot ----
    plex: dict[str, object]


class MovieEntry(_EntryBase, total=False):
    """One physical file belonging to a Plex movie item."""
    title: str
    year: int | None
    edition: str | None          # edition tag appended to the filename, if any
    edition_title: str | None    # Plex's own editionTitle field
    video_resolution: str        # e.g. "1080p", "4k"
    part_index: int              # 1-based; >1 only for multi-part movies
    total_parts: int


class TVEntry(_EntryBase, total=False):
    """One physical file belonging to a Plex TV episode."""
    show_title: str
    show_year: int | None
    season: int
    episode: int
    episode_title: str
    part_index: int
    total_parts: int
    show_provider_ids: list[str]  # series-level IDs for season/episode fallback


# Union used in function signatures where both shapes are accepted.
Entry = MovieEntry | TVEntry
