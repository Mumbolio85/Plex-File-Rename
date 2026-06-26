# Plex -> Jellyfin Rename Tool

[![CI](https://github.com/Mumbolio85/Plex-File-Rename/actions/workflows/tests.yml/badge.svg)](https://github.com/Mumbolio85/Plex-File-Rename/actions)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Renames your messy media files to clean, standard names using the correct**
**titles and years from Plex -- so your library imports perfectly into Jellyfin**
(or any other media player).

It takes files like this:

```
   Before                              After
   ----------------------------        ----------------------------
   the.matrix.1999.1080p.x264.mkv  ->  The Matrix (1999).mkv
   inception[2010]BRRip.mkv        ->  Inception (2010).mkv
   got.s01e01.web-dl.mkv           ->  Game of Thrones (2011) - S01E01 - Winter Is Coming.mkv
```

Plex already knows the real title, year, and episode info for every file. This
tool reads that information and uses it to rename the **actual files on your**
**computer** to match.

> **Is it safe?**
> **Yes.** Nothing on your disk is changed until you review a full list of every
> rename and type **`yes`** to approve it. Every change is also saved to an
> "undo" file, so any run can be completely reversed. You can even do a **trial**
> **run** that shows you everything it *would* do without touching a single file.

---

## Quick start

```
pip install plexapi
python3 plex_rename.py --dry-run     # preview everything, change nothing
python3 plex_rename.py               # do it for real (asks before any change)
```

That's the whole loop: dry-run first, look at the plan, then run it for real.
Everything below is detail.

> 💡 **Prefer a proper install?** Run `pip install .` in this folder instead.
> It pulls in `plexapi` for you and adds two commands you can run from anywhere —
> `plex-rename` and `plex-undo-rename` — so you can skip the `python3 …` prefix.

## Contents

- [Before you start: what you'll need](#before-you-start-what-youll-need)
- [Step-by-step walkthrough](#step-by-step-walkthrough)
- [Ways to connect to Plex](#ways-to-connect-to-plex)
- [Command-line options](#command-line-options)
- [The optional Jellyfin restructure](#the-optional-jellyfin-restructure)
- [Step 7 (optional) — Migrate watched-state into Jellyfin](#step-7-optional--migrate-watched-state-into-jellyfin)
- [Step 8 (optional) — Copy Plex artwork into Jellyfin](#step-8-optional--copy-plex-artwork-into-jellyfin)
- [Safety features](#safety-features)
- [Undoing a run](#undoing-a-run)
- [Development](#development) — running the tests
- [Troubleshooting](#troubleshooting)
- [How it compares to other tools](#how-it-compares-to-other-tools)
- [Under the Hood](#under-the-hood)

---

## What's in this folder

| File | What it's for |
| --- | --- |
| **`plex_rename.py`** | **The main tool.** This is the one you run (or use the `plex-rename` command after `pip install .`). |
| `plex_undo_rename.py` | Reverses a previous run if you change your mind (`plex-undo-rename`). |
| `plexrename/` | The package the two launchers above run: the real code, split into focused modules (`common`, `models`, `naming`, `connect`, `apply`, `jellyfin`, `artwork`, `undo`, `options`, `cli`). You never run these directly. |
| `pyproject.toml` | Packaging metadata: the `plexapi` dependency and the `plex-rename` / `plex-undo-rename` console commands. |
| `LICENSE` | MIT license. |
| `tests/` | Automated tests. Not needed for normal use — see [Development](#development). |

> The two `plex_*.py` files in the root are thin launchers; everything they do
> lives in the `plexrename` package.

---

## Before you start: what you'll need

1. **Python 3** installed (version 3.12 or newer). To check, open a terminal and
   type `python3 --version`.
2. **The `plexapi` package.** Install it once by running:

   ```
   pip install plexapi
   ```

3. **Access to your Plex server** (you'll connect to it in a moment), and
4. **The folder on your computer where the media files actually live** -- for
   example `/Volumes/Media/Movies` (Mac) or `D:\Media\Movies` (Windows). If
   your files are on a NAS or network drive, make sure it's connected first.

---

## Step-by-step walkthrough

Follow these steps in order. The tool guides you through each one and won't
change anything until the very end.

### Step 1 — Start the tool

Open a terminal, go to this folder, and run:

```
python3 plex_rename.py
```

> 💡 **First time? Do a trial run first.** Add `--dry-run` to the end
> (`python3 plex_rename.py --dry-run`). It walks through every step exactly the
> same way but only *shows* you what it would do -- no files are changed. Once
> you're happy with the preview, run it again without `--dry-run`.

### Step 2 — Connect to your Plex server

You'll be asked how you want to connect. The **easiest** way:

1. Open the Plex web app in your browser.
2. Hover over any movie or show, click the **`...`** (three dots) button.
3. Choose **Get Info**, then click **View XML**.
4. A new browser tab opens with a long web address (URL). **Copy that entire**
   **URL** and paste it into the tool when asked.

The tool reads your server address and login token straight from that URL -- you
don't have to find them yourself. (Other connection options are listed in
[Ways to connect to Plex](#ways-to-connect-to-plex) below.)

### Step 3 — Choose which library to rename

The tool shows a numbered list of your Plex libraries (Movies, TV Shows, etc.).
Type the number of the one you want and press Enter.

### Step 4 — Point it at your files on this computer

The tool now knows the *correct* names, but it needs to find the matching files
**on your own computer**. It asks:

```
Folder on this computer that contains your media files:
```

Enter the folder you'd open in Finder/Explorer to see the actual video files
(e.g. `/Volumes/Media/Movies` or `D:\Media\Movies`). The tool then reports how
many files it successfully matched -- for example *"Matched 248 of 250 files."*

### Step 5 — Review the plan and approve it

This is the important step. The tool prints a complete list of every rename it
wants to make, shown as `current name -> new name`. Read it over. When you're
satisfied, type **`yes`** to apply the changes. Type anything else to cancel and
nothing happens.

```
Rename plan (3 items):
  the.matrix.1999.1080p.x264.mkv  ->  The Matrix (1999).mkv
  inception[2010]BRRip.mkv        ->  Inception (2010).mkv
  got.s01e01.web-dl.mkv           ->  Game of Thrones (2011) - S01E01 - Winter Is Coming.mkv

Apply these 3 video(s)? [y/N]: yes
  Renaming 3 of 3...
Done.
```

### Step 6 (optional) — Organize into Jellyfin folders

After renaming, the tool offers to also tidy everything into Jellyfin's
recommended folder layout (e.g. each movie in its own `Heat (1995)/` folder).
This is optional and asks for its own separate `yes`. You can skip it.

### Steps 7 & 8 (optional) — Bring your watched-state and artwork across

If you restructured into Jellyfin's layout, the tool can also carry over two
things that moving files alone doesn't: your **watched-state** (what's
watched, play counts, resume positions, ratings) and your **artwork** (the
posters/fanart you set in Plex). Each is optional, asks for its own `yes`, and
is covered in detail in
[Step 7](#step-7-optional--migrate-watched-state-into-jellyfin) and
[Step 8](#step-8-optional--copy-plex-artwork-into-jellyfin) below.

### Done!

That's it -- your files are renamed. The tool then offers to process
**another library** on the same Plex server without reconnecting -- handy if you
keep Movies and TV Shows in separate libraries. If anything was skipped, or if
you want to undo the whole thing, see [Undoing a run](#undoing-a-run) below.

---

## Ways to connect to Plex

When you start, you'll be offered three options:

1. **Paste a "View XML" URL** *(easiest)* -- the method described in
   [Step 2](#step-2--connect-to-your-plex-server) above.
2. **Enter the server address and token separately** -- e.g. server
   `http://127.0.0.1:32400` and your Plex token.
3. **Log in with your Plex account** -- enter your plex.tv username/password
   (and 2-factor code if you use one); the tool auto-discovers the servers on
   your account and lets you pick one.

---

## Command-line options

You can run the tool with no arguments at all (it will prompt you for
everything), or use these flags:

```
python3 plex_rename.py [library_folder] [options]
```

| Option | What it does |
| --- | --- |
| `library_folder` | The local path to your library folder. If you leave it off, you'll be asked for it. |
| `--dry-run` | **Preview mode.** Shows every change it would make and touches nothing. Highly recommended for the first run. |
| `--export-only` | Phase 1 only: connect to Plex, build the mapping, save it to a file, and stop. Doesn't rename anything. |
| `--export-file PATH` | Save the Plex mapping to this JSON file (otherwise you're asked whether to save it). |
| `--from-mapping PATH` | Skip Plex entirely and apply a mapping JSON file you exported earlier. |
| `--log-dir PATH` | Where to write the undo/skip logs (default: `~/Downloads`). |
| `--migrate-watched` | **Step 7 (standalone).** Migrate Plex watched-state into Jellyfin from a post-restructure mapping. Use with `--from-mapping`. See [Step 7](#step-7-optional--migrate-watched-state-into-jellyfin). |
| `--copy-artwork` | **Step 8 (standalone).** Copy Plex artwork (poster/fanart) into the media folders from a post-restructure mapping. Use with `--from-mapping`. See [Step 8](#step-8-optional--copy-plex-artwork-into-jellyfin). |
| `--skip-step8` | Don't offer step 8 (the artwork copy) after organizing. |
| `--force` | With `--migrate-watched`, re-add play counts to items already migrated (otherwise they're skipped to avoid double-counting). |
| `--yes`, `-y` | Skip the per-step confirmation prompts (the plan is still printed). Intended for repeat runs after you've previewed with `--dry-run`; a single "are you sure?" gate still guards a non-dry run. |
| `--version` | Print the tool's version and exit. |

> 💡 Flags can go in any order after the script name; the optional
> `library_folder` is the one positional argument (e.g.
> `python3 plex_rename.py /Volumes/Media/Movies --dry-run`).

If you run with **no flags**, the tool also offers an interactive settings menu
where you can toggle these same options before it starts.

After it finishes a library, the tool offers to process **another library** on
the same Plex server without reconnecting.

---

## The optional Jellyfin restructure

After renaming, the tool offers to **restructure** your files into Jellyfin's
recommended folder layout:

- **Movies** -> `Library/Title (Year)/Title (Year).ext`
- **TV Shows** -> `Library/Series (Year)/Season 01/episode.ext`

This is optional and separately confirmed -- you can rename without
restructuring, or do both.

---

## Step 7 (optional) — Migrate watched-state into Jellyfin

Steps 1–6 only move *files*. They never carry your Plex **user data**: what's
watched/unwatched, play counts, resume positions, and your personal ratings.
**Step 7** copies that into Jellyfin over its REST API (so it works on recent
Jellyfin versions — tested against 10.10/10.11.x — and any database backend,
with the server left running).

It matches each Plex item to its Jellyfin counterpart differently depending on
how you run it:

- **Inline** (right after the restructure, same machine): by the file's final
  path first, falling back to the filename, then IMDb/TMDb/TVDB provider IDs.
- **Standalone** (`--migrate-watched`, possibly a different machine): by
  **provider IDs first**, falling back to the **filename** — because a path
  recorded on one machine usually won't line up with the Jellyfin server's.

> **Important — let Jellyfin scan first.** Jellyfin can only hold watched-state
> for files it has already *scanned in*. Since step 6 just moved the files, run a
> Jellyfin library scan (Dashboard → Scan All Libraries) and let it finish before
> migrating. The tool reminds you and waits.

**Two ways to run it:**

1. **Inline**, right after the restructure — enable it from the interactive
   settings menu (or `--migrate-watched`). The tool reminds you to scan, waits,
   then migrates.
2. **Standalone**, later — re-run with any saved v2.0 mapping:

   ```
   python3 plex_rename.py --migrate-watched --from-mapping ~/Downloads/plex_rename_applied_<timestamp>.json
   ```

   A `plex_rename_applied_*.json` is written automatically when a restructure
   runs, but any mapping exported with this version works too — they all carry
   the captured watched-state plus the provider IDs and filename this mode
   matches on.

**How conflicts are resolved (merge, never regress):**

- **Watched** — stays watched if Jellyfin already had it watched; otherwise set
  from Plex. Never un-watches.
- **Play count** — Plex's count is **added** to Jellyfin's. To avoid
  double-counting, each migrated item is logged; re-running won't add again
  unless you pass `--force`.
- **Resume position** — the larger of the two offsets wins.
- **User rating** — Plex's rating is applied only if it wouldn't lower Jellyfin's.

> Favorites are **not** migrated: Plex has no native per-item favorite flag, so
> there's nothing reliable to carry across.

**Connecting to Jellyfin:** when you run Step 7 you'll be offered two options:

1. **Server URL + API key** *(recommended)* — enter your Jellyfin server
   address (e.g. `http://192.168.1.100:8096`) and an API key. To make one:
   log in to Jellyfin → **Dashboard** → **API Keys** → **+** → give it a name
   → copy the key.
2. **Username and password** — enter your Jellyfin server address, then your
   Jellyfin username and password. The tool exchanges these for a session token.

If your account has access to multiple Jellyfin users, the tool will ask you
which user to migrate watched-state into.

Step 7 needs network access to your Jellyfin server, but **no extra Python package** (it uses only the standard library).

Every watched-state write is recorded in the same undo log as the file moves, so
`plex_undo_rename.py` can put the prior values back (it connects to Jellyfin only
when it encounters such a record).

---

## Step 8 (optional) — Copy Plex artwork into Jellyfin

Moving files doesn't bring over the **posters and fanart** you picked in Plex.
**Step 8** downloads the artwork Plex has for each item and drops it into the
media folders as standard image files, so Jellyfin's image scanner picks them
up on its next scan — no need to enable "Save metadata to media folders" in
Jellyfin. It uses only the Python standard library (no extra package).

Where the images land:

- **Movies** -> `{Title (Year)}-folder.jpg` (poster) and
  `{Title (Year)}-backdrop.jpg` (fanart), alongside the movie file.
- **TV Shows** -> `poster.jpg` and `fanart.jpg` inside the series folder.

**When it's offered.** Step 8 only runs once **Step 7** has been run (this
session or a previous one) — that's how it knows the files are already in
Jellyfin's layout. It asks whether to copy artwork at all, then whether to:

- **Skip images Jellyfin already placed** *(safe default)* — leaves any artwork
  you've already set in Jellyfin untouched, or
- **Overwrite with the Plex versions** — replaces existing images with Plex's.

**Two ways to run it:**

1. **Inline**, right after the restructure and watched-state migration — the
   tool offers it automatically (skip with `--skip-step8`).
2. **Standalone**, later — re-run with a saved v2.1 mapping:

   ```
   python3 plex_rename.py --copy-artwork --from-mapping ~/Downloads/plex_rename_applied_<timestamp>.json
   ```

   The mapping must carry artwork URLs (any mapping exported with v2.1+ does),
   and your Plex server must still be reachable at the same address with a valid
   token — the artwork URLs captured during Phase 1 embed that token.

Each **newly downloaded** image is recorded in the undo log, so
`plex_undo_rename.py` removes it on undo. Images you chose to *overwrite* are
**not** reversible — the previous file's contents are gone — so the safe default
leaves existing artwork alone.

---

## Safety features

- **Nothing changes without confirmation.** You always see the full plan and
  must type `yes`.
- **`--dry-run`** lets you preview everything, risk-free.
- **Undo log.** Every move is recorded to a file named
  `plex_rename_undo_<timestamp>.txt` in your `~/Downloads` folder (or wherever
  `--log-dir` points).
- **Skip log.** Anything skipped or failed is recorded to a separate log in the
  same folder, so you know exactly what didn't get touched and why.
- **Resilient moves.** A move that hits a transient error (e.g. a brief network
  hiccup on a NAS) is retried once; anything that still fails is skipped and
  logged rather than aborting the whole run.
- **Sidecars stay paired.** Subtitles, `.nfo` metadata, and sidecar artwork
  files already on disk (e.g. `-poster.jpg`) are moved alongside their video,
  never orphaned. (Downloading Plex artwork from scratch is a separate optional
  step — see [Step 8](#step-8-optional--copy-plex-artwork-into-jellyfin).)

---

## Undoing a run

Every apply writes an undo log to `~/Downloads`. To reverse a run:

```
python3 plex_undo_rename.py
```

It will ask for the path to the undo log (or pass it directly:
`python3 plex_undo_rename.py ~/Downloads/plex_rename_undo_20260615_173000.txt`).
As with the main tool, it shows a full plan, changes nothing until you confirm,
and supports `--dry-run`.

---

## Development

The code lives in the `plexrename` package; the root `plex_*.py` files are thin
launchers. The test suite needs **no live Plex/Jellyfin server and no third-party packages** (`plexapi` is imported lazily, so the logic is exercised with fake objects). Run everything with:

```
python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

The same command runs in CI (GitHub Actions, Python 3.12) on every push and pull
request. To work on the tool as an installed package:

```
pip install -e .          # editable install; provides plex-rename / plex-undo-rename
```

---

## Troubleshooting

**"Matched 0 of N files" (or a very low match count)**
The local folder you entered doesn't line up with where the files actually are.
Double-check the path — it should be the folder you'd open in Finder/Explorer
to see the video files directly. If your library is on a NAS or external drive,
make sure it's mounted first.

**"Cannot connect to server" / token errors**
Your Plex token may have expired or the server address has changed. Grab a fresh
URL by going to Plex web, clicking `...` on any item → **Get Info** → **View XML**, and paste the new URL when the tool asks.

**Single-item library warning**
If your library has only one item, the tool warns you that it can't reliably
infer the folder structure from a single path. It's safe to proceed — just
confirm the proposed rename looks correct before typing `yes`.

**Files renamed but Jellyfin still shows old names**
Jellyfin won't pick up the changes until it scans. Go to **Dashboard → Scan All Libraries** and wait for it to finish. If Step 7 (watched-state) or Step 8
(artwork) didn't run yet, do that scan before running either of those steps.

**`--copy-artwork` says "no migration log found"**
Step 8 requires Step 7 to have been run first. Run `--migrate-watched` with the
same mapping file, then retry `--copy-artwork`.

---

## How it compares to other tools

Most existing tools do **one** of the jobs this tool does. What makes this one
different is that it fuses the whole "switch from Plex to Jellyfin" workflow into
a single guided, reversible run: **rename → restructure → carry over
watched-state → carry over artwork**. Two families of tool overlap with parts of
it.

### As a metadata-driven renamer

| Tool | Names come from | Scope | Notes |
| --- | --- | --- | --- |
| **This tool** | **Your existing Plex library** | Movies + TV | Trusts what Plex already matched — no re-scraping, in-place rename, full undo |
| [FileBot](https://www.filebot.net/) | Online DBs (TMDB/TVDB/AniDB) | Movies, TV, anime, music | The most powerful/flexible option; paid; re-identifies every file from scratch |
| [tinyMediaManager](https://www.tinymediamanager.org/) | Online DBs + regex parsing | Movies + TV | Free, open-source, GUI, writes NFOs; steeper learning curve |
| [perplex](https://github.com/rieck/perplex) | Plex metadata | Movies only | Same Plex-driven idea, but movies-only and **copies** files to a new folder |
| [plex-renamer](https://github.com/Will-Bo/plex-renamer) | Filename parsing | TV | Lightweight, no server connection |
| [PlexifyFiles](https://github.com/patrickenfuego/PlexifyFiles) | Filename parsing | Movies + TV | PowerShell, cross-platform |

The signature here is reading names from the library **Plex already curated**,
rather than re-identifying each file. FileBot and tinyMediaManager are more
flexible (and can rename media that was never in Plex), but they re-match every
file and can get it wrong. Only `perplex` shares the Plex-driven approach, and
it's movies-only and copy-based rather than an in-place rename with undo.

### As a Plex → Jellyfin watched-state migrator

| Tool | Direction | Matching | Notes |
| --- | --- | --- | --- |
| **This tool** | Plex → Jellyfin | Provider IDs → filename → path | Merge-never-regress; per-item dedup log; **writes are reversible** via the undo log |
| [JellyPlex-Watched](https://github.com/luigi311/JellyPlex-Watched) | Plex ↔ Jellyfin ↔ Emby | Filenames + provider IDs | The most mature option: continuous **two-way** sync, multi-user, Docker |
| [migrate-plex-to-jellyfin](https://github.com/wilmardo/migrate-plex-to-jellyfin) | Plex → Jellyfin | Filename only | One-shot CLI |
| [plex-jellyfin-sync](https://github.com/Linkek/plex-jellyfin-sync) | Plex → Jellyfin | IMDb IDs | Watched flag focus |
| [Watchstate](https://github.com/arabcoders/watchstate) | Multi-server | DB intermediary | Powerful, more setup |

For ongoing, bidirectional, multi-user sync, **JellyPlex-Watched** is
purpose-built and more capable. This tool's watched-state migration is instead
deliberately careful and reversible: it **merges** rather than overwrites (adds
play counts with a dedup log so re-runs don't double-count, keeps the larger
resume position, never un-watches, only raises ratings), and every write lands
in the **same undo log** as the file moves — so the whole migration can be
reversed.

### Where this tool stands out

- **End-to-end in one pass.** Nothing else does rename **and** Jellyfin
  restructure **and** watched-state **and** artwork in a single workflow —
  normally you'd stitch together a renamer + a watched-state syncer + a manual
  artwork copy.
- **Plex is the single source of truth** for both filenames and user data — no
  re-scraping, so it inherits the matches you already curated in Plex.
- **Reversible across the board.** File moves, watched-state writes, and
  newly-downloaded artwork all go into one undo log. Most watched-state
  migrators have no undo at all.
- **Cautious by default.** Dry-run, full plan + typed `yes`, skip log,
  retry-once on transient NAS errors, and sidecars (subs/`.nfo`/artwork) that
  travel with their video.

### Honest gaps

- **No GUI** — it's CLI-only (FileBot and tinyMediaManager have polished
  interfaces).
- **Movies + TV only** — no anime or music handling (a FileBot strength).
- **Watched-state is one-way and one-shot** — not a continuous multi-server sync
  (a JellyPlex-Watched strength).
- **No online re-scraping or NFO writing** — it can't fix metadata Plex got
  wrong, and it doesn't generate Kodi/Jellyfin NFOs (a tinyMediaManager
  strength).
- **Needs a live Plex server** as the data source — it's no help if Plex is
  already gone.

In short: as a *renamer* it overlaps with FileBot/tinyMediaManager (its angle:
trust Plex's metadata instead of re-scraping); as a *migrator* it overlaps with
JellyPlex-Watched and friends (its angle: merge safely, with undo). What
essentially nothing else does is combine both halves into one reversible,
dry-runnable workflow built for someone moving from Plex to Jellyfin.

---

＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

<a id="under-the-hood"></a>

# !!!  Under the Hood --- How and Why the Tools Work  !!!

> **Everything below this line is optional reading.** It's for the curious or
> for anyone modifying the scripts. You do **not** need any of it for normal
> use -- the sections above cover everything required to run the tools.

＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

### Why two phases, and why a JSON mapping?

Plex knows the *correct* title, year, season, and episode for everything --
that's the metadata you want in your filenames. But Plex stores files under
*server-side* paths, which may not match where the files live on the machine
you're running this from (different drive letters, a mounted network share,
etc.).

So Phase 1 captures Plex's knowledge as a list of
`old_path -> new_name` entries (plus all the extra metadata Plex reported), and
Phase 2 *remaps* those server-side paths onto your **local** folder by:

1. Finding the common root of all the recorded paths (`detect_recorded_root`).
2. Taking each file's path *relative* to that root.
3. Re-rooting it under the local library folder you supply.

This is why a library with **only one item** triggers a warning -- with a single
path there's nothing to compare against, so the structure can't be inferred
reliably.

The optional **JSON mapping file** is just that list of entries written to disk.
It's handy as a reviewable, resumable artifact (you can apply it later with
`--from-mapping`), and it captures *every* scalar field plexapi reported for
each item, media version, and file part under a `plex` key. JSON is used instead
of a delimited text format because it survives any character that might appear
in a path or title.

### How names are built

- **Movies:** `Title (Year).ext`. If a movie has multiple versions (e.g. a 3D
  cut and a regular cut), an **edition label** is appended:
  `Title (Year) - [IMAX].ext`. Multi-file movies get `- part1`, `- part2`, etc.
- **TV:** `Series (Year) - S01E02 - Episode Title.ext`.
- **Editions / disambiguation:** When two different files would end up with the
  *same* name, the tool appends a distinguishing tag, in order of preference:
  Plex's edition title -> a marker found in the path (`3D`, `IMAX`, `Director's
  Cut`, `Angle 2`, resolution, …) -> finally a generic `version N`. The marker
  list (`EDITION_MARKERS`) is ordered so longer, more specific tags win over
  shorter ones they contain (`Extended Cut` before `Extended`, `HDR10` before
  `HDR`). Multi-angle discs are matched flexibly (`angle1`, `Angle 2`,
  `angle-3`) and normalized to `Angle N`.
- **Sanitizing:** Characters illegal in filenames (`<>:"/\|?*`) and ASCII
  control characters (newlines, tabs, etc.) are stripped from any name the tool
  generates -- but only from path components *below* your library root, so the
  root path you typed is never altered.

### Sidecar handling

A "sidecar" is a file that belongs to a video and must travel with it: subtitles
(`.srt`, `.ass`, `.vtt`, …), `.nfo` metadata, and artwork following the
`<name>-poster.jpg` / `-fanart` / `-thumb` convention. The tool:

1. Scans for sidecars **once**, at each video's original on-disk location, by
   matching files that share the video's stem followed by a `.` or `-` boundary
   (so `Movie 2.mkv` is never mistaken for a sidecar of `Movie.mkv`).
2. Stores just the *remainder* of each name (e.g. `.en.srt`, `-poster.jpg`).
3. Projects those remainders onto each move so the sidecar lands next to the
   renamed video. Because both sides are derived from the stems rather than a
   fresh disk scan, the real and dry-run paths behave identically -- which is how
   a dry-run preview can correctly list sidecars for a restructure that follows
   a rename that hasn't actually happened yet.

### Structure detection & outliers

Before renaming, the tool counts how many folders deep each item sits (loose in
the root = 0, its own folder = 1, `Show/Season` = 2) and treats the **majority**
pattern as the norm. Items that don't match are flagged as **outliers**. When
several are flagged, the tool first offers to bring them **all** into line or
leave them **all** alone in one step; otherwise (or if you decline both) you
decide per-item whether to bring each into line or **leave it completely alone**
(skipped, never renamed). Anything nested deeper than the majority is kept where
it is rather than guessing at a reconstruction.

### The undo log format

Each applied move is appended to the undo log as
`<new path> ––––– <original path>`, flushed immediately so a crash mid-run still
leaves a usable log. Empty folders removed during cleanup are recorded with a
`[[MKDIR]]` sentinel so `plex_undo_rename.py` can recreate them. Watched-state
writes (step 7) are recorded with a `[[USERDATA]]` sentinel that carries the
prior Jellyfin UserData, so `plex_undo_rename.py` can restore it. The undo tool
reads the log in **reverse** order (last change undone first) and, like the main
tool, refuses to overwrite a file that already exists at the destination.

---

### `plexrename/common.py` -- the shared helpers

This module exists so the apply step and the undo step can never disagree about
the file formats and behaviors they share. Keeping them in one place guarantees
both tools use the same:

- **`sanitize()`** and the `INVALID_CHARS` set -- identical filename cleaning.
- **`SEP`** (the ` ––––– ` separator written into the undo log) and **`SEP_RE`**
  (a tolerant regex that reads it back, accepting any dash type/count and
  whitespace so hand-edited logs still parse).
- **`MKDIR_SENTINEL`** -- the `[[MKDIR]]` marker for recreating removed folders.
- **`USERDATA_SENTINEL`** -- the `[[USERDATA]]` marker for recording (and reversing) Jellyfin watched-state writes.
- **`DOWNLOADS`** -- the `~/Downloads` location where logs are written.
- **`RunLog`** -- records skipped/failed items to the screen and, lazily, to a
  file that's only created if something is actually skipped.
- The interactive prompt helpers -- **`ask`**, **`ask_path`**, **`ask_yes_no`**
  (which shows whether Enter means yes or no via `[Y/n]`/`[y/N]`), **`ask_choice`**,
  and **`ask_multichoice`** (pick several options by number) -- so both tools
  prompt consistently.
- **`clean_path_input()`** -- normalises a pasted path by trimming surrounding
  quotes/backticks and expanding a leading `~`, shared by every path prompt.
- **`cleanup_empty_dirs()`** -- removes folders left empty by the moves
  (bottom-up; a lone `.DS_Store` counts as empty; the root is never removed; on
  undo, just-recreated folders are preserved). When given an undo log it records
  each removal so it can be reversed.

---

### `tests/` -- the test suite

A comprehensive `unittest` suite (run it with `python3 -m unittest discover -s tests -t . -p 'test_*.py'`)
that requires **no live Plex server**. Because `plexapi` is only imported
*inside* functions, the tests feed in lightweight **fake** Plex objects
(`FakeMovie`, `FakeShow`, `FakeEpisode`, `FakeMedia`, `FakePart`, …) to exercise
the scanning logic without a network connection. Coverage includes:

- **Pure helpers** -- URL/token extraction, name building, edition-marker
  matching, path remapping, Jellyfin target paths.
- **Filesystem operations** -- sidecar detection, sanitizing, plan building, and
  actual move execution, all run inside temporary directories that are cleaned
  up afterward.
- **JSON round-trips** -- writing a mapping and reading it back, including the
  handling of malformed, partial, or non-list files.
- **Plan execution edge cases** -- dry-run changes nothing, existing targets are
  skipped, a sidecar moving must not falsely "advance" a video whose own move
  was skipped, etc.
- **`apply_mapping` integration** -- full rename-then-restructure runs with the
  interactive prompts patched to scripted answers.
- **CLI / onboarding** -- argument parsing, the interactive settings picker, and
  the single-item warning.

`test_plex_jellyfin_userdata.py` covers the step-7 migration logic using a
`FakeJellyfinClient` (no live server needed):

- **`merge_userdata` conflict rules** -- watched/play-count/resume/rating merge
  semantics, double-count prevention, and the `--force` override.
- **Full `migrate_watched` runs** -- path-first and provider-ID-first matching,
  dry-run, already-migrated skipping, and undo-log recording.

The interactive prompts (`ask_yes_no`, `ask_choice`, etc.) are temporarily
swapped out for canned responses during tests, and standard output is captured,
so the suite runs fully unattended.
