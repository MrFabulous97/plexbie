# path: tests/test_efficiency.py
"""Wasted work, not wrong answers.

None of the defects covered here produced an incorrect result - which is why they
survived five correctness passes. They each did the right thing far more often, or
far more expensively, than necessary:

  * every stats-message update issued a GET whose body was never read, so the
    bot's own rate-limit monitor showed identical counts on the two routes
    (98 GET / 98 PATCH in a live sample) - half of its Discord traffic;
  * the auto-link loop fetched the whole Tautulli user table every five minutes
    even when every invite it held was already linked;
  * the alias file was opened, read and JSON-parsed once per streaming user on
    every tick of a ten-second loop, synchronously, on the event loop;
  * hint files that expire after fourteen days were swept every ten seconds;
  * the cleanup scan asked Plex for episodes one season at a time.

Each test below pins the cheap shape in place.
"""
import ast
import asyncio
import inspect
import pathlib
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)


def _source_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(skip in rel for skip in ("sync-conflict", ".bak", "tests/", "backups/")):
            continue
        yield rel, path.read_text()


# ===================================================================
# 1. A message is edited, never fetched first
# ===================================================================

#: The only place a real fetch is justified: _edit_episode_message reads the
#: existing embed's fields to merge new episodes into them, and PartialMessage
#: carries no .embeds. Everything else only ever calls .edit(), for which a
#: partial message is enough - and which still raises NotFound, so the
#: create-a-new-one fallbacks keep working.
FETCH_MESSAGE_ALLOWED = {
    "plugins/new_media_added/cog.py",
}


def _fetch_message_sites():
    for rel, text in _source_files():
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fetch_message"
            ):
                yield rel, node.lineno


def test_fetch_message_is_only_used_where_the_body_is_read():
    """A fetch whose result is only edited is a wasted round-trip.

    Adding a file here must be a conscious decision: it means the code genuinely
    reads the fetched message, not merely overwrites it.
    """
    offenders = sorted(
        f"{rel}:{line}"
        for rel, line in _fetch_message_sites()
        if rel not in FETCH_MESSAGE_ALLOWED
    )
    assert offenders == [], (
        "these fetch a message only to edit it; use "
        "channel.get_partial_message(id).edit(...) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_three_stats_displays_use_partial_messages():
    """The hot path: one 10-second loop and two 5-minute loops."""
    text = (ROOT / "plugins/watch_tracking/cog.py").read_text()
    partials = [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_partial_message"
    ]
    assert len(partials) == 3, (
        f"expected now-watching, leaderboard and streaks to all use a partial "
        f"message; found {len(partials)}"
    )


def test_now_watching_tick_issues_no_message_fetch():
    """Behavioural: drive one tick and count the fetches."""
    from plugins.watch_tracking.cog import WatchTrackingCog

    calls = {"fetch": 0, "partial": 0, "edit": 0}

    class FakePartial:
        async def edit(self, **kwargs):
            calls["edit"] += 1

    class FakeChannel:
        def get_partial_message(self, message_id):
            calls["partial"] += 1
            return FakePartial()

        async def fetch_message(self, message_id):
            calls["fetch"] += 1
            return FakePartial()

    class FakePlex:
        def sessions(self):
            return []

    class FakeServices:
        plex_server = FakePlex()

    cog = object.__new__(WatchTrackingCog)
    cog.services = FakeServices()
    cog.stats_channel = FakeChannel()
    cog.now_watching_message_id = 123
    cog._username_cache = {}
    cog._aliases_cache = {}
    cog._aliases_stamp = None

    async def _true():
        return True

    async def _noop():
        return None

    cog._ensure_channel = _true
    cog._refresh_username_cache = _noop

    asyncio.run(WatchTrackingCog.update_now_watching.coro(cog))

    assert calls["edit"] == 1, "the tick did not complete - the display would go stale"
    assert calls["partial"] == 1
    assert calls["fetch"] == 0, (
        "the loop still fetches the message it is about to overwrite; at one tick "
        "every 10 seconds that is ~8,600 pointless REST calls a day"
    )


# ===================================================================
# 2. No Tautulli round-trip when nothing is waiting to link
# ===================================================================

def _user_mgmt_cog(invites):
    from plugins.user_mgmt import cog as module

    calls = {"tautulli": 0}

    class FakeResponse:
        status = 500

        async def json(self):
            return {}

    class FakeCM:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *exc):
            return False

    class FakeTautulli:
        async def get(self, url, params=None, **kwargs):
            calls["tautulli"] += 1
            return FakeCM()

    class FakeApi:
        tautulli = FakeTautulli()

    class FakeConfig:
        tautulli_url = "http://tautulli.local"
        tautulli_token = "token"

    class FakeServices:
        config = FakeConfig()
        api = FakeApi()

    cog = object.__new__(module.UserMgmtCog)
    cog.services = FakeServices()
    cog.bot = None

    async def _kv_get_all(namespace):
        return invites

    original = module.kv_get_all
    module.kv_get_all = _kv_get_all
    return module, cog, calls, original


def test_fully_linked_invites_do_not_touch_tautulli():
    """The steady state: invites are never removed once they link."""
    invites = {
        "1": {"status": "linked", "email": "a@example.com"},
        "2": {"status": "linked", "email": "b@example.com"},
    }
    module, cog, calls, original = _user_mgmt_cog(invites)
    try:
        asyncio.run(module.UserMgmtCog.auto_link_users.coro(cog))
    finally:
        module.kv_get_all = original

    assert calls["tautulli"] == 0, (
        "the whole Tautulli user table was fetched to skip every row; at one pass "
        "every 5 minutes that is 288 no-op round-trips a day"
    )


def test_a_pending_invite_still_queries_tautulli():
    """The guard must not turn the feature off."""
    invites = {
        "1": {"status": "linked", "email": "a@example.com"},
        "2": {"status": "pending", "email": "b@example.com"},
    }
    module, cog, calls, original = _user_mgmt_cog(invites)
    try:
        asyncio.run(module.UserMgmtCog.auto_link_users.coro(cog))
    finally:
        module.kv_get_all = original

    assert calls["tautulli"] == 1, "a pending invite must still be looked up"


def test_invites_with_no_email_are_not_worth_a_request():
    """An invite with no email can never match, pending or not."""
    invites = {"1": {"status": "pending"}, "2": {"status": "pending", "email": ""}}
    module, cog, calls, original = _user_mgmt_cog(invites)
    try:
        asyncio.run(module.UserMgmtCog.auto_link_users.coro(cog))
    finally:
        module.kv_get_all = original

    assert calls["tautulli"] == 0


# ===================================================================
# 3. The alias file is read only when it changes
# ===================================================================

def _watch_tracking_cog():
    from plugins.watch_tracking.cog import WatchTrackingCog

    cog = object.__new__(WatchTrackingCog)
    cog._aliases_cache = {}
    cog._aliases_stamp = None
    return cog


def test_alias_lookups_reuse_one_parse():
    """_get_display_name runs once per streaming user on a 10-second loop."""
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "user_aliases.json"
        path.write_text('{"aliases": {"alt": "primary"}}')

        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = path
        try:
            cog = _watch_tracking_cog()
            first = cog._load_aliases()
            second = cog._load_aliases()
            assert first == {"alt": "primary"}
            assert second is first, (
                "the file was re-read and re-parsed; this is a synchronous read "
                "on the event loop, once per user, every 10 seconds"
            )
        finally:
            module.USER_ALIASES_FILE = original


def test_editing_the_alias_file_still_takes_effect():
    """Caching must not require a restart to pick up a change."""
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "user_aliases.json"
        path.write_text('{"aliases": {"alt": "primary"}}')

        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = path
        try:
            cog = _watch_tracking_cog()
            assert cog._load_aliases() == {"alt": "primary"}

            # A different size guarantees a new stamp regardless of clock
            # granularity, which is the point of keying on size as well as mtime.
            path.write_text('{"aliases": {"alt": "primary", "other": "second"}}')
            assert cog._load_aliases() == {"alt": "primary", "other": "second"}
        finally:
            module.USER_ALIASES_FILE = original


def test_a_missing_alias_file_is_not_an_error():
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = Path(tmp) / "absent.json"
        try:
            cog = _watch_tracking_cog()
            assert cog._load_aliases() == {}
            assert cog._resolve_alias("someone") == "someone"
        finally:
            module.USER_ALIASES_FILE = original


def test_a_corrupt_alias_file_is_retried_not_cached():
    """Caching a parse failure would hide a later repair until restart."""
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "user_aliases.json"
        path.write_text("{ not json")

        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = path
        try:
            cog = _watch_tracking_cog()
            assert cog._load_aliases() == {}
            assert cog._aliases_stamp is None, "a bad parse must not be stamped"

            path.write_text('{"aliases": {"alt": "primary"}}')
            assert cog._load_aliases() == {"alt": "primary"}
        finally:
            module.USER_ALIASES_FILE = original


# ===================================================================
# 5. Hint expiry is swept on the hint's timescale, not the loop's
# ===================================================================

def test_sweep_interval_is_far_longer_than_the_scan_interval():
    from plugins.bookshelf_processor.cog import (
        HINT_MAX_AGE_DAYS,
        HINT_SWEEP_INTERVAL_SECONDS,
    )

    assert HINT_SWEEP_INTERVAL_SECONDS >= 3600
    assert HINT_SWEEP_INTERVAL_SECONDS < HINT_MAX_AGE_DAYS * 86400, (
        "sweeping less often than the expiry would leave hints past their deadline"
    )


def _bookshelf_cog(tmp):
    from plugins.bookshelf_processor.cog import BookshelfProcessorCog

    audiobooks = Path(tmp) / "audiobooks"
    ebooks = Path(tmp) / "ebooks"
    audiobooks.mkdir()
    ebooks.mkdir()

    cog = object.__new__(BookshelfProcessorCog)
    cog.bot = None
    cog.services = None
    cog.settle_seconds = 120
    cog.pending = {}
    cog.failed = {}
    cog.audiobook_watch = audiobooks
    cog.ebook_watch = ebooks
    cog.audiobook_lib = Path(tmp) / "lib_a"
    cog.ebook_lib = Path(tmp) / "lib_e"
    cog.cache_dir = Path(tmp) / "cache"
    cog._last_hint_sweep = None
    return cog


def test_hints_are_not_swept_on_every_tick():
    from plugins.bookshelf_processor import cog as module
    from plugins.bookshelf_processor.cog import BookshelfProcessorCog

    swept = []

    def fake_expire(watch_dir):
        swept.append(watch_dir)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        cog = _bookshelf_cog(tmp)
        original = module._expire_stale_hints
        module._expire_stale_hints = fake_expire
        try:
            asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 2, "the first tick should sweep both watch dirs"

            for _ in range(5):
                asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 2, (
                f"swept {len(swept)} times across 6 ticks; at one tick every 10 "
                f"seconds that is ~17,000 directory scans a day to enforce a "
                f"14-day expiry"
            )
        finally:
            module._expire_stale_hints = original


def test_the_sweep_happens_again_once_the_interval_has_passed():
    """Throttling must not become never."""
    from plugins.bookshelf_processor import cog as module
    from plugins.bookshelf_processor.cog import (
        BookshelfProcessorCog,
        HINT_SWEEP_INTERVAL_SECONDS,
    )

    swept = []

    def fake_expire(watch_dir):
        swept.append(watch_dir)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        cog = _bookshelf_cog(tmp)
        original = module._expire_stale_hints
        module._expire_stale_hints = fake_expire
        try:
            asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 2

            cog._last_hint_sweep = datetime.now() - timedelta(
                seconds=HINT_SWEEP_INTERVAL_SECONDS + 1
            )
            asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 4, "an expired interval must sweep again"
        finally:
            module._expire_stale_hints = original


# ===================================================================
# 7. One request per show, not one per season
# ===================================================================

def test_show_episodes_are_fetched_in_one_request():
    from plugins.media_cleanup.cog import MediaCleanupCog

    calls = {"episodes": 0, "seasons": 0}

    class FakeEpisode:
        lastViewedAt = datetime.now() - timedelta(days=400)

    class FakeShow:
        type = "show"
        title = "Some Show"
        ratingKey = 42
        addedAt = datetime.now() - timedelta(days=500)

        def episodes(self):
            calls["episodes"] += 1
            return [FakeEpisode(), FakeEpisode()]

        def seasons(self):
            calls["seasons"] += 1
            raise AssertionError("seasons() costs one extra request per season")

    cog = object.__new__(MediaCleanupCog)
    cog.config = {
        "inactivity_days": 90,
        "notify_days_before": 7,
        "exempt_items": {},
        "dry_run": True,
    }
    cog._get_recent_request_timestamp = lambda item: None

    result = cog.check_item_for_cleanup(FakeShow())

    # check_item_for_cleanup swallows exceptions and returns None, so assert on
    # the result too - otherwise a raising fake would look like a pass.
    assert result is not None, "the show was not classified; an exception was swallowed"
    assert result["action"] == "delete"
    assert calls["seasons"] == 0, "seasons() costs 1 + N requests per show"
    assert calls["episodes"] == 1, "expected exactly one /allLeaves request"


def test_media_cleanup_does_not_walk_seasons():
    """Pattern: the 1 + N shape must not come back."""
    text = (ROOT / "plugins/media_cleanup/cog.py").read_text()
    offenders = [
        node.lineno
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "seasons"
    ]
    assert offenders == [], (
        f"seasons() at line(s) {offenders}: use show.episodes(), which fetches "
        f"/allLeaves in a single request"
    )
