#!/usr/bin/env python3
"""
Phase 1 onboarding: connect to a Plex server (three entry points) and pick a
library. plexapi is imported lazily inside the functions that need it so the
rest of the tool -- and the test suite -- never depends on it being installed.

Prompt helpers are referenced through the `common` module (common.ask, ...)
rather than imported by name, so a test can patch a single point
(plexrename.common.ask) and have every caller here see it.
"""

import re
import getpass

from plexrename import common


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
        url = common.ask("\nPaste the View XML URL (blank to go back): ")
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
    baseurl = common.ask("Plex server URL (e.g. http://127.0.0.1:32400): ")
    token = common.ask("Plex token: ")
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

    username = common.ask("Plex.tv username or email (blank to go back): ")
    if not username:
        return None
    password = getpass.getpass("Plex.tv password: ")
    code = common.ask("Two-factor code (blank if none): ")
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
        choice = common.ask("Choose a server by number (blank to go back): ")
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
        method = common.ask_choice(
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
        choice = common.ask("\nType the number of the library: ")
        if choice.isdigit() and 0 <= int(choice) < len(sections):
            return sections[int(choice)]
        print("That isn't one of the numbers above. Try again.")
