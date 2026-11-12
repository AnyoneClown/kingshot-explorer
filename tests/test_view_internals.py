"""Panels must not replace discord.py's own View and Modal machinery.

discord.py calls private methods of a view while the bot runs, for example
``View._refresh(components)`` whenever a message with a live view is edited. A panel
that defines a method of the same name receives that call, and the error it raises
there is not caught: it stops the bot.
"""

import importlib
import inspect
import pkgutil

import discord
import pytest

import handlers
from handlers.leaderboard_handler import LeaderboardHandler, LeaderboardView
from services.leaderboard_service import BOARDS_BY_TYPE

# Hooks discord.py documents for overriding.
OVERRIDABLE = {"interaction_check", "on_timeout", "on_error", "on_submit"}


def panel_classes():
    found = {}
    for module_info in pkgutil.iter_modules(handlers.__path__):
        module = importlib.import_module(f"handlers.{module_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and issubclass(cls, (discord.ui.View, discord.ui.Modal)):
                found[f"{module_info.name}.{cls.__name__}"] = cls
    return found


def library_names(cls) -> set[str]:
    names = set()
    for base in cls.__mro__:
        if base.__module__.startswith("discord."):
            names.update(vars(base))
            names.update(getattr(base, "__slots__", ()))
    return {name for name in names if not name.startswith("__")}


PANELS = panel_classes()


def test_the_handlers_define_panels():
    assert "leaderboard_handler.LeaderboardView" in PANELS and len(PANELS) > 5


@pytest.mark.parametrize("name", sorted(PANELS))
def test_panels_do_not_replace_library_internals(name):
    cls = PANELS[name]
    own = {
        attribute
        for base in cls.__mro__
        if not base.__module__.startswith("discord.") and base is not object
        for attribute in vars(base)
        if not attribute.startswith("__")
    }

    assert own & library_names(cls) - OVERRIDABLE == set()


@pytest.mark.asyncio
async def test_an_edited_leaderboard_message_refreshes_its_view_like_any_other():
    handler = LeaderboardHandler(object(), object())
    view = LeaderboardView(handler, 1, board=BOARDS_BY_TYPE[3], kingdom=830, global_scope=False)

    assert LeaderboardView._refresh is discord.ui.View._refresh
    # What discord.py does on MESSAGE_UPDATE for a message that carries this view.
    view._refresh([])
