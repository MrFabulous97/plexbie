# path: tests/test_pagination.py
"""PaginationView button state, ownership and timeout behaviour.

Regression coverage for three defects in one class, all reachable from /recent:
  * update_buttons() was never called during construction, so Previous was
    enabled on page 1 and its callback returned without responding - which
    Discord shows the user as "This interaction failed".
  * self.message was never assigned anywhere, so on_timeout's `if self.message:`
    guard never passed and the buttons stayed clickable on an expired view.
  * There was no interaction_check, and the view edits one shared message with
    shared current_page state - so any user could move the page another user was
    reading.
"""
import asyncio

import discord

import conftest  # noqa: F401

from utils.embeds import PaginationView


def _embeds(count):
    return [discord.Embed(title=f"page {i}") for i in range(count)]


class _Response:
    def __init__(self):
        self.edited = []
        self.sent = []

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)

    async def send_message(self, content, ephemeral=False):
        self.sent.append(content)


class _Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _Interaction:
    def __init__(self, user_id=1):
        self.user = type("U", (), {"id": user_id})()
        self.response = _Response()
        self.message = _Message()


# --- initial button state ---

def test_previous_is_disabled_on_the_first_page():
    view = PaginationView(_embeds(5))
    assert view.previous_button.disabled is True, (
        "an enabled Previous on page 1 leads to an unanswered interaction"
    )
    assert view.next_button.disabled is False


def test_single_page_disables_both():
    view = PaginationView(_embeds(1))
    assert view.previous_button.disabled and view.next_button.disabled


def test_empty_embeds_does_not_crash():
    view = PaginationView([])
    assert view.previous_button.disabled and view.next_button.disabled


def test_next_is_disabled_on_the_last_page():
    view = PaginationView(_embeds(3))
    view.current_page = 2
    view.update_buttons()
    assert view.next_button.disabled is True


# --- every click answers the interaction ---

def test_clicking_previous_on_first_page_still_responds():
    """The original bug: the callback fell through and answered nothing."""
    view = PaginationView(_embeds(5))
    interaction = _Interaction()
    asyncio.run(view.previous_button.callback(interaction))
    assert interaction.response.edited, "interaction was never answered"
    assert view.current_page == 0, "page should stay clamped at 0"


def test_clicking_next_on_last_page_still_responds():
    view = PaginationView(_embeds(3))
    view.current_page = 2
    interaction = _Interaction()
    asyncio.run(view.next_button.callback(interaction))
    assert interaction.response.edited
    assert view.current_page == 2


def test_paging_forward_and_back():
    view = PaginationView(_embeds(4))
    i = _Interaction()
    asyncio.run(view.next_button.callback(i))
    assert view.current_page == 1
    asyncio.run(view.next_button.callback(i))
    assert view.current_page == 2
    asyncio.run(view.previous_button.callback(i))
    assert view.current_page == 1


def test_button_states_track_the_page():
    view = PaginationView(_embeds(3))
    i = _Interaction()
    asyncio.run(view.next_button.callback(i))
    assert not view.previous_button.disabled
    asyncio.run(view.next_button.callback(i))
    assert view.next_button.disabled, "at the last page Next must be disabled"


# --- ownership ---

def test_author_may_page():
    view = PaginationView(_embeds(3), author_id=42)
    assert asyncio.run(view.interaction_check(_Interaction(user_id=42))) is True


def test_other_user_is_refused_and_told_why():
    view = PaginationView(_embeds(3), author_id=42)
    interaction = _Interaction(user_id=999)
    assert asyncio.run(view.interaction_check(interaction)) is False
    assert interaction.response.sent, "the refused user should be told"


def test_no_author_id_keeps_the_old_open_behaviour():
    """Callers that do not pass author_id are unchanged."""
    view = PaginationView(_embeds(3))
    assert asyncio.run(view.interaction_check(_Interaction(user_id=7))) is True


# --- timeout ---

def test_timeout_disables_buttons_when_message_known():
    view = PaginationView(_embeds(3))
    message = _Message()
    view.message = message
    asyncio.run(view.on_timeout())
    assert all(child.disabled for child in view.children)
    assert message.edits, "the message should be edited to show disabled buttons"


def test_timeout_without_a_message_is_a_noop_not_a_crash():
    view = PaginationView(_embeds(3))
    asyncio.run(view.on_timeout())
    assert all(child.disabled for child in view.children)


def test_interacting_captures_the_message_for_timeout():
    """So a caller that forgets to assign view.message still gets cleanup."""
    view = PaginationView(_embeds(3))
    assert view.message is None
    interaction = _Interaction()
    asyncio.run(view.next_button.callback(interaction))
    assert view.message is interaction.message


def test_caller_passes_author_and_keeps_the_message():
    """recently_added is the only call site; it must supply both."""
    import inspect

    from plugins.recently_added.cog import RecentlyAddedCog

    source = inspect.getsource(RecentlyAddedCog.plex_recently.callback)
    assert "author_id=interaction.user.id" in source
    assert "view.message =" in source
