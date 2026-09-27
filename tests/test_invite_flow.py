# path: tests/test_invite_flow.py
"""Plex invite approval: persistence, ordering, and nickname length.

Regression coverage for:
  * PlexInviteApprovalView was the only persistent view never registered with
    add_view, and its buttons carried no custom_id - so after any restart
    (including every deploy) clicking Approve returned "This interaction failed"
    and the pending request became silently unactionable.
  * _send_to_admin recorded the admin message id against the saved request, but
    ran BEFORE _save_request created it, so the id was always dropped. Verified
    against the live database: 3 entries, 0 with a message_id.
  * ": Certified Dummy" is 17 characters, so any display name over 15 exceeded
    Discord's 32-char nickname limit and raised HTTPException - which was not
    caught, because only discord.Forbidden was, and Forbidden is a SUBCLASS of
    HTTPException. The role was already assigned by then.
"""
import inspect
import re

import discord

import conftest  # noqa: F401

from plugins.forever_dumb.cog import (
    DUMMY_SUFFIX, MAX_NICKNAME_LENGTH, dummy_nickname,
)
from plugins.user_invites.cog import (
    INVITE_MESSAGES_NAMESPACE, INVITES_NAMESPACE, PlexInviteApprovalView,
    UserInvitesCog,
)


def _source(obj) -> str:
    """Source of a function, unwrapping discord.py wrapper objects.

    app_commands.Command stores the function on .callback and tasks.Loop on
    .coro; inspect.getsource rejects the wrappers themselves.
    """
    target = getattr(obj, "callback", None) or getattr(obj, "coro", None) or obj
    return inspect.getsource(target)


def _code(obj) -> str:
    """Source with comment lines removed.

    Ordering assertions below search for literal strings, and a comment that
    explains the bug naturally quotes the very text being searched for. Stripping
    comments keeps those assertions about the code rather than the prose.
    """
    return "\n".join(
        line for line in _source(obj).splitlines()
        if not line.strip().startswith("#")
    )


# --- persistent view ---

def test_view_is_constructible_with_no_arguments():
    """add_view() registers an argument-less instance at startup."""
    view = PlexInviteApprovalView()
    assert view.user_id is None and view.email is None


def test_every_button_has_a_custom_id():
    """add_view raises ValueError on a persistent view whose items lack one."""
    view = PlexInviteApprovalView()
    for child in view.children:
        assert getattr(child, "custom_id", None), f"{child.label} has no custom_id"


def test_custom_ids_are_distinct_and_stable():
    view = PlexInviteApprovalView()
    ids = [c.custom_id for c in view.children]
    assert len(set(ids)) == len(ids)
    assert set(ids) == {"plex_invite_approve", "plex_invite_deny"}


def test_view_is_persistent():
    assert PlexInviteApprovalView().timeout is None


def test_discord_accepts_it_as_persistent():
    """The real check discord.py performs when registering a persistent view."""
    view = PlexInviteApprovalView()
    assert view.is_persistent(), (
        "is_persistent() is False, so bot.add_view() would raise ValueError"
    )


def test_cog_load_registers_the_view():
    """cog_load, not setup(): the plugin loader never calls setup()."""
    source = _source(UserInvitesCog.cog_load)
    assert "add_view(PlexInviteApprovalView())" in source, (
        "without this, pending approval messages stop working after a restart"
    )


def test_both_callbacks_recover_state_before_acting():
    for name in ("approve", "deny"):
        source = _source(getattr(PlexInviteApprovalView, name))
        assert "_ensure_loaded" in source, f"{name} does not recover view state"


# --- message id persistence ---

def test_request_is_saved_before_admins_are_notified():
    """The ordering bug: the id was recorded against a record that did not exist."""
    source = _code(UserInvitesCog.join_plex)
    save_at = source.find("_save_request(")
    notify_at = source.find("_send_to_admin(")
    assert save_at != -1 and notify_at != -1
    assert save_at < notify_at, (
        "_send_to_admin records the message id onto the saved request, so the "
        "request must be saved first"
    )


def test_message_record_is_written_unconditionally():
    source = _code(UserInvitesCog._send_to_admin)
    assert f"kv_set({INVITE_MESSAGES_NAMESPACE}" in source.replace(
        "INVITE_MESSAGES_NAMESPACE", INVITE_MESSAGES_NAMESPACE
    ) or "INVITE_MESSAGES_NAMESPACE" in source
    # It must not be gated on an existing record, which is what silently failed.
    assert "await kv_set(INVITE_MESSAGES_NAMESPACE, str(message.id)" in source


def test_message_ids_use_a_separate_namespace():
    """user_mgmt iterates plex_invites and reads every key as a Discord user id.

    Putting message ids there would make auto_link_users try to link them as users.
    """
    assert INVITE_MESSAGES_NAMESPACE != INVITES_NAMESPACE

    import plugins.user_mgmt.cog as user_mgmt

    source = _source(user_mgmt.UserMgmtCog.auto_link_users)
    assert "int(discord_id_str)" in source, (
        "if this stops treating keys as user ids, revisit the namespace split"
    )


# --- duplicate flow guard ---

def test_cog_tracks_in_progress_flows():
    cog = object.__new__(UserInvitesCog)
    UserInvitesCog.__init__(cog, bot=None, services=None)
    assert isinstance(cog._in_progress, set)


def test_join_plex_guards_and_always_releases():
    source = _code(UserInvitesCog.join_plex)
    assert "in self._in_progress" in source, "no guard against a concurrent flow"
    assert "finally:" in source and "_in_progress.discard" in source, (
        "the guard must be released in a finally, or a user could never retry"
    )


def test_dm_is_sent_before_the_user_is_told_to_check_dms():
    source = _code(UserInvitesCog.join_plex)
    dm_at = source.find("join-plex email collection prompt")
    told_at = source.find("Check your DMs!")
    assert dm_at != -1 and told_at != -1
    assert dm_at < told_at, (
        "telling the user to check DMs before sending one leaves them waiting for "
        "a message that may never arrive"
    )


def test_blocked_dm_tells_the_user():
    source = _code(UserInvitesCog.join_plex)
    forbidden = source.split("except discord.Forbidden:")[1]
    assert "followup.send" in forbidden, (
        "a blocked DM was only logged, so the user got no feedback at all"
    )


# --- nickname length ---

def test_suffix_length_assumption_holds():
    assert len(DUMMY_SUFFIX) == 17
    assert MAX_NICKNAME_LENGTH == 32


def test_short_name_is_untouched():
    assert dummy_nickname("someuser42") == "someuser42: Certified Dummy"


def test_long_name_is_trimmed_to_the_limit():
    result = dummy_nickname("SomeLongerUsername")
    assert len(result) <= MAX_NICKNAME_LENGTH
    assert result.endswith(DUMMY_SUFFIX)


def test_every_plausible_name_length_fits():
    for length in range(1, 60):
        for times in (1, 2, 12):
            result = dummy_nickname("x" * length, times)
            assert len(result) <= MAX_NICKNAME_LENGTH, (length, times, result)


def test_counter_variant_still_fits():
    result = dummy_nickname("AVeryVeryLongDisplayName", 2)
    assert len(result) <= MAX_NICKNAME_LENGTH
    assert result.endswith(" x2")


def test_absurd_counter_does_not_overflow():
    result = dummy_nickname("Bob", 99999)
    assert len(result) <= MAX_NICKNAME_LENGTH


def test_trimming_does_not_leave_trailing_space():
    assert ": " not in dummy_nickname("Name With Spaces Here", 1).replace(DUMMY_SUFFIX, "")


def test_http_exception_is_caught_not_just_forbidden():
    """Forbidden is a SUBCLASS of HTTPException, so ordering and coverage matter."""
    assert issubclass(discord.Forbidden, discord.HTTPException)

    import plugins.forever_dumb.cog as module

    source = inspect.getsource(module)
    assert "except discord.HTTPException" in source, (
        "a 32-char overflow raises HTTPException, which except Forbidden misses"
    )


def test_no_nickname_is_built_by_string_concatenation():
    """All sites must route through dummy_nickname so trimming is guaranteed."""
    import pathlib

    root = pathlib.Path(conftest.PROJECT_ROOT)
    offenders = []
    for name in ("forever_dumb", "self_roles"):
        path = root / "plugins" / name / "cog.py"
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if re.search(r'new_nickname\s*=\s*f"', line):
                offenders.append(f"{name}/cog.py:{number}")
    assert offenders == [], offenders


# --- persistent views must register from a hook the loader actually calls ---

def test_plugin_loader_never_calls_setup():
    """The premise of the test below, asserted rather than assumed.

    core.plugin_manager imports the module with fromlist=["setup"] but then
    instantiates the cog class and calls add_cog itself - it never invokes
    setup(). Anything placed there is dead code in this bot.
    """
    from core.plugin_manager import PluginManager

    source = _source(PluginManager.load_plugin).replace('fromlist=["setup"]', "")
    assert "setup(" not in source
    assert "cog_class(self.bot" in source


def test_no_persistent_view_is_registered_only_in_setup():
    """Registering in setup() means the buttons silently die on every restart.

    This is how PlexInviteApprovalView, AdminApprovalView and
    BookAdminApprovalView all ended up unregistered.
    """
    import pathlib

    root = pathlib.Path(conftest.PROJECT_ROOT)
    offenders = []
    for path in sorted(root.glob("plugins/*/cog.py")):
        in_setup = False
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.startswith("async def setup"):
                in_setup = True
            elif line and not line[0].isspace() and not line.startswith("async def setup"):
                in_setup = False
            if in_setup and "add_view(" in line and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert offenders == [], (
        "move these to cog_load or __init__, which add_cog actually triggers: "
        + str(offenders)
    )


def test_every_persistent_view_is_registered_somewhere_reachable():
    """Each timeout=None view must be registered from __init__ or cog_load."""
    import pathlib
    import re

    root = pathlib.Path(conftest.PROJECT_ROOT)
    missing = []
    for path in sorted(root.glob("plugins/*/cog.py")):
        text = path.read_text()
        if "timeout=None" not in text:
            continue
        registered = re.findall(r"add_view\(", text)
        if not registered:
            missing.append(path.relative_to(root).as_posix())
    assert missing == [], f"persistent views never registered in: {missing}"
