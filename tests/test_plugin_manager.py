# path: tests/test_plugin_manager.py
"""Plugin cog-name derivation and unload bookkeeping.

Regression coverage for: load_plugin derived the cog class name with a PascalCase
join while unload_plugin used str.title(), producing "Watch_TrackingCog" for any
multi-word plugin. remove_cog() then matched nothing, yet the plugin was still
dropped from loaded_cogs - so the cog kept running while the manager reported it
gone, and a subsequent reload raised "cog already loaded".
"""
import asyncio

import conftest  # noqa: F401

from core.plugin_manager import PluginManager, cog_class_name


class _FakeBot:
    def __init__(self, registered=()):
        self.registered = set(registered)
        self.removed = []

    async def remove_cog(self, name):
        self.removed.append(name)
        if name in self.registered:
            self.registered.discard(name)
            return object()   # discord.py returns the cog
        return None           # ...or None when the name is unknown


def _manager(loaded, registered):
    manager = PluginManager(_FakeBot(registered), services=None)
    manager.loaded_cogs = list(loaded)
    return manager


# --- the name derivation both sides now share ---

def test_multi_word_plugin_name():
    assert cog_class_name("watch_tracking") == "WatchTrackingCog"


def test_three_word_plugin_name():
    assert cog_class_name("rate_limit_monitor") == "RateLimitMonitorCog"


def test_single_word_plugin_name():
    assert cog_class_name("status") == "StatusCog"


def test_title_case_bug_is_not_reintroduced():
    """str.title() produced Watch_TrackingCog - the underscore is the tell."""
    assert "_" not in cog_class_name("watch_tracking")


def test_derivation_matches_every_real_plugin():
    """The real cog classes must be findable by the derived name."""
    import importlib
    import pathlib

    plugins_dir = pathlib.Path(conftest.PROJECT_ROOT) / "plugins"
    checked = 0
    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir() or not (plugin_dir / "cog.py").exists():
            continue
        module = importlib.import_module(f"plugins.{plugin_dir.name}.cog")
        expected = cog_class_name(plugin_dir.name)
        assert hasattr(module, expected), (
            f"plugins/{plugin_dir.name}/cog.py has no class {expected}"
        )
        checked += 1
    assert checked >= 20, f"expected to check the full plugin set, got {checked}"


# --- unload bookkeeping ---

def test_unload_uses_the_pascal_case_name():
    manager = _manager(["watch_tracking"], ["WatchTrackingCog"])
    assert asyncio.run(manager.unload_plugin("watch_tracking")) is True
    assert manager.bot.removed == ["WatchTrackingCog"]
    assert manager.loaded_cogs == []


def test_unload_keeps_state_when_cog_is_not_registered():
    """The old code removed it from loaded_cogs even though nothing was unloaded."""
    manager = _manager(["watch_tracking"], [])   # nothing registered
    assert asyncio.run(manager.unload_plugin("watch_tracking")) is False
    assert manager.loaded_cogs == ["watch_tracking"], (
        "state must stay accurate when the unload failed"
    )


def test_unload_unknown_plugin_is_a_no_op():
    manager = _manager([], [])
    assert asyncio.run(manager.unload_plugin("nope")) is False
    assert manager.bot.removed == []


def test_reload_bails_out_when_unload_fails():
    """Otherwise load_plugin's add_cog raises against the still-live instance."""
    manager = _manager(["watch_tracking"], [])
    assert asyncio.run(manager.reload_plugin("watch_tracking")) is False
    assert manager.loaded_cogs == ["watch_tracking"]
