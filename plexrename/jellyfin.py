#!/usr/bin/env python3
"""
Step 7: migrate Plex watched-state into Jellyfin (v2.0).

Steps 1-6 only move *files*. They never carry Plex *user data* -- watched/
unwatched, play counts, resume positions, user ratings. This module migrates
that into Jellyfin over its REST API (version- and backend-agnostic, works on
any 10.11.x and even a Postgres backend, server stays running).

It is gated on step 6 (the Jellyfin restructure) having run, because the primary
way it matches a Plex item to a Jellyfin item is the final on-disk path
(`result_path`) produced by that restructure; provider IDs (IMDb/TMDb/TVDB) are
the fallback.

No third-party dependencies: the Jellyfin client uses urllib from the stdlib.

Connecting to Jellyfin
----------------------
Two methods (Jellyfin has no Plex-style "View XML" URL that embeds a token):
  1. Enter the server URL and an API key.
  2. Log in with a Jellyfin username and password.

To generate an API key:
  1. Log in to your Jellyfin server.
  2. Select your user profile.
  3. Choose the (admin) Dashboard.
  4. Scroll down the left sidebar to "API Keys" and select it.
  5. Choose "New API Key" and give it a name.
  6. Copy it.

Favorites are intentionally NOT migrated in v2.0: Plex has no native per-item
favorite flag, so there is nothing reliable to carry across.
"""

from __future__ import annotations

import os
import json
import getpass
import urllib.error
import urllib.parse
import urllib.request

from plexrename import __version__
from plexrename.common import (
    SEP, USERDATA_SENTINEL, ask, ask_choice, make_progress,
)

# Sent in the Authorization header; purely cosmetic identity for the server.
CLIENT_NAME = "plex-rename"
CLIENT_VERSION = __version__

# Resume positions: Plex stores viewOffset in milliseconds, Jellyfin stores
# PlaybackPositionTicks in 100-nanosecond ticks.
TICKS_PER_MS = 10_000

# Page size for the (potentially huge) library listing.
PAGE_SIZE = 500


# =========================================================================== #
# Jellyfin REST client
# =========================================================================== #
class JellyfinClient:
    """Thin urllib wrapper around a Jellyfin server + access token.

    The token (an API key, or a user AccessToken from login) is sent on every
    request via the header:
        Authorization: MediaBrowser Token="<token>", Client="plex-rename",
                       Device="plex-rename", DeviceId="...", Version="2.0.0"
    _request() builds this header; nothing else needs to know the format."""

    def __init__(self, base_url, token, user_id=None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.user_id = user_id

    # -- low level ---------------------------------------------------------- #
    def _auth_header(self):
        return (f'MediaBrowser Token="{self.token}", Client="{CLIENT_NAME}", '
                f'Device="{CLIENT_NAME}", DeviceId="{CLIENT_NAME}", '
                f'Version="{CLIENT_VERSION}"')

    def _request(self, method, path, params=None, body=None):
        """Make a request and return parsed JSON (or None for an empty body).

        On an HTTP error the server's response body is read and folded into the
        raised exception, so a bad key / wrong user / 4xx surfaces a useful
        message instead of a bare 'HTTP Error 401: Unauthorized'."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": self._auth_header(),
                   "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace").strip()
            except OSError:
                pass
            msg = f"HTTP {e.code} {e.reason} for {method} {path}"
            raise RuntimeError(f"{msg}: {detail}" if detail else msg) from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    # -- typed helpers ------------------------------------------------------ #
    def server_name(self):
        """Force an authenticated call so bad credentials fail here, not later."""
        info = self._request("GET", "/System/Info")
        return (info or {}).get("ServerName", "Jellyfin")

    def users(self):
        return self._request("GET", "/Users") or []

    def get_user_data(self, user_id, item_id):
        item = self._request("GET", f"/Users/{user_id}/Items/{item_id}")
        return (item or {}).get("UserData") or {}

    def set_user_data(self, user_id, item_id, data):
        self._request("POST", f"/Users/{user_id}/Items/{item_id}/UserData",
                      body=data)

    def set_favorite(self, user_id, item_id, value):
        # Unused in v2.0 (favorites are not migrated); kept so a later version
        # can add favorites without touching the client.
        method = "POST" if value else "DELETE"
        self._request(method, f"/Users/{user_id}/FavoriteItems/{item_id}")

    def iter_items(self, user_id, fields=("Path", "ProviderIds")):
        """Yield every library item, paging through the results so a large
        server isn't pulled down (and parsed) in a single giant response."""
        start = 0
        while True:
            params = {"Recursive": "true", "Fields": ",".join(fields),
                      "StartIndex": start, "Limit": PAGE_SIZE}
            res = self._request("GET", f"/Users/{user_id}/Items", params=params)
            items = (res or {}).get("Items", [])
            if not items:
                break
            for it in items:
                yield it
            # TotalRecordCount tells us when we've seen everything; fall back to
            # "a short page means the end" if the server omits it.
            total = (res or {}).get("TotalRecordCount")
            start += len(items)
            if len(items) < PAGE_SIZE or (total is not None and start >= total):
                break


# =========================================================================== #
# Connection / onboarding (mirrors connect.connect)
# =========================================================================== #
API_KEY_STEPS = (
    "Generate a Jellyfin API key:\n"
    "  1. Log in to your Jellyfin server.\n"
    "  2. Select your user profile.\n"
    "  3. Choose the (admin) Dashboard.\n"
    "  4. Scroll down the left sidebar to 'API Keys' and select it.\n"
    "  5. Choose 'New API Key' and give it a name.\n"
    "  6. Copy it and paste it here."
)


def connect_with_feedback(base_url, token, user_id=None):
    """Build a client and force a call so bad input fails here. Returns a
    connected JellyfinClient, or None on failure."""
    client = JellyfinClient(base_url, token, user_id)
    try:
        client.server_name()
        return client
    except Exception as e:
        print(f"\nCouldn't connect to Jellyfin: {e}")
        print("Check that the address is reachable and the API key is current, "
              "then try again.")
        return None


def connect_jf_via_separate():
    """Server URL + API key. Returns a connected JellyfinClient, or None."""
    base_url = ask("\nJellyfin server URL (e.g. http://127.0.0.1:8096): ")
    if not base_url:
        return None
    print("\n" + API_KEY_STEPS)
    token = ask("\nPaste your Jellyfin API key: ")
    if not token:
        print("  An API key is required.")
        return None
    return connect_with_feedback(base_url, token)


def connect_jf_via_login():
    """Log in with username/password (POST /Users/AuthenticateByName). The
    response carries an AccessToken and the User.Id, so the chosen user is known
    and choose_jellyfin_user can be skipped. Returns a client, or None."""
    base_url = ask("\nJellyfin server URL (e.g. http://127.0.0.1:8096): ")
    if not base_url:
        return None
    username = ask("Jellyfin username: ")
    if not username:
        return None
    password = getpass.getpass("Jellyfin password: ")
    client = JellyfinClient(base_url, "")  # no token yet; auth-by-name sets it
    try:
        resp = client._request("POST", "/Users/AuthenticateByName",
                               body={"Username": username, "Pw": password})
    except Exception as e:
        print(f"  Login failed: {e}")
        return None
    if not resp or not resp.get("AccessToken"):
        print("  Login failed: no access token returned.")
        return None
    client.token = resp["AccessToken"]
    client.user_id = (resp.get("User") or {}).get("Id")
    return client


def connect_jellyfin():
    """Guided connection flow with retries and two entry points. Each entry
    point returns a connected JellyfinClient (or None to retry)."""
    entry_points = {
        "1": connect_jf_via_separate,
        "2": connect_jf_via_login,
    }
    while True:
        method = ask_choice(
            "\nHow would you like to connect to Jellyfin?",
            [("1", "Enter server URL and an API key"),
             ("2", "Log in with a Jellyfin username and password")])
        client = entry_points[method]()
        if client is None:
            continue
        return client


def choose_jellyfin_user(client):
    """Return the Jellyfin user Id whose watched-state will be written. Login
    already knows the user; an API key may see several, so we ask."""
    if client.user_id:
        return client.user_id
    users = client.users()
    if not users:
        print("  No users found on this Jellyfin server.")
        return None
    if len(users) == 1:
        return users[0].get("Id")
    print("\nWhich Jellyfin user should receive the watched-state?")
    for i, u in enumerate(users):
        print(f"  [{i}] {u.get('Name')}")
    while True:
        choice = ask("Type the number of the user: ")
        if choice.isdigit() and 0 <= int(choice) < len(users):
            return users[int(choice)].get("Id")
        print("  That isn't one of the numbers above. Try again.")


# =========================================================================== #
# Matching: Plex entry -> Jellyfin item
# =========================================================================== #
def _norm_path(p):
    """Normalise a path for matching: forward slashes, no trailing slash,
    case-folded -- the same shape result_path and Jellyfin's Path are compared
    in, so OS/separator differences don't defeat an otherwise-equal path."""
    p = (p or "").replace("\\", "/").rstrip("/")
    return p.casefold()


def _parse_plex_guid(guid):
    """'imdb://tt0113277' -> ('imdb', 'tt0113277'); None if unparseable."""
    if not isinstance(guid, str) or "://" not in guid:
        return None
    src, _, rest = guid.partition("://")
    src, rest = src.strip().lower(), rest.strip()
    if not src or not rest:
        return None
    return (src, rest)


def entry_provider_keys(guids):
    """Plex guid list -> list of (source, id) tuples."""
    out = []
    for g in guids or []:
        key = _parse_plex_guid(g)
        if key:
            out.append(key)
    return out


def jf_provider_keys(provider_ids):
    """Jellyfin ProviderIds dict ({'Imdb': 'tt...'}) -> list of (source, id)."""
    out = []
    for k, v in (provider_ids or {}).items():
        if v is None:
            continue
        out.append((str(k).strip().lower(), str(v).strip()))
    return out


def _index_put(index, bucket, key, item):
    """Record item under key in a bucket. If a *different* item already holds
    the key, mark it ambiguous so matching skips it rather than guessing."""
    existing = index[bucket].get(key)
    if existing is not None and existing.get("Id") != item.get("Id"):
        index["ambiguous"].add((bucket, key))
    else:
        index[bucket].setdefault(key, item)


def build_jellyfin_index(client, user_id):
    """One pass over the library so each entry isn't a separate HTTP call.
    Builds lookups by full path, by basename, by provider id, and (for the TV
    fallback) by (series-provider-key, season, episode)."""
    items = list(client.iter_items(user_id))
    index = {"by_path": {}, "by_basename": {}, "by_provider": {},
             "by_series_episode": {}, "ambiguous": set()}

    # First resolve each series' provider ids so episodes can borrow them.
    series_providers = {}
    for it in items:
        if (it.get("Type") or "") == "Series":
            series_providers[it.get("Id")] = jf_provider_keys(it.get("ProviderIds"))

    for it in items:
        path = it.get("Path")
        if path:
            np = _norm_path(path)
            _index_put(index, "by_path", np, it)
            _index_put(index, "by_basename", os.path.basename(np), it)
        for pk in jf_provider_keys(it.get("ProviderIds")):
            _index_put(index, "by_provider", pk, it)
        if (it.get("Type") or "") == "Episode":
            season = it.get("ParentIndexNumber")
            episode = it.get("IndexNumber")
            sid = it.get("SeriesId")
            if season is not None and episode is not None and sid in series_providers:
                for pk in series_providers[sid]:
                    _index_put(index, "by_series_episode",
                               (pk, int(season), int(episode)), it)
    return index


def _lookup(index, bucket, key):
    """Return (item, status) where status is 'HIT', 'AMBIGUOUS', or 'MISS'."""
    if (bucket, key) in index["ambiguous"]:
        return None, "AMBIGUOUS"
    item = index[bucket].get(key)
    return (item, "HIT") if item is not None else (None, "MISS")


# Each strategy returns (item, reason) on a hit, (None, "AMBIGUOUS") when it
# found competing candidates, or None to let the next strategy try.
def _match_path(entry, index):
    rp = entry.get("result_path")
    if not rp:
        return None
    item, st = _lookup(index, "by_path", _norm_path(rp))
    if st == "HIT":
        return item, "PATH"
    if st == "AMBIGUOUS":
        return None, "AMBIGUOUS"
    return None


def _match_filename(entry, index):
    """Filename fallback: match on the file's basename. Tries the final on-disk
    name (result_path) and the planned new_name -- either should equal the
    Jellyfin file's basename, so this works even when the full directory paths
    differ between this machine and the Jellyfin server."""
    candidates = []
    rp = entry.get("result_path")
    if rp:
        candidates.append(os.path.basename(_norm_path(rp)))
    new_name = entry.get("new_name")
    if new_name:
        candidates.append(_norm_path(new_name))
    for key in candidates:
        item, st = _lookup(index, "by_basename", key)
        if st == "HIT":
            return item, "FILENAME"
        if st == "AMBIGUOUS":
            return None, "AMBIGUOUS"
    return None


def _match_provider(entry, index):
    for pk in entry_provider_keys(entry.get("provider_ids")):
        item, st = _lookup(index, "by_provider", pk)
        if st == "HIT":
            return item, "PROVIDER"
        if st == "AMBIGUOUS":
            return None, "AMBIGUOUS"
    return None


def _match_series_episode(entry, index):
    if entry.get("media_type") != "tv":
        return None
    season, episode = entry.get("season"), entry.get("episode")
    if season is None or episode is None:
        return None
    for pk in entry_provider_keys(entry.get("show_provider_ids")):
        item, st = _lookup(index, "by_series_episode",
                           (pk, int(season), int(episode)))
        if st == "HIT":
            return item, "SERIES_EPISODE"
        if st == "AMBIGUOUS":
            return None, "AMBIGUOUS"
    return None


def match_jellyfin_item(entry, index, provider_first=False):
    """Return (jf_item, reason), or (None, 'UNMATCHED'/'AMBIGUOUS').

    Two precedences:
      - default (inline, right after a restructure on the same machine): match
        by full path first, then filename, then provider IDs, then TV series+S/E.
      - provider_first (standalone --migrate-watched, possibly a different
        machine where the recorded path won't line up): match by provider IDs
        first, with a fallback to the filename.

    An ambiguous key short-circuits to (None, 'AMBIGUOUS') so the caller skips
    rather than guessing."""
    if provider_first:
        strategies = (_match_provider, _match_series_episode,
                      _match_path, _match_filename)
    else:
        strategies = (_match_path, _match_filename,
                      _match_provider, _match_series_episode)
    for strategy in strategies:
        result = strategy(entry, index)
        if result is not None:
            return result
    return None, "UNMATCHED"


# =========================================================================== #
# Conflict merge (pure, fully unit-testable)
# =========================================================================== #
def merge_userdata(plex_state, jf_userdata, *, count_already_migrated):
    """Given a Plex watched-state snapshot and Jellyfin's current UserData,
    return (new_userdata, changed) applying the locked conflict rules:

      - watched:    jf.Played OR plex watched (never un-watch)
      - play count: jf + plex view_count (additive), UNLESS already migrated and
                    not forced, in which case the count is left as-is
      - resume:     max(jf ticks, plex offset)  (larger offset wins)
      - rating:     plex rating unless it would regress jf's (never lower it)

    `changed` is True only if something would actually differ, so no-op writes
    (and undo-log noise) are skipped."""
    plex_state = plex_state or {}
    jf_userdata = jf_userdata or {}

    view_count = int(plex_state.get("view_count") or 0)
    jf_played = bool(jf_userdata.get("Played"))
    played = jf_played or view_count > 0

    jf_count = int(jf_userdata.get("PlayCount") or 0)
    new_count = jf_count if count_already_migrated else jf_count + view_count

    jf_ticks = int(jf_userdata.get("PlaybackPositionTicks") or 0)
    plex_ticks = int(plex_state.get("view_offset_ms") or 0) * TICKS_PER_MS
    new_ticks = max(jf_ticks, plex_ticks)

    jf_rating = jf_userdata.get("Rating")
    plex_rating = plex_state.get("user_rating")
    if plex_rating is None:
        new_rating = jf_rating
    elif jf_rating is None or plex_rating > jf_rating:
        new_rating = plex_rating
    else:
        new_rating = jf_rating

    changed = (played != jf_played or new_count != jf_count
               or new_ticks != jf_ticks or new_rating != jf_rating)

    new_userdata = {"Played": played, "PlayCount": new_count,
                    "PlaybackPositionTicks": new_ticks}
    if new_rating is not None:
        new_userdata["Rating"] = new_rating
    return new_userdata, changed


# =========================================================================== #
# Re-run guard
# =========================================================================== #
class MigratedLog:
    """Persistent set of (user_id, item_id) pairs whose play count has already
    been added, so a re-run doesn't double-count.

    Stored as JSON Lines (one ["user", "item"] pair per line) so recording a
    pair is a cheap append instead of rewriting the whole file each time. A
    legacy single-JSON-array file (written by older versions) is still read."""

    def __init__(self, path):
        self.path = path
        self._pairs = set()
        if os.path.isfile(path):
            self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return
        # Legacy format: the whole file is one JSON array of [user, item] pairs.
        # (A single JSONL line is also valid JSON, e.g. ["u", "m"], so require
        # every element to itself be a 2-item pair before treating it as legacy.)
        data = None
        try:
            data = json.loads(text)
        except ValueError:
            pass
        if (isinstance(data, list) and data
                and all(isinstance(x, (list, tuple)) and len(x) == 2
                        and not isinstance(x[0], (list, tuple)) for x in data)):
            for pair in data:
                self._add_pair(pair)
            return
        # JSON Lines: one pair per line.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._add_pair(json.loads(line))
            except ValueError:
                continue

    def _add_pair(self, pair):
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            self._pairs.add((str(pair[0]), str(pair[1])))

    def contains(self, user_id, item_id):
        return (str(user_id), str(item_id)) in self._pairs

    def record(self, user_id, item_id):
        pair = (str(user_id), str(item_id))
        if pair in self._pairs:
            return
        self._pairs.add(pair)
        # Cheap O(1) append rather than rewriting the whole file.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps([pair[0], pair[1]]) + "\n")


# =========================================================================== #
# Core migration (one path, used by both inline and standalone entry points)
# =========================================================================== #
def migrate_watched(entries, client, user_id, *, dry_run, undo_log, run_log,
                    migrated_log, force, provider_first=False):
    """For each entry with captured watched-state: match it to a Jellyfin item,
    read that item's current UserData, merge per the rules, and (unless dry-run)
    write it back -- recording the prior UserData to undo_log first and the
    (user, item) pair to migrated_log. Unmatched/ambiguous items go to run_log.
    `provider_first` selects the matching precedence (see match_jellyfin_item);
    standalone runs pass True so provider IDs win with a filename fallback.
    Returns the number of items changed."""
    index = build_jellyfin_index(client, user_id)
    progress = make_progress("Migrating item", len(entries))
    on_progress_line = False
    changed_count = 0

    def log_skip(category, target):
        nonlocal on_progress_line
        if on_progress_line:
            print()
            on_progress_line = False
        run_log.skip(category, target)

    for n, entry in enumerate(entries, start=1):
        on_progress_line = progress(n)
        plex_state = entry.get("watched_state")
        if not plex_state:
            continue  # nothing captured to migrate for this entry

        jf_item, reason = match_jellyfin_item(entry, index,
                                              provider_first=provider_first)
        if jf_item is None:
            log_skip(reason, entry.get("result_path") or entry.get("new_name"))
            continue

        item_id = jf_item.get("Id")
        try:
            current = client.get_user_data(user_id, item_id)
        except Exception as e:
            log_skip("ERROR", f"reading UserData for {item_id}: {e}")
            continue

        already = migrated_log.contains(user_id, item_id) and not force
        new_data, changed = merge_userdata(plex_state, current,
                                           count_already_migrated=already)
        if not changed:
            continue

        if dry_run:
            changed_count += 1
            continue

        # Read-before-write: record the prior UserData so undo can restore it.
        if undo_log is not None:
            undo_log.write(f"{client.base_url}|{user_id}|{item_id}{SEP}"
                           f"{USERDATA_SENTINEL} {json.dumps(current)}\n")
            undo_log.flush()
        try:
            client.set_user_data(user_id, item_id, new_data)
        except Exception as e:
            log_skip("ERROR", f"writing UserData for {item_id}: {e}")
            continue
        if not already:
            migrated_log.record(user_id, item_id)
        changed_count += 1

    if on_progress_line:
        print()
    label = "would update" if dry_run else "updated"
    print(f"  Watched-state migration: {label} {changed_count} item(s).")
    return changed_count
