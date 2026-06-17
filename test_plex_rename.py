#!/usr/bin/env python3
"""Comprehensive tests for plex_rename.py and plex_rename_common.py.

Covers pure helpers, filesystem operations (in temp dirs), JSON round-trips,
fake-Plex scanning, plan building, execution (with sidecars/undo), and the
apply_mapping orchestration. No live Plex server required -- plexapi imports
live inside functions, so we feed in lightweight fake objects.
"""

import os
import sys
import json
import shutil
import tempfile
import datetime
import unittest
from io import StringIO
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plex_rename as pr
import plex_rename_common as prc


# --------------------------------------------------------------------------- #
# Fake plexapi objects
# --------------------------------------------------------------------------- #
class FakePart:
    def __init__(self, file):
        self.file = file


class FakeMedia:
    def __init__(self, parts, title=None, videoResolution=None):
        self.parts = [FakePart(p) for p in parts]
        self.title = title
        self.videoResolution = videoResolution


class FakeMovie:
    type = "movie"

    def __init__(self, title, year, media, editionTitle=None):
        self.title = title
        self.year = year
        self.media = media
        self.editionTitle = editionTitle


class FakeEpisode:
    def __init__(self, parentIndex, index, title, media):
        self.parentIndex = parentIndex
        self.index = index
        self.title = title
        self.media = media


class FakeShow:
    type = "show"

    def __init__(self, title, year, episodes):
        self.title = title
        self.year = year
        self._episodes = episodes

    def episodes(self):
        return self._episodes


class FakeMusic:
    type = "artist"

    def __init__(self, title):
        self.title = title


class FakeSection:
    def __init__(self, title, type_, items):
        self.title = title
        self.type = type_
        self._items = items

    def all(self):
        return self._items


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
class TestExtractXmlUrl(unittest.TestCase):
    def test_full_http(self):
        url = "http://127.0.0.1:32400/library/metadata/123?x=1&X-Plex-Token=abc123&y=2"
        self.assertEqual(pr.extract_from_xml_url(url),
                         ("http://127.0.0.1:32400", "abc123"))

    def test_https(self):
        url = "https://192-168-1-2.plex.direct:32400/foo?X-Plex-Token=TOK"
        self.assertEqual(pr.extract_from_xml_url(url),
                         ("https://192-168-1-2.plex.direct:32400", "TOK"))

    def test_whitespace_stripped(self):
        url = "  http://h:32400/a?X-Plex-Token=t  "
        self.assertEqual(pr.extract_from_xml_url(url), ("http://h:32400", "t"))

    def test_missing_token(self):
        b, t = pr.extract_from_xml_url("http://h:32400/library/metadata/1")
        self.assertEqual(b, "http://h:32400")
        self.assertIsNone(t)

    def test_missing_server(self):
        b, t = pr.extract_from_xml_url("X-Plex-Token=abc")
        self.assertIsNone(b)
        self.assertEqual(t, "abc")

    def test_token_terminated_by_amp(self):
        url = "http://h:32400/?X-Plex-Token=abc&next=1"
        self.assertEqual(pr.extract_from_xml_url(url)[1], "abc")


class TestBuildNewName(unittest.TestCase):
    def test_year(self):
        self.assertEqual(pr.build_new_name("Heat", 1995, ".mkv"), "Heat (1995).mkv")

    def test_no_year(self):
        self.assertEqual(pr.build_new_name("Heat", None, ".mkv"), "Heat.mkv")

    def test_edition(self):
        self.assertEqual(pr.build_new_name("Heat", 1995, ".mkv", edition="IMAX"),
                         "Heat (1995) - [IMAX].mkv")

    def test_multi_part(self):
        self.assertEqual(
            pr.build_new_name("Heat", 1995, ".mkv", part_index=2, total_parts=2),
            "Heat (1995) - part2.mkv")

    def test_edition_and_part(self):
        self.assertEqual(
            pr.build_new_name("Heat", 1995, ".mkv", part_index=1, total_parts=2,
                              edition="IMAX"),
            "Heat (1995) - [IMAX] - part1.mkv")

    def test_sanitizes_illegal_chars(self):
        self.assertEqual(pr.build_new_name("A:B?C", 2000, ".mp4"), "ABC (2000).mp4")


class TestPathEditionHint(unittest.TestCase):
    def test_angle_variants(self):
        self.assertEqual(pr.path_edition_hint("movie angle1.mkv"), "Angle 1")
        self.assertEqual(pr.path_edition_hint("movie Angle 2.mkv"), "Angle 2")
        self.assertEqual(pr.path_edition_hint("movie angle-3.mkv"), "Angle 3")

    def test_angle_word_boundary(self):
        self.assertIsNone(pr.path_edition_hint("Triangle.mkv"))
        self.assertIsNone(pr.path_edition_hint("Angles of attack.mkv"))

    def test_longer_marker_first(self):
        # "Full-SBS" must win over "SBS"
        self.assertEqual(pr.path_edition_hint("movie Full-SBS.mkv"), "Full-SBS")
        # "HDR10" must win over "HDR"
        self.assertEqual(pr.path_edition_hint("movie HDR10.mkv"), "HDR10")
        # "Extended Cut" before "Extended"
        self.assertEqual(pr.path_edition_hint("movie Extended Cut.mkv"),
                         "Extended Cut")

    def test_case_insensitive(self):
        self.assertEqual(pr.path_edition_hint("movie imax.mkv"), "IMAX")

    def test_none(self):
        self.assertIsNone(pr.path_edition_hint("plain movie.mkv"))


class TestMediaEditionLabel(unittest.TestCase):
    def test_title_preferred(self):
        m = FakeMedia(["/x.mkv"], title="Director's Cut")
        self.assertEqual(pr.media_edition_label(m, 1), "Director's Cut")

    def test_path_hint(self):
        m = FakeMedia(["/movie 3D.mkv"])
        self.assertEqual(pr.media_edition_label(m, 1), "3D")

    def test_resolution_digit(self):
        m = FakeMedia(["/x.mkv"], videoResolution="1080")
        self.assertEqual(pr.media_edition_label(m, 1), "1080p")

    def test_resolution_4k(self):
        m = FakeMedia(["/x.mkv"], videoResolution="4k")
        self.assertEqual(pr.media_edition_label(m, 1), "4k")

    def test_version_fallback(self):
        m = FakeMedia(["/x.mkv"])
        self.assertEqual(pr.media_edition_label(m, 3), "version 3")


class TestVideoParts(unittest.TestCase):
    def test_basic(self):
        m = FakeMedia(["/a/b/file.mkv"])
        out = list(pr.video_parts(m))
        self.assertEqual(len(out), 1)
        i, total, old, ext, part = out[0]
        self.assertEqual((i, total, old, ext), (1, 1, "/a/b/file.mkv", ".mkv"))

    def test_backslash_normalized(self):
        m = FakeMedia([r"D:\Media\Movie\file.mkv"])
        _, _, old, _, _ = list(pr.video_parts(m))[0]
        self.assertEqual(old, "D:/Media/Movie/file.mkv")

    def test_multi_part_total(self):
        m = FakeMedia(["/a/p1.mkv", "/a/p2.mkv"])
        out = list(pr.video_parts(m))
        self.assertEqual([(o[0], o[1]) for o in out], [(1, 2), (2, 2)])

    def test_none_file(self):
        p = FakePart(None)
        m = FakeMedia([])
        m.parts = [p]
        _, _, old, ext, _ = list(pr.video_parts(m))[0]
        self.assertEqual(old, "")
        self.assertEqual(ext, "")


class TestPlexAttrs(unittest.TestCase):
    def test_scalars_and_none(self):
        class O:
            pass
        o = O()
        o.a, o.b, o.c, o.d = "s", 3, 1.5, True
        o.n = None
        self.assertEqual(pr.plex_attrs(o),
                         {"a": "s", "b": 3, "c": 1.5, "d": True, "n": None})

    def test_private_skipped(self):
        class O:
            pass
        o = O()
        o._server = object()
        o.title = "x"
        self.assertEqual(pr.plex_attrs(o), {"title": "x"})

    def test_datetime_iso(self):
        class O:
            pass
        o = O()
        o.added = datetime.datetime(2020, 1, 2, 3, 4, 5)
        self.assertEqual(pr.plex_attrs(o)["added"], "2020-01-02T03:04:05")

    def test_list_of_scalars(self):
        class O:
            pass
        o = O()
        o.genres = ["action", "drama"]
        o.objs = [object(), object()]
        out = pr.plex_attrs(o)
        self.assertEqual(out["genres"], ["action", "drama"])
        self.assertNotIn("objs", out)  # list of non-scalars dropped

    def test_non_vars_object(self):
        self.assertEqual(pr.plex_attrs(42), {})


class TestRelativeComponents(unittest.TestCase):
    def test_under_root(self):
        self.assertEqual(
            pr.relative_components("/srv/media/Heat/Heat.mkv", "/srv/media"),
            ["Heat", "Heat.mkv"])

    def test_not_under_root_basename(self):
        self.assertEqual(
            pr.relative_components("/other/path/file.mkv", "/srv/media"),
            ["file.mkv"])

    def test_trailing_slashes(self):
        self.assertEqual(
            pr.relative_components("/srv/media/x.mkv", "/srv/media/"),
            ["x.mkv"])


class TestDetectRecordedRoot(unittest.TestCase):
    def test_single(self):
        self.assertEqual(pr.detect_recorded_root(["/a/b/c.mkv"]), "/a/b")

    def test_common(self):
        self.assertEqual(
            pr.detect_recorded_root(["/a/b/c.mkv", "/a/b/d/e.mkv"]), "/a/b")

    def test_mixed_roots_exits(self):
        # commonpath raises ValueError on mixed absolute/relative -> sys.exit
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit):
                pr.detect_recorded_root(["/a/b.mkv", "rel/c.mkv"])


class TestDescribeLevels(unittest.TestCase):
    def test_known(self):
        self.assertIn("loose", pr.describe_levels(0))
        self.assertIn("own folder", pr.describe_levels(1))

    def test_unknown(self):
        self.assertEqual(pr.describe_levels(5), "nested 5 folders deep")


class TestJellyfinTarget(unittest.TestCase):
    def test_movie_simple(self):
        self.assertEqual(
            pr.jellyfin_target("Heat (1995).mkv", "movie", "/lib"),
            os.path.join("/lib", "Heat (1995)", "Heat (1995).mkv"))

    def test_movie_strips_edition(self):
        self.assertEqual(
            pr.jellyfin_target("Heat (1995) - [IMAX].mkv", "movie", "/lib"),
            os.path.join("/lib", "Heat (1995)", "Heat (1995) - [IMAX].mkv"))

    def test_movie_strips_part(self):
        self.assertEqual(
            pr.jellyfin_target("Heat (1995) - part2.mkv", "movie", "/lib"),
            os.path.join("/lib", "Heat (1995)", "Heat (1995) - part2.mkv"))

    def test_tv(self):
        self.assertEqual(
            pr.jellyfin_target("Show (2000) - S02E05 - Title.mkv", "tv", "/lib"),
            os.path.join("/lib", "Show (2000)", "Season 02",
                         "Show (2000) - S02E05 - Title.mkv"))

    def test_tv_unparseable(self):
        self.assertIsNone(pr.jellyfin_target("random.mkv", "tv", "/lib"))

    def test_unknown_type(self):
        self.assertIsNone(pr.jellyfin_target("x.mkv", None, "/lib"))


class TestNormalizedDir(unittest.TestCase):
    def test_level0(self):
        it = {"new_name": "Heat (1995).mkv", "current_dir": "/lib/x"}
        self.assertEqual(pr.normalized_dir(it, 0, "/lib"), "/lib")

    def test_level1(self):
        it = {"new_name": "Heat (1995).mkv", "current_dir": "/lib/x"}
        self.assertEqual(pr.normalized_dir(it, 1, "/lib"),
                         os.path.join("/lib", "Heat (1995)"))

    def test_level2_keeps(self):
        it = {"new_name": "Heat (1995).mkv", "current_dir": "/lib/x"}
        self.assertEqual(pr.normalized_dir(it, 2, "/lib"), "/lib/x")


# --------------------------------------------------------------------------- #
# sanitize / sanitize_under_root
# --------------------------------------------------------------------------- #
class TestSanitize(unittest.TestCase):
    def test_strips(self):
        self.assertEqual(prc.sanitize('a<b>c:"/\\|?*d'), "abcd")

    def test_trims(self):
        self.assertEqual(prc.sanitize("  hi  "), "hi")


class TestSanitizeUnderRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_strips_below_root(self):
        p = os.path.join(self.root, 'Show: A', 'ep?.mkv')
        out = pr.sanitize_under_root(p, self.root)
        self.assertEqual(out, os.path.join(self.root, 'Show A', 'ep.mkv'))

    def test_root_untouched(self):
        # The root prefix itself is left alone even if it contains odd chars.
        self.assertEqual(pr.sanitize_under_root(self.root, self.root), self.root)

    def test_outside_root_unchanged(self):
        other = "/somewhere/else/file.mkv"
        self.assertEqual(pr.sanitize_under_root(other, self.root), other)


# --------------------------------------------------------------------------- #
# find_sidecar_remainders + sidecar_pairs (filesystem)
# --------------------------------------------------------------------------- #
class TestFindSidecarRemainders(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _touch(self, name):
        open(os.path.join(self.d, name), "w").close()

    def test_subtitle_nfo_image(self):
        for n in ["Movie.mkv", "Movie.en.srt", "Movie.nfo", "Movie-poster.jpg"]:
            self._touch(n)
        rems = pr.find_sidecar_remainders(os.path.join(self.d, "Movie.mkv"))
        self.assertEqual(set(rems), {".en.srt", ".nfo", "-poster.jpg"})

    def test_image_without_known_suffix_not_sidecar(self):
        self._touch("Movie.mkv")
        self._touch("Movie-random.jpg")
        self.assertEqual(
            pr.find_sidecar_remainders(os.path.join(self.d, "Movie.mkv")), [])

    def test_boundary_prevents_prefix_collision(self):
        # "Movie 2.mkv" must NOT be treated as a sidecar of "Movie".
        self._touch("Movie.mkv")
        self._touch("Movie 2.mkv")
        self.assertEqual(
            pr.find_sidecar_remainders(os.path.join(self.d, "Movie.mkv")), [])

    def test_video_itself_skipped(self):
        self._touch("Movie.mkv")
        self.assertEqual(
            pr.find_sidecar_remainders(os.path.join(self.d, "Movie.mkv")), [])

    def test_missing_dir(self):
        self.assertEqual(
            pr.find_sidecar_remainders("/no/such/dir/Movie.mkv"), [])


class TestSidecarPairs(unittest.TestCase):
    def test_projects_stems(self):
        pairs = pr.sidecar_pairs([".en.srt", "-poster.jpg"],
                                 "/a/Movie.mkv", "/b/Heat (1995).mkv")
        self.assertEqual(pairs, [
            ("/a/Movie.en.srt", "/b/Heat (1995).en.srt"),
            ("/a/Movie-poster.jpg", "/b/Heat (1995)-poster.jpg"),
        ])

    def test_empty(self):
        self.assertEqual(pr.sidecar_pairs([], "/a/x.mkv", "/b/y.mkv"), [])


# --------------------------------------------------------------------------- #
# write_mapping / read_mapping
# --------------------------------------------------------------------------- #
class TestMappingIO(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "map.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_round_trip(self):
        entries = [{"old_path": "/a/b.mkv", "new_name": "B (2000).mkv",
                    "media_type": "movie", "extra": [1, 2, 3]}]
        with redirect_stdout(StringIO()):
            pr.write_mapping(entries, self.p)
        out = pr.read_mapping(self.p)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["old_path"], "/a/b.mkv")
        self.assertEqual(out[0]["new_name"], "B (2000).mkv")
        self.assertEqual(out[0]["media_type"], "movie")
        self.assertEqual(out[0]["extra"], [1, 2, 3])  # extra carried along

    def test_backslash_normalized(self):
        json.dump([{"old_path": r"D:\m\f.mkv", "new_name": "f.mkv"}],
                  open(self.p, "w"))
        out = pr.read_mapping(self.p)
        self.assertEqual(out[0]["old_path"], "D:/m/f.mkv")

    def test_invalid_media_type_to_none(self):
        json.dump([{"old_path": "/a.mkv", "new_name": "a.mkv",
                    "media_type": "weird"}], open(self.p, "w"))
        self.assertIsNone(pr.read_mapping(self.p)[0]["media_type"])

    def test_missing_fields_skipped(self):
        json.dump([{"old_path": "", "new_name": "x"},
                   {"old_path": "/a.mkv", "new_name": ""},
                   {"old_path": "/b.mkv", "new_name": "b.mkv"}],
                  open(self.p, "w"))
        out = pr.read_mapping(self.p)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["old_path"], "/b.mkv")

    def test_non_list(self):
        json.dump({"not": "a list"}, open(self.p, "w"))
        with redirect_stdout(StringIO()):
            self.assertEqual(pr.read_mapping(self.p), [])

    def test_invalid_json(self):
        open(self.p, "w").write("{ not json")
        with redirect_stdout(StringIO()):
            self.assertEqual(pr.read_mapping(self.p), [])

    def test_non_dict_entries_skipped(self):
        json.dump(["string", 5, {"old_path": "/a.mkv", "new_name": "a.mkv"}],
                  open(self.p, "w"))
        self.assertEqual(len(pr.read_mapping(self.p)), 1)

    def test_missing_file(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(pr.read_mapping(os.path.join(self.d, "nope.json")),
                             [])


# --------------------------------------------------------------------------- #
# Collectors (fake Plex)
# --------------------------------------------------------------------------- #
class TestCollectors(unittest.TestCase):
    def test_single_movie(self):
        item = FakeMovie("Heat", 1995, [FakeMedia(["/srv/Heat/Heat.mkv"])])
        entries = pr.collect_movie_entries(item)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["new_name"], "Heat (1995).mkv")
        self.assertEqual(e["media_type"], "movie")
        self.assertEqual(e["old_path"], "/srv/Heat/Heat.mkv")
        self.assertIn("plex", e)

    def test_multi_part_movie(self):
        item = FakeMovie("Heat", 1995,
                         [FakeMedia(["/srv/Heat/cd1.mkv", "/srv/Heat/cd2.mkv"])])
        entries = pr.collect_movie_entries(item)
        names = sorted(e["new_name"] for e in entries)
        self.assertEqual(names, ["Heat (1995) - part1.mkv",
                                 "Heat (1995) - part2.mkv"])

    def test_multi_media_editions(self):
        item = FakeMovie("Heat", 1995, [
            FakeMedia(["/srv/Heat/a.mkv"], videoResolution="1080"),
            FakeMedia(["/srv/Heat/b.mkv"], videoResolution="720"),
        ])
        entries = pr.collect_movie_entries(item)
        names = sorted(e["new_name"] for e in entries)
        self.assertEqual(names, ["Heat (1995) - [1080p].mkv",
                                 "Heat (1995) - [720p].mkv"])

    def test_multi_media_duplicate_label(self):
        item = FakeMovie("Heat", 1995, [
            FakeMedia(["/srv/Heat/a.mkv"], videoResolution="1080"),
            FakeMedia(["/srv/Heat/b.mkv"], videoResolution="1080"),
        ])
        entries = pr.collect_movie_entries(item)
        names = sorted(e["new_name"] for e in entries)
        self.assertEqual(names, ["Heat (1995) - [1080p (2)].mkv",
                                 "Heat (1995) - [1080p].mkv"])

    def test_episode(self):
        ep = FakeEpisode(2, 5, "The One", [FakeMedia(["/srv/Show/s2e5.mkv"])])
        show = FakeShow("Show", 2000, [ep])
        entries = pr.collect_episode_entries(show, ep)
        self.assertEqual(entries[0]["new_name"],
                         "Show (2000) - S02E05 - The One.mkv")
        self.assertEqual(entries[0]["season"], 2)
        self.assertEqual(entries[0]["episode"], 5)

    def test_episode_multipart(self):
        ep = FakeEpisode(1, 1, "Pilot",
                         [FakeMedia(["/srv/Show/a.mkv", "/srv/Show/b.mkv"])])
        show = FakeShow("Show", None, [ep])
        entries = pr.collect_episode_entries(show, ep)
        names = sorted(e["new_name"] for e in entries)
        self.assertEqual(names, ["Show - S01E01 - Pilot - part1.mkv",
                                 "Show - S01E01 - Pilot - part2.mkv"])

    def test_collect_entries_mixed_and_skips(self):
        movie = FakeMovie("Heat", 1995, [FakeMedia(["/srv/m/Heat.mkv"])])
        ep = FakeEpisode(1, 1, "Pilot", [FakeMedia(["/srv/s/Show/s1e1.mkv"])])
        show = FakeShow("Show", 2000, [ep])
        music = FakeMusic("Band")
        section = FakeSection("Mixed", "movie", [movie, show, music])
        buf = StringIO()
        with redirect_stdout(buf):
            entries = pr.collect_entries(section)
        types = sorted(e["media_type"] for e in entries)
        self.assertEqual(types, ["movie", "tv"])
        self.assertIn("Skipped unsupported item type(s): 1 artist", buf.getvalue())


class TestDisambiguateMovies(unittest.TestCase):
    def test_cross_item_collision_path_hint(self):
        entries = [
            {"old_path": "/srv/Heat IMAX.mkv", "new_name": "Heat (1995).mkv",
             "edition_title": "", "video_resolution": ""},
            {"old_path": "/srv/Heat.mkv", "new_name": "Heat (1995).mkv",
             "edition_title": "", "video_resolution": ""},
        ]
        pr.disambiguate_movies(entries)
        names = sorted(e["new_name"] for e in entries)
        self.assertEqual(names,
                         ["Heat (1995) - [IMAX].mkv",
                          "Heat (1995) - [version 2].mkv"])

    def test_edition_title_preferred(self):
        entries = [
            {"old_path": "/a.mkv", "new_name": "X (2000).mkv",
             "edition_title": "Director's Cut", "video_resolution": ""},
            {"old_path": "/b.mkv", "new_name": "X (2000).mkv",
             "edition_title": "Theatrical", "video_resolution": ""},
        ]
        pr.disambiguate_movies(entries)
        names = sorted(e["new_name"] for e in entries)
        # sanitize() strips <>:"/\|?* but NOT apostrophes, so "Director's" stays.
        self.assertEqual(names, ["X (2000) - [Director's Cut].mkv",
                                 "X (2000) - [Theatrical].mkv"])

    def test_same_old_path_not_collision(self):
        entries = [
            {"old_path": "/a.mkv", "new_name": "X (2000).mkv",
             "edition_title": "", "video_resolution": ""},
            {"old_path": "/a.mkv", "new_name": "X (2000).mkv",
             "edition_title": "", "video_resolution": ""},
        ]
        pr.disambiguate_movies(entries)
        # untouched, since old_path is identical (same physical file)
        self.assertTrue(all(e["new_name"] == "X (2000).mkv" for e in entries))


# --------------------------------------------------------------------------- #
# build_items
# --------------------------------------------------------------------------- #
class TestBuildItems(unittest.TestCase):
    def test_levels_and_paths(self):
        entries = [
            {"old_path": "/srv/media/Heat/Heat.mkv", "new_name": "Heat (1995).mkv",
             "media_type": "movie"},
            {"old_path": "/srv/media/Top/Top.mkv", "new_name": "Top (1986).mkv",
             "media_type": "movie"},
        ]
        items, root = pr.build_items(entries, "/local/lib")
        self.assertEqual(root, "/srv/media")
        self.assertEqual(items[0]["levels"], 1)
        self.assertEqual(items[0]["current_path"],
                         os.path.join("/local/lib", "Heat", "Heat.mkv"))
        self.assertEqual(items[0]["current_dir"],
                         os.path.join("/local/lib", "Heat"))
        # Sidecars captured at build time (none here -- paths don't exist).
        self.assertEqual(items[0]["sidecar_remainders"], [])


# --------------------------------------------------------------------------- #
# Plan building + execution (filesystem)
# --------------------------------------------------------------------------- #
class TestPlansAndExecute(unittest.TestCase):
    def setUp(self):
        self.lib = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.lib, ignore_errors=True)

    def _mk(self, *rel):
        path = os.path.join(self.lib, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return path

    def test_rename_plan_includes_sidecars(self):
        self._mk("Heat", "orig.mkv")
        self._mk("Heat", "orig.en.srt")
        it = {
            "current_path": os.path.join(self.lib, "Heat", "orig.mkv"),
            "current_dir": os.path.join(self.lib, "Heat"),
            "levels": 1, "new_name": "Heat (1995).mkv", "leave_alone": False,
            "result_path": os.path.join(self.lib, "Heat", "orig.mkv"),
            "sidecar_remainders": [".en.srt"],
        }
        plan = pr.build_rename_plan([it], 1, self.lib)
        self.assertEqual(len(plan), 1)
        _, src, dst, sidecars = plan[0]
        self.assertEqual(os.path.basename(dst), "Heat (1995).mkv")
        self.assertEqual(len(sidecars), 1)
        self.assertEqual(os.path.basename(sidecars[0][0]), "orig.en.srt")
        self.assertEqual(os.path.basename(sidecars[0][1]), "Heat (1995).en.srt")

    def test_rename_plan_skips_noop(self):
        self._mk("Heat (1995).mkv")
        it = {
            "current_path": os.path.join(self.lib, "Heat (1995).mkv"),
            "current_dir": self.lib, "levels": 0,
            "new_name": "Heat (1995).mkv", "leave_alone": False,
            "result_path": os.path.join(self.lib, "Heat (1995).mkv"),
            "sidecar_remainders": [],
        }
        plan = pr.build_rename_plan([it], 0, self.lib)
        self.assertEqual(plan, [])

    def test_leave_alone_excluded(self):
        it = {"current_path": "/x", "current_dir": "/", "levels": 0,
              "new_name": "n.mkv", "leave_alone": True, "result_path": "/x",
              "sidecar_remainders": []}
        self.assertEqual(pr.build_rename_plan([it], 0, self.lib), [])

    def test_execute_moves_video_and_sidecar(self):
        src = self._mk("Heat", "orig.mkv")
        sc = self._mk("Heat", "orig.en.srt")
        dst = os.path.join(self.lib, "Heat", "Heat (1995).mkv")
        dst_sc = os.path.join(self.lib, "Heat", "Heat (1995).en.srt")
        it = {"result_path": src}
        plan = [(it, src, dst, [(sc, dst_sc)])]
        undo = StringIO()
        run_log = prc.RunLog(os.path.join(self.lib, "skip.txt"))
        with redirect_stdout(StringIO()):
            done = pr.execute_plan(plan, undo, run_log, dry_run=False)
        self.assertEqual(done, 1)
        self.assertTrue(os.path.exists(dst))
        self.assertTrue(os.path.exists(dst_sc))
        self.assertFalse(os.path.exists(src))
        self.assertEqual(it["result_path"], dst)
        # undo log records both moves (new -> original)
        self.assertIn(dst, undo.getvalue())
        self.assertIn(dst_sc, undo.getvalue())
        run_log.close()

    def test_execute_dry_run_no_changes(self):
        src = self._mk("Heat", "orig.mkv")
        dst = os.path.join(self.lib, "Heat", "Heat (1995).mkv")
        it = {"result_path": src}
        plan = [(it, src, dst, [])]
        run_log = prc.RunLog(os.path.join(self.lib, "skip.txt"))
        with redirect_stdout(StringIO()):
            done = pr.execute_plan(plan, None, run_log, dry_run=True)
        self.assertEqual(done, 1)
        self.assertTrue(os.path.exists(src))   # untouched
        self.assertFalse(os.path.exists(dst))
        run_log.close()

    def test_execute_target_exists_skips(self):
        src = self._mk("a", "orig.mkv")
        dst = self._mk("a", "Heat (1995).mkv")  # already exists
        it = {"result_path": src}
        plan = [(it, src, dst, [])]
        run_log = prc.RunLog(os.path.join(self.lib, "skip.txt"))
        with redirect_stdout(StringIO()):
            done = pr.execute_plan(plan, StringIO(), run_log, dry_run=False)
        self.assertEqual(done, 0)
        self.assertTrue(os.path.exists(src))   # not moved
        run_log.close()
        self.assertTrue(run_log.created)

    def test_execute_missing_src_skips(self):
        src = os.path.join(self.lib, "ghost.mkv")
        dst = os.path.join(self.lib, "new.mkv")
        it = {"result_path": src}
        run_log = prc.RunLog(os.path.join(self.lib, "skip.txt"))
        with redirect_stdout(StringIO()):
            done = pr.execute_plan([(it, src, dst, [])], StringIO(), run_log)
        self.assertEqual(done, 0)
        run_log.close()

    def test_execute_sidecar_moves_but_video_target_exists(self):
        # Issue #5 regression: video target already exists, only sidecar moves.
        # result_path must NOT advance and the move must not be counted.
        src = self._mk("a", "orig.mkv")
        sc = self._mk("a", "orig.en.srt")
        dst = self._mk("a", "Heat (1995).mkv")  # blocks the video move
        dst_sc = os.path.join(self.lib, "a", "Heat (1995).en.srt")
        it = {"result_path": src}
        plan = [(it, src, dst, [(sc, dst_sc)])]
        run_log = prc.RunLog(os.path.join(self.lib, "skip.txt"))
        with redirect_stdout(StringIO()):
            done = pr.execute_plan(plan, StringIO(), run_log, dry_run=False)
        self.assertEqual(done, 0)             # not counted
        self.assertEqual(it["result_path"], src)  # NOT advanced to dst
        self.assertTrue(os.path.exists(src))  # video still in place
        run_log.close()

    def test_jellyfin_plan(self):
        # video sitting loose; restructure into Title (Year)/...
        src = self._mk("Heat (1995).mkv")
        it = {"result_path": src, "media_type": "movie", "leave_alone": False,
              "sidecar_remainders": []}
        plan = pr.build_jellyfin_plan([it], self.lib)
        self.assertEqual(len(plan), 1)
        _, s, d, _ = plan[0]
        self.assertEqual(d, os.path.join(self.lib, "Heat (1995)", "Heat (1995).mkv"))

    def test_jellyfin_plan_unparseable_skipped(self):
        src = self._mk("random.mkv")
        it = {"result_path": src, "media_type": "tv", "leave_alone": False,
              "sidecar_remainders": []}
        with redirect_stdout(StringIO()) as buf:
            plan = pr.build_jellyfin_plan([it], self.lib)
        self.assertEqual(plan, [])
        self.assertIn("Could not parse", buf.getvalue())


class TestPreviewAndConfirm(unittest.TestCase):
    def test_empty(self):
        with redirect_stdout(StringIO()) as buf:
            self.assertFalse(pr.preview_and_confirm([], "TITLE"))
        self.assertIn("nothing to do", buf.getvalue())

    def test_dry_run_true(self):
        plan = [({}, "/a.mkv", "/b.mkv", [])]
        with redirect_stdout(StringIO()):
            self.assertTrue(pr.preview_and_confirm(plan, "T", dry_run=True))

    def test_shows_sidecars(self):
        plan = [({}, "/a.mkv", "/b.mkv", [("/a.srt", "/b.srt")])]
        with redirect_stdout(StringIO()) as buf:
            pr.preview_and_confirm(plan, "T", dry_run=True)
        out = buf.getvalue()
        self.assertIn("1 sidecar(s)", out)
        self.assertIn("a.srt", out)

    def test_confirm_yes(self):
        plan = [({}, "/a.mkv", "/b.mkv", [])]
        orig = pr.ask_yes_no
        pr.ask_yes_no = lambda *a, **k: True
        try:
            with redirect_stdout(StringIO()):
                self.assertTrue(pr.preview_and_confirm(plan, "T", dry_run=False))
        finally:
            pr.ask_yes_no = orig


# --------------------------------------------------------------------------- #
# cleanup_empty_dirs (common)
# --------------------------------------------------------------------------- #
class TestCleanupEmptyDirs(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_removes_empty(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        with redirect_stdout(StringIO()):
            removed = prc.cleanup_empty_dirs(self.root)
        self.assertIn(empty, removed)
        self.assertFalse(os.path.exists(empty))

    def test_ds_store_counts_as_empty(self):
        d = os.path.join(self.root, "x")
        os.makedirs(d)
        open(os.path.join(d, ".DS_Store"), "w").close()
        with redirect_stdout(StringIO()):
            removed = prc.cleanup_empty_dirs(self.root)
        self.assertIn(d, removed)

    def test_keeps_nonempty_and_root(self):
        d = os.path.join(self.root, "x")
        os.makedirs(d)
        open(os.path.join(d, "f.mkv"), "w").close()
        with redirect_stdout(StringIO()):
            removed = prc.cleanup_empty_dirs(self.root)
        self.assertEqual(removed, [])
        self.assertTrue(os.path.exists(self.root))

    def test_undo_log_mkdir(self):
        d = os.path.join(self.root, "x")
        os.makedirs(d)
        undo = StringIO()
        with redirect_stdout(StringIO()):
            prc.cleanup_empty_dirs(self.root, undo_log=undo)
        self.assertIn(prc.MKDIR_SENTINEL, undo.getvalue())

    def test_dry_run(self):
        d = os.path.join(self.root, "x")
        os.makedirs(d)
        with redirect_stdout(StringIO()):
            removed = prc.cleanup_empty_dirs(self.root, dry_run=True)
        self.assertIn(d, removed)
        self.assertTrue(os.path.exists(d))  # not actually removed


# --------------------------------------------------------------------------- #
# analyze_and_handle_outliers (interactive -> patched)
# --------------------------------------------------------------------------- #
class TestAnalyzeOutliers(unittest.TestCase):
    def test_all_same(self):
        items = [{"levels": 1, "current_path": "/a", "leave_alone": False},
                 {"levels": 1, "current_path": "/b", "leave_alone": False}]
        with redirect_stdout(StringIO()):
            maj = pr.analyze_and_handle_outliers(items)
        self.assertEqual(maj, 1)

    def test_outlier_left_alone(self):
        items = [{"levels": 1, "current_path": "/a", "leave_alone": False},
                 {"levels": 1, "current_path": "/b", "leave_alone": False},
                 {"levels": 0, "current_path": "/c", "leave_alone": False}]
        orig = pr.ask_yes_no
        pr.ask_yes_no = lambda *a, **k: False  # "no, don't change it"
        try:
            with redirect_stdout(StringIO()):
                maj = pr.analyze_and_handle_outliers(items)
        finally:
            pr.ask_yes_no = orig
        self.assertEqual(maj, 1)
        self.assertTrue(items[2]["leave_alone"])


# --------------------------------------------------------------------------- #
# apply_mapping orchestration (integration, patched prompts)
# --------------------------------------------------------------------------- #
class TestApplyMappingIntegration(unittest.TestCase):
    def setUp(self):
        self.lib = tempfile.mkdtemp()
        self.dl = tempfile.mkdtemp()
        self._orig_downloads = pr.DOWNLOADS
        pr.DOWNLOADS = self.dl
        self._orig_yn = pr.ask_yes_no
        self._orig_choice = pr.ask_choice

    def tearDown(self):
        pr.DOWNLOADS = self._orig_downloads
        pr.ask_yes_no = self._orig_yn
        pr.ask_choice = self._orig_choice
        shutil.rmtree(self.lib, ignore_errors=True)
        shutil.rmtree(self.dl, ignore_errors=True)

    def _mk(self, *rel):
        path = os.path.join(self.lib, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return path

    def test_full_rename_then_restructure(self):
        # Library laid out as one-folder-per-item with messy names. Two items so
        # the recorded library root is detectable (commonpath of >1 path).
        self._mk("Heat", "heat.mkv")
        self._mk("Heat", "heat.en.srt")
        self._mk("Top", "top.mkv")
        entries = [
            {"old_path": "/srv/media/Heat/heat.mkv",
             "new_name": "Heat (1995).mkv", "media_type": "movie"},
            {"old_path": "/srv/media/Top/top.mkv",
             "new_name": "Top (1986).mkv", "media_type": "movie"},
        ]
        # Answer "yes" to everything (continue, restructure, confirms).
        pr.ask_yes_no = lambda *a, **k: True
        with redirect_stdout(StringIO()):
            pr.apply_mapping(entries, self.lib, dry_run=False)
        # Renamed and restructured into Title (Year)/Title (Year).ext
        final = os.path.join(self.lib, "Heat (1995)", "Heat (1995).mkv")
        final_sc = os.path.join(self.lib, "Heat (1995)", "Heat (1995).en.srt")
        final2 = os.path.join(self.lib, "Top (1986)", "Top (1986).mkv")
        self.assertTrue(os.path.exists(final))
        self.assertTrue(os.path.exists(final_sc))
        self.assertTrue(os.path.exists(final2))
        # Undo log written
        undo_logs = [f for f in os.listdir(self.dl) if "undo" in f]
        self.assertTrue(undo_logs)

    def test_dry_run_changes_nothing(self):
        self._mk("Heat", "heat.mkv")
        self._mk("Top", "top.mkv")
        entries = [
            {"old_path": "/srv/media/Heat/heat.mkv",
             "new_name": "Heat (1995).mkv", "media_type": "movie"},
            {"old_path": "/srv/media/Top/top.mkv",
             "new_name": "Top (1986).mkv", "media_type": "movie"},
        ]
        pr.ask_yes_no = lambda *a, **k: True
        with redirect_stdout(StringIO()):
            pr.apply_mapping(entries, self.lib, dry_run=True)
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Heat", "heat.mkv")))
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Top", "top.mkv")))
        # No undo log in dry run
        self.assertFalse([f for f in os.listdir(self.dl) if "undo" in f])

    def test_dry_run_restructure_lists_sidecars(self):
        # Regression: in dry run the restructure step follows a *simulated*
        # rename, so its sidecars must still be projected into the preview.
        self._mk("Heat", "heat.mkv")
        self._mk("Heat", "heat.en.srt")
        self._mk("Top", "top.mkv")
        entries = [
            {"old_path": "/srv/media/Heat/heat.mkv",
             "new_name": "Heat (1995).mkv", "media_type": "movie"},
            {"old_path": "/srv/media/Top/top.mkv",
             "new_name": "Top (1986).mkv", "media_type": "movie"},
        ]
        pr.ask_yes_no = lambda *a, **k: True
        buf = StringIO()
        with redirect_stdout(buf):
            pr.apply_mapping(entries, self.lib, dry_run=True)
        out = buf.getvalue()
        self.assertIn("JELLYFIN RESTRUCTURE PLAN", out)
        restructure = out.split("JELLYFIN RESTRUCTURE PLAN", 1)[1]
        # The sidecar's projected restructure destination must appear.
        expected = os.path.join(self.lib, "Heat (1995)", "Heat (1995).en.srt")
        self.assertIn(expected, restructure)
        # ...and nothing actually moved.
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Heat", "heat.en.srt")))

    def test_none_found_aborts(self):
        # entries point at files that don't exist locally
        entries = [{"old_path": "/srv/media/Heat/heat.mkv",
                    "new_name": "Heat (1995).mkv", "media_type": "movie"}]
        pr.ask_yes_no = lambda *a, **k: True
        buf = StringIO()
        with redirect_stdout(buf):
            pr.apply_mapping(entries, self.lib, dry_run=False)
        self.assertIn("Couldn't find ANY of the files", buf.getvalue())

    def test_restructure_declined(self):
        # Two items already in the final layout.
        self._mk("Heat (1995)", "Heat (1995).mkv")
        self._mk("Top (1986)", "Top (1986).mkv")
        entries = [
            {"old_path": "/srv/media/Heat (1995)/Heat (1995).mkv",
             "new_name": "Heat (1995).mkv", "media_type": "movie"},
            {"old_path": "/srv/media/Top (1986)/Top (1986).mkv",
             "new_name": "Top (1986).mkv", "media_type": "movie"},
        ]

        def yn(prompt, default="n"):
            return False if "Restructure" in prompt else True
        pr.ask_yes_no = yn
        with redirect_stdout(StringIO()):
            pr.apply_mapping(entries, self.lib, dry_run=False)
        # Files stay where they were (already correctly named/placed)
        self.assertTrue(os.path.exists(
            os.path.join(self.lib, "Heat (1995)", "Heat (1995).mkv")))
        self.assertTrue(os.path.exists(
            os.path.join(self.lib, "Top (1986)", "Top (1986).mkv")))


# --------------------------------------------------------------------------- #
# CLI / onboarding helpers
# --------------------------------------------------------------------------- #
class TestResolveInputPath(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._orig_ask_path = pr.ask_path

    def tearDown(self):
        pr.ask_path = self._orig_ask_path
        shutil.rmtree(self.d, ignore_errors=True)

    def test_valid_file(self):
        f = os.path.join(self.d, "f.json")
        open(f, "w").close()
        self.assertEqual(pr.resolve_input_path(f, "p", must_be_file=True), f)

    def test_strips_quotes_and_expands(self):
        f = os.path.join(self.d, "f.json")
        open(f, "w").close()
        self.assertEqual(pr.resolve_input_path(f'"{f}"', "p", must_be_file=True), f)

    def test_invalid_file_exits(self):
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit):
                pr.resolve_input_path("/no/such.json", "p", must_be_file=True)

    def test_invalid_dir_exits(self):
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit):
                pr.resolve_input_path("/no/such/dir", "p", must_be_dir=True)

    def test_none_falls_back_to_prompt(self):
        pr.ask_path = lambda *a, **k: "/prompted/path"
        self.assertEqual(pr.resolve_input_path(None, "p"), "/prompted/path")


class TestConnectViaSeparate(unittest.TestCase):
    def setUp(self):
        self._orig_ask = pr.ask
        self._orig_try = pr.try_connect

    def tearDown(self):
        pr.ask = self._orig_ask
        pr.try_connect = self._orig_try

    def test_both_present_connects(self):
        answers = iter(["http://h:32400", "tok"])
        pr.ask = lambda *a, **k: next(answers)
        sentinel = object()
        captured = {}
        pr.try_connect = lambda b, t: captured.update(b=b, t=t) or sentinel
        self.assertIs(pr.connect_via_separate(), sentinel)
        self.assertEqual(captured, {"b": "http://h:32400", "t": "tok"})

    def test_missing_returns_none(self):
        answers = iter(["", ""])
        pr.ask = lambda *a, **k: next(answers)
        with redirect_stdout(StringIO()):
            self.assertIsNone(pr.connect_via_separate())

    def test_connect_failure_returns_none(self):
        answers = iter(["http://h:32400", "tok"])
        pr.ask = lambda *a, **k: next(answers)
        def boom(*a, **k):
            raise ConnectionError("nope")
        pr.try_connect = boom
        with redirect_stdout(StringIO()):
            self.assertIsNone(pr.connect_via_separate())


class TestConnectViaXmlUrl(unittest.TestCase):
    def setUp(self):
        self._orig_ask = pr.ask
        self._orig_try = pr.try_connect

    def tearDown(self):
        pr.ask = self._orig_ask
        pr.try_connect = self._orig_try

    def test_valid_connects(self):
        pr.ask = lambda *a, **k: "http://h:32400/x?X-Plex-Token=tok"
        sentinel = object()
        captured = {}
        pr.try_connect = lambda b, t: captured.update(b=b, t=t) or sentinel
        with redirect_stdout(StringIO()):
            self.assertIs(pr.connect_via_xml_url(), sentinel)
        self.assertEqual(captured, {"b": "http://h:32400", "t": "tok"})

    def test_blank_goes_back(self):
        pr.ask = lambda *a, **k: ""
        with redirect_stdout(StringIO()):
            self.assertIsNone(pr.connect_via_xml_url())

    def test_bad_url_then_blank(self):
        answers = iter(["not a url", ""])
        pr.ask = lambda *a, **k: next(answers)
        with redirect_stdout(StringIO()):
            self.assertIsNone(pr.connect_via_xml_url())

    def test_connect_failure_then_blank(self):
        # First a valid URL that fails to connect, then blank to go back.
        answers = iter(["http://h:32400/x?X-Plex-Token=tok", ""])
        pr.ask = lambda *a, **k: next(answers)
        def boom(*a, **k):
            raise ConnectionError("nope")
        pr.try_connect = boom
        with redirect_stdout(StringIO()):
            self.assertIsNone(pr.connect_via_xml_url())


class FakeLibrary:
    def __init__(self, sections):
        self._sections = sections

    def sections(self):
        return self._sections


class FakePlex:
    def __init__(self, sections):
        self.library = FakeLibrary(sections)


class TestChooseLibrary(unittest.TestCase):
    def setUp(self):
        self._orig = pr.ask

    def tearDown(self):
        pr.ask = self._orig

    def test_valid_choice(self):
        secs = [FakeSection("Movies", "movie", []),
                FakeSection("TV", "show", [])]
        plex = FakePlex(secs)
        pr.ask = lambda *a, **k: "1"
        with redirect_stdout(StringIO()):
            chosen = pr.choose_library(plex)
        self.assertIs(chosen, secs[1])

    def test_retries_on_bad_input(self):
        secs = [FakeSection("Movies", "movie", [])]
        plex = FakePlex(secs)
        answers = iter(["9", "x", "0"])
        pr.ask = lambda *a, **k: next(answers)
        with redirect_stdout(StringIO()):
            chosen = pr.choose_library(plex)
        self.assertIs(chosen, secs[0])


class TestDefaultExportPath(unittest.TestCase):
    def test_under_downloads(self):
        name = os.path.basename(pr.default_export_path())
        self.assertTrue(name.startswith("plex_rename_list_"))
        self.assertTrue(name.endswith(".json"))


class TestConfigureInteractively(unittest.TestCase):
    def setUp(self):
        self._orig_yn = pr.ask_yes_no
        self._orig_ask = pr.ask
        self._orig_ask_path = pr.ask_path
        self._orig_multi = pr.ask_multichoice

    def tearDown(self):
        pr.ask_yes_no = self._orig_yn
        pr.ask = self._orig_ask
        pr.ask_path = self._orig_ask_path
        pr.ask_multichoice = self._orig_multi

    def _args(self):
        import argparse
        return argparse.Namespace(dry_run=False, export_only=False,
                                  export_file=None, from_mapping=None)

    def test_declined(self):
        pr.ask_yes_no = lambda *a, **k: False
        args = self._args()
        with redirect_stdout(StringIO()):
            pr.configure_interactively(args)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.export_only)

    def test_selects_dry_run_and_export_only(self):
        pr.ask_yes_no = lambda *a, **k: True
        pr.ask_multichoice = lambda *a, **k: ["dry-run", "export-only"]
        args = self._args()
        with redirect_stdout(StringIO()):
            pr.configure_interactively(args)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.export_only)

    def test_export_with_custom_path(self):
        pr.ask_yes_no = lambda *a, **k: True
        pr.ask_multichoice = lambda *a, **k: ["export"]
        pr.ask = lambda *a, **k: "/tmp/custom.json"  # the export path prompt
        args = self._args()
        with redirect_stdout(StringIO()):
            pr.configure_interactively(args)
        self.assertEqual(args.export_file, "/tmp/custom.json")

    def test_from_mapping(self):
        pr.ask_yes_no = lambda *a, **k: True
        pr.ask_multichoice = lambda *a, **k: ["from-mapping"]
        pr.ask_path = lambda *a, **k: "/tmp/map.json"
        args = self._args()
        with redirect_stdout(StringIO()):
            pr.configure_interactively(args)
        self.assertEqual(args.from_mapping, "/tmp/map.json")

    def test_no_valid_selection(self):
        pr.ask_yes_no = lambda *a, **k: True
        pr.ask_multichoice = lambda *a, **k: []
        args = self._args()
        with redirect_stdout(StringIO()):
            pr.configure_interactively(args)
        self.assertFalse(args.dry_run)

    def test_log_dir(self):
        pr.ask_yes_no = lambda *a, **k: True
        pr.ask_multichoice = lambda *a, **k: ["log-dir"]
        pr.ask_path = lambda *a, **k: "/tmp/logs"
        args = self._args()
        with redirect_stdout(StringIO()):
            pr.configure_interactively(args)
        self.assertEqual(args.log_dir, "/tmp/logs")


class TestAskMultichoice(unittest.TestCase):
    def setUp(self):
        self._orig = prc.ask

    def tearDown(self):
        prc.ask = self._orig

    def _opts(self):
        return [("a", "A"), ("b", "B"), ("c", "C")]

    def test_parses_numbers(self):
        prc.ask = lambda *a, **k: "1, 3"
        with redirect_stdout(StringIO()):
            self.assertEqual(prc.ask_multichoice("p", self._opts()), ["a", "c"])

    def test_blank_is_empty(self):
        prc.ask = lambda *a, **k: ""
        with redirect_stdout(StringIO()):
            self.assertEqual(prc.ask_multichoice("p", self._opts()), [])

    def test_ignores_out_of_range_and_dupes(self):
        prc.ask = lambda *a, **k: "2 2 9 x"
        with redirect_stdout(StringIO()):
            self.assertEqual(prc.ask_multichoice("p", self._opts()), ["b"])


class TestParseArgs(unittest.TestCase):
    def test_flags(self):
        orig = sys.argv
        sys.argv = ["prog", "/lib", "--dry-run", "--export-only",
                    "--export-file", "/x.json", "--from-mapping", "/m.json"]
        try:
            args = pr.parse_args()
        finally:
            sys.argv = orig
        self.assertEqual(args.library, "/lib")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.export_only)
        self.assertEqual(args.export_file, "/x.json")
        self.assertEqual(args.from_mapping, "/m.json")

    def test_defaults(self):
        orig = sys.argv
        sys.argv = ["prog"]
        try:
            args = pr.parse_args()
        finally:
            sys.argv = orig
        self.assertIsNone(args.library)
        self.assertFalse(args.dry_run)


class TestMainSingleItemWarning(unittest.TestCase):
    def setUp(self):
        self._saved = (pr.read_mapping, pr.resolve_input_path, pr.apply_mapping)

    def tearDown(self):
        pr.read_mapping, pr.resolve_input_path, pr.apply_mapping = self._saved

    def _run(self, entries):
        import argparse
        pr.read_mapping = lambda p: entries
        pr.resolve_input_path = lambda *a, **k: "/some/dir"
        pr.apply_mapping = lambda *a, **k: None
        args = argparse.Namespace(library="/some/dir", dry_run=False,
                                  export_only=False, export_file=None,
                                  from_mapping="/m.json")
        buf = StringIO()
        with redirect_stdout(buf):
            pr.main(args)
        return buf.getvalue()

    def test_warns_on_single_item(self):
        out = self._run([{"old_path": "/a.mkv", "new_name": "a.mkv",
                          "media_type": "movie"}])
        self.assertIn("only ONE item", out)

    def test_no_warning_on_multiple(self):
        out = self._run([
            {"old_path": "/a.mkv", "new_name": "a.mkv", "media_type": "movie"},
            {"old_path": "/b.mkv", "new_name": "b.mkv", "media_type": "movie"},
        ])
        self.assertNotIn("only ONE item", out)


# --------------------------------------------------------------------------- #
# Regression tests for the fixes applied to the tools
# --------------------------------------------------------------------------- #
class TestSanitizeControlChars(unittest.TestCase):
    def test_strips_control_chars(self):
        self.assertEqual(prc.sanitize("a\x00b\x1fc"), "abc")

    def test_strips_invalid_and_control_together(self):
        self.assertEqual(prc.sanitize('Movie\t: 2000?'), "Movie 2000")


class TestAskYesNoDefaultSuffix(unittest.TestCase):
    def setUp(self):
        self._orig = prc.input if hasattr(prc, "input") else None

    def test_suffix_reflects_default(self):
        prompts = []
        import builtins
        orig_input = builtins.input
        builtins.input = lambda p="": (prompts.append(p), "y")[1]
        try:
            prc.ask_yes_no("Go?", default="y")
            prc.ask_yes_no("Go?", default="n")
        finally:
            builtins.input = orig_input
        self.assertIn("[Y/n]", prompts[0])
        self.assertIn("[y/N]", prompts[1])


class TestCleanPathInput(unittest.TestCase):
    def test_strips_backticks_and_quotes(self):
        self.assertEqual(prc.clean_path_input('  "`/tmp/x`"  '), "/tmp/x")

    def test_expands_user(self):
        self.assertEqual(prc.clean_path_input("~"), os.path.expanduser("~"))


class TestJellyfinTargetLongSeasons(unittest.TestCase):
    def test_three_digit_episode(self):
        self.assertEqual(
            pr.jellyfin_target("Show (2000) - S01E150 - T.mkv", "tv", "/lib"),
            os.path.join("/lib", "Show (2000)", "Season 01",
                         "Show (2000) - S01E150 - T.mkv"))


class TestSafeMove(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_creates_dirs_and_moves(self):
        src = os.path.join(self.d, "src.mkv")
        open(src, "w").close()
        dst = os.path.join(self.d, "new", "dst.mkv")
        with redirect_stdout(StringIO()):
            err = pr.safe_move(src, dst)
        self.assertIsNone(err)  # None means success
        self.assertTrue(os.path.exists(dst))

    def test_failure_returns_error(self):
        with redirect_stdout(StringIO()):
            err = pr.safe_move(os.path.join(self.d, "ghost.mkv"),
                               os.path.join(self.d, "out.mkv"))
        self.assertIsNotNone(err)
        self.assertIn("ghost.mkv", err)


class TestAnalyzeOutliersBulk(unittest.TestCase):
    def _items(self):
        return [{"levels": 1, "current_path": "/a", "leave_alone": False},
                {"levels": 1, "current_path": "/b", "leave_alone": False},
                {"levels": 0, "current_path": "/c", "leave_alone": False},
                {"levels": 0, "current_path": "/d", "leave_alone": False}]

    def test_bulk_fix_all(self):
        items = self._items()
        orig = pr.ask_yes_no
        pr.ask_yes_no = lambda *a, **k: True  # yes to "bring all into line"
        try:
            with redirect_stdout(StringIO()):
                pr.analyze_and_handle_outliers(items)
        finally:
            pr.ask_yes_no = orig
        self.assertFalse(any(it["leave_alone"] for it in items))

    def test_bulk_leave_all(self):
        items = self._items()
        # First bulk prompt: no; second bulk prompt ("leave all alone"): yes.
        answers = iter([False, True])
        orig = pr.ask_yes_no
        pr.ask_yes_no = lambda *a, **k: next(answers)
        try:
            with redirect_stdout(StringIO()):
                pr.analyze_and_handle_outliers(items)
        finally:
            pr.ask_yes_no = orig
        self.assertTrue(items[2]["leave_alone"])
        self.assertTrue(items[3]["leave_alone"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
