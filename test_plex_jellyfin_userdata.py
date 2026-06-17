#!/usr/bin/env python3
"""Tests for plex_jellyfin_userdata.py (step 7: watched-state migration).

No live Jellyfin server: a FakeJellyfinClient records writes and serves canned
items/UserData, mirroring how test_plex_rename.py uses fake Plex objects.
"""

import os
import sys
import json
import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plex_jellyfin_userdata as jf
import plex_rename_common as prc


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeJellyfinClient:
    """Stand-in for JellyfinClient: serves a fixed item list + UserData and
    records every set_user_data call."""

    def __init__(self, items=None, userdata=None, base_url="http://jf:8096"):
        self.base_url = base_url
        self._items = items or []
        self._userdata = userdata or {}
        self.writes = []  # list of (user_id, item_id, data)

    def iter_items(self, user_id, fields=("Path", "ProviderIds")):
        return self._items

    def get_user_data(self, user_id, item_id):
        return dict(self._userdata.get(item_id, {}))

    def set_user_data(self, user_id, item_id, data):
        self.writes.append((user_id, item_id, dict(data)))
        self._userdata[item_id] = dict(data)

    def users(self):
        return []


# --------------------------------------------------------------------------- #
# merge_userdata (pure logic -- the heart of the conflict rules)
# --------------------------------------------------------------------------- #
class TestMergeUserdata(unittest.TestCase):
    def merge(self, plex, jf_data, migrated=False):
        return jf.merge_userdata(plex, jf_data, count_already_migrated=migrated)

    def test_watched_sets_played(self):
        data, changed = self.merge({"view_count": 1}, {})
        self.assertTrue(data["Played"])
        self.assertTrue(changed)

    def test_never_unwatches(self):
        # Plex unwatched but Jellyfin already played -> stays played.
        data, changed = self.merge({"view_count": 0}, {"Played": True, "PlayCount": 3})
        self.assertTrue(data["Played"])

    def test_play_count_additive(self):
        data, _ = self.merge({"view_count": 2}, {"PlayCount": 3})
        self.assertEqual(data["PlayCount"], 5)

    def test_play_count_guarded_when_already_migrated(self):
        data, changed = self.merge({"view_count": 2}, {"PlayCount": 3, "Played": True},
                                   migrated=True)
        self.assertEqual(data["PlayCount"], 3)   # not re-added
        self.assertFalse(changed)                # nothing else differs

    def test_resume_max_and_ms_to_ticks(self):
        # 1000 ms -> 10_000_000 ticks; wins over a smaller existing offset.
        data, _ = self.merge({"view_offset_ms": 1000}, {"PlaybackPositionTicks": 5})
        self.assertEqual(data["PlaybackPositionTicks"], 1000 * jf.TICKS_PER_MS)

    def test_resume_keeps_larger_jellyfin_offset(self):
        data, _ = self.merge({"view_offset_ms": 1}, {"PlaybackPositionTicks": 10**9})
        self.assertEqual(data["PlaybackPositionTicks"], 10**9)

    def test_rating_applies_when_higher(self):
        data, _ = self.merge({"user_rating": 9.0}, {"Rating": 5.0})
        self.assertEqual(data["Rating"], 9.0)

    def test_rating_never_regresses(self):
        data, _ = self.merge({"user_rating": 4.0}, {"Rating": 8.0})
        self.assertEqual(data["Rating"], 8.0)

    def test_rating_none_keeps_jellyfin(self):
        data, _ = self.merge({"user_rating": None}, {"Rating": 7.0})
        self.assertEqual(data["Rating"], 7.0)

    def test_rating_absent_both_omits_key(self):
        data, _ = self.merge({"view_count": 1}, {})
        self.assertNotIn("Rating", data)

    def test_no_change_is_not_changed(self):
        jf_data = {"Played": True, "PlayCount": 1, "PlaybackPositionTicks": 0}
        _, changed = self.merge({"view_count": 1}, jf_data, migrated=True)
        self.assertFalse(changed)


# --------------------------------------------------------------------------- #
# Provider-key / path helpers + matching
# --------------------------------------------------------------------------- #
class TestHelpers(unittest.TestCase):
    def test_parse_plex_guid(self):
        self.assertEqual(jf._parse_plex_guid("imdb://tt0113277"),
                         ("imdb", "tt0113277"))
        self.assertIsNone(jf._parse_plex_guid("garbage"))

    def test_entry_provider_keys(self):
        self.assertEqual(jf.entry_provider_keys(["imdb://tt1", "tmdb://2"]),
                         [("imdb", "tt1"), ("tmdb", "2")])

    def test_jf_provider_keys(self):
        self.assertEqual(jf.jf_provider_keys({"Imdb": "tt1", "Tmdb": 2}),
                         [("imdb", "tt1"), ("tmdb", "2")])

    def test_norm_path(self):
        self.assertEqual(jf._norm_path("C:\\Media\\Heat (1995)\\Heat (1995).MKV"),
                         "c:/media/heat (1995)/heat (1995).mkv")


class TestMatching(unittest.TestCase):
    def _index(self, items):
        return jf.build_jellyfin_index(FakeJellyfinClient(items=items), "u1")

    def test_match_by_path(self):
        items = [{"Id": "m1", "Type": "Movie",
                  "Path": "/lib/Heat (1995)/Heat (1995).mkv"}]
        index = self._index(items)
        entry = {"result_path": "/lib/Heat (1995)/Heat (1995).mkv"}
        item, reason = jf.match_jellyfin_item(entry, index)
        self.assertEqual(item["Id"], "m1")
        self.assertEqual(reason, "PATH")

    def test_match_by_filename_when_dir_differs(self):
        items = [{"Id": "m1", "Type": "Movie",
                  "Path": "/srv/movies/Heat (1995).mkv"}]
        index = self._index(items)
        entry = {"result_path": "/different/root/Heat (1995).mkv"}
        item, reason = jf.match_jellyfin_item(entry, index)
        self.assertEqual(item["Id"], "m1")
        self.assertEqual(reason, "FILENAME")

    def test_match_by_filename_via_new_name(self):
        # No result_path at all -- filename fallback uses new_name.
        items = [{"Id": "m1", "Type": "Movie",
                  "Path": "/srv/movies/Heat (1995).mkv"}]
        index = self._index(items)
        entry = {"new_name": "Heat (1995).mkv"}
        item, reason = jf.match_jellyfin_item(entry, index)
        self.assertEqual(item["Id"], "m1")
        self.assertEqual(reason, "FILENAME")

    def test_provider_first_prefers_provider_over_path(self):
        # Path points at p1, provider id points at p2; provider_first picks p2.
        items = [
            {"Id": "p1", "Type": "Movie", "Path": "/lib/a.mkv"},
            {"Id": "p2", "Type": "Movie", "Path": "/lib/b.mkv",
             "ProviderIds": {"Imdb": "tt7"}},
        ]
        index = self._index(items)
        entry = {"result_path": "/lib/a.mkv", "provider_ids": ["imdb://tt7"]}
        # Default precedence: path wins.
        item, reason = jf.match_jellyfin_item(entry, index)
        self.assertEqual((item["Id"], reason), ("p1", "PATH"))
        # provider_first: provider wins.
        item, reason = jf.match_jellyfin_item(entry, index, provider_first=True)
        self.assertEqual((item["Id"], reason), ("p2", "PROVIDER"))

    def test_provider_first_falls_back_to_filename(self):
        items = [{"Id": "m1", "Type": "Movie", "Path": "/srv/Heat (1995).mkv"}]
        index = self._index(items)
        entry = {"new_name": "Heat (1995).mkv", "provider_ids": ["imdb://nope"]}
        item, reason = jf.match_jellyfin_item(entry, index, provider_first=True)
        self.assertEqual((item["Id"], reason), ("m1", "FILENAME"))

    def test_match_by_provider_when_path_misses(self):
        items = [{"Id": "m1", "Type": "Movie", "Path": "/x/a.mkv",
                  "ProviderIds": {"Imdb": "tt9"}}]
        index = self._index(items)
        entry = {"result_path": "/no/match.mkv", "provider_ids": ["imdb://tt9"]}
        item, reason = jf.match_jellyfin_item(entry, index)
        self.assertEqual(item["Id"], "m1")
        self.assertEqual(reason, "PROVIDER")

    def test_match_episode_by_series_and_number(self):
        items = [
            {"Id": "S1", "Type": "Series", "ProviderIds": {"Tvdb": "99"}},
            {"Id": "e1", "Type": "Episode", "SeriesId": "S1",
             "ParentIndexNumber": 2, "IndexNumber": 5, "Path": "/x/ep.mkv"},
        ]
        index = self._index(items)
        entry = {"result_path": "/no/match.mkv", "media_type": "tv",
                 "season": 2, "episode": 5, "show_provider_ids": ["tvdb://99"]}
        item, reason = jf.match_jellyfin_item(entry, index)
        self.assertEqual(item["Id"], "e1")
        self.assertEqual(reason, "SERIES_EPISODE")

    def test_unmatched(self):
        index = self._index([{"Id": "m1", "Type": "Movie", "Path": "/x/a.mkv"}])
        item, reason = jf.match_jellyfin_item({"result_path": "/y/b.mkv"}, index)
        self.assertIsNone(item)
        self.assertEqual(reason, "UNMATCHED")

    def test_ambiguous_provider_skips(self):
        items = [
            {"Id": "m1", "Type": "Movie", "Path": "/x/a.mkv", "ProviderIds": {"Imdb": "tt1"}},
            {"Id": "m2", "Type": "Movie", "Path": "/x/b.mkv", "ProviderIds": {"Imdb": "tt1"}},
        ]
        index = self._index(items)
        entry = {"result_path": "/no/match.mkv", "provider_ids": ["imdb://tt1"]}
        item, reason = jf.match_jellyfin_item(entry, index)
        self.assertIsNone(item)
        self.assertEqual(reason, "AMBIGUOUS")


# --------------------------------------------------------------------------- #
# MigratedLog
# --------------------------------------------------------------------------- #
class TestMigratedLog(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "migrated.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_record_and_contains(self):
        log = jf.MigratedLog(self.p)
        self.assertFalse(log.contains("u1", "m1"))
        log.record("u1", "m1")
        self.assertTrue(log.contains("u1", "m1"))

    def test_persists_across_instances(self):
        jf.MigratedLog(self.p).record("u1", "m1")
        self.assertTrue(jf.MigratedLog(self.p).contains("u1", "m1"))

    def test_missing_file_is_empty(self):
        self.assertFalse(jf.MigratedLog(self.p).contains("u1", "m1"))


# --------------------------------------------------------------------------- #
# migrate_watched (core, with fake client)
# --------------------------------------------------------------------------- #
class TestMigrateWatched(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.run_log = prc.RunLog(os.path.join(self.d, "skip.txt"))
        self.migrated = jf.MigratedLog(os.path.join(self.d, "migrated.json"))

    def tearDown(self):
        import shutil
        self.run_log.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def _movie_entry(self, view_count=1):
        return {"result_path": "/lib/Heat (1995)/Heat (1995).mkv",
                "watched_state": {"view_count": view_count, "view_offset_ms": 0,
                                  "user_rating": None}}

    def _client(self):
        return FakeJellyfinClient(items=[
            {"Id": "m1", "Type": "Movie",
             "Path": "/lib/Heat (1995)/Heat (1995).mkv"}])

    def test_writes_matched_item_and_logs_undo(self):
        client = self._client()
        undo = StringIO()
        with redirect_stdout(StringIO()):
            n = jf.migrate_watched([self._movie_entry()], client, "u1",
                                   dry_run=False, undo_log=undo,
                                   run_log=self.run_log, migrated_log=self.migrated,
                                   force=False)
        self.assertEqual(n, 1)
        self.assertEqual(len(client.writes), 1)
        self.assertTrue(client.writes[0][2]["Played"])
        # Undo line: "<url>|u1|m1 ----- [[USERDATA]] {...}"
        self.assertIn(prc.USERDATA_SENTINEL, undo.getvalue())
        self.assertIn("http://jf:8096|u1|m1", undo.getvalue())
        self.assertTrue(self.migrated.contains("u1", "m1"))

    def test_dry_run_writes_nothing(self):
        client = self._client()
        with redirect_stdout(StringIO()):
            n = jf.migrate_watched([self._movie_entry()], client, "u1",
                                   dry_run=True, undo_log=None,
                                   run_log=self.run_log, migrated_log=self.migrated,
                                   force=False)
        self.assertEqual(n, 1)               # would change 1
        self.assertEqual(client.writes, [])  # but wrote nothing
        self.assertFalse(self.migrated.contains("u1", "m1"))

    def test_unmatched_is_logged(self):
        client = self._client()
        entry = self._movie_entry()
        entry["result_path"] = "/nowhere/ghost.mkv"  # no path/provider match
        with redirect_stdout(StringIO()):
            n = jf.migrate_watched([entry], client, "u1", dry_run=False,
                                   undo_log=StringIO(), run_log=self.run_log,
                                   migrated_log=self.migrated, force=False)
        self.assertEqual(n, 0)
        self.assertEqual(client.writes, [])
        self.assertTrue(self.run_log.created)

    def test_rerun_guard_blocks_double_count(self):
        client = self._client()
        entry = self._movie_entry(view_count=2)
        with redirect_stdout(StringIO()):
            jf.migrate_watched([entry], client, "u1", dry_run=False,
                               undo_log=StringIO(), run_log=self.run_log,
                               migrated_log=self.migrated, force=False)
            # Second run: count already migrated, nothing else differs -> no write.
            n2 = jf.migrate_watched([entry], client, "u1", dry_run=False,
                                    undo_log=StringIO(), run_log=self.run_log,
                                    migrated_log=self.migrated, force=False)
        self.assertEqual(n2, 0)
        self.assertEqual(len(client.writes), 1)         # only the first run wrote
        self.assertEqual(client.writes[0][2]["PlayCount"], 2)

    def test_provider_first_matches_without_path(self):
        client = FakeJellyfinClient(items=[
            {"Id": "m1", "Type": "Movie", "Path": "/server/side/path.mkv",
             "ProviderIds": {"Imdb": "tt5"}}])
        entry = {"new_name": "Heat (1995).mkv", "provider_ids": ["imdb://tt5"],
                 "watched_state": {"view_count": 1}}
        with redirect_stdout(StringIO()):
            n = jf.migrate_watched([entry], client, "u1", dry_run=False,
                                   undo_log=StringIO(), run_log=self.run_log,
                                   migrated_log=self.migrated, force=False,
                                   provider_first=True)
        self.assertEqual(n, 1)
        self.assertEqual(client.writes[0][1], "m1")

    def test_force_readds_count(self):
        client = self._client()
        entry = self._movie_entry(view_count=2)
        with redirect_stdout(StringIO()):
            jf.migrate_watched([entry], client, "u1", dry_run=False,
                               undo_log=StringIO(), run_log=self.run_log,
                               migrated_log=self.migrated, force=False)
            jf.migrate_watched([entry], client, "u1", dry_run=False,
                               undo_log=StringIO(), run_log=self.run_log,
                               migrated_log=self.migrated, force=True)
        self.assertEqual(len(client.writes), 2)
        self.assertEqual(client.writes[1][2]["PlayCount"], 4)  # 2 + 2 again


# --------------------------------------------------------------------------- #
# Connection / onboarding
# --------------------------------------------------------------------------- #
class TestConnectViaSeparate(unittest.TestCase):
    def setUp(self):
        self._ask = jf.ask
        self._cwf = jf.connect_with_feedback

    def tearDown(self):
        jf.ask = self._ask
        jf.connect_with_feedback = self._cwf

    def test_url_and_key_connects(self):
        answers = iter(["http://jf:8096", "APIKEY"])
        jf.ask = lambda *a, **k: next(answers)
        captured = {}
        sentinel = object()
        jf.connect_with_feedback = lambda b, t: captured.update(b=b, t=t) or sentinel
        with redirect_stdout(StringIO()):
            self.assertIs(jf.connect_jf_via_separate(), sentinel)
        self.assertEqual(captured, {"b": "http://jf:8096", "t": "APIKEY"})

    def test_missing_key_returns_none(self):
        answers = iter(["http://jf:8096", ""])
        jf.ask = lambda *a, **k: next(answers)
        with redirect_stdout(StringIO()):
            self.assertIsNone(jf.connect_jf_via_separate())


class TestConnectViaLogin(unittest.TestCase):
    def setUp(self):
        self._ask = jf.ask
        self._getpass = jf.getpass.getpass
        self._request = jf.JellyfinClient._request

    def tearDown(self):
        jf.ask = self._ask
        jf.getpass.getpass = self._getpass
        jf.JellyfinClient._request = self._request

    def test_login_sets_token_and_user(self):
        answers = iter(["http://jf:8096", "alice"])
        jf.ask = lambda *a, **k: next(answers)
        jf.getpass.getpass = lambda *a, **k: "pw"
        jf.JellyfinClient._request = (
            lambda self, m, p, params=None, body=None:
            {"AccessToken": "tok", "User": {"Id": "u1"}})
        with redirect_stdout(StringIO()):
            client = jf.connect_jf_via_login()
        self.assertEqual(client.token, "tok")
        self.assertEqual(client.user_id, "u1")


class TestAuthHeaderAndUser(unittest.TestCase):
    def test_auth_header_carries_token(self):
        header = jf.JellyfinClient("http://x", "TOKEN")._auth_header()
        self.assertIn('Token="TOKEN"', header)
        self.assertIn('Client="plex-rename"', header)

    def test_choose_user_uses_login_user(self):
        client = jf.JellyfinClient("http://x", "t", user_id="u9")
        self.assertEqual(jf.choose_jellyfin_user(client), "u9")

    def test_choose_single_user(self):
        client = FakeJellyfinClient()
        client.user_id = None
        client.users = lambda: [{"Id": "only", "Name": "Solo"}]
        self.assertEqual(jf.choose_jellyfin_user(client), "only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
