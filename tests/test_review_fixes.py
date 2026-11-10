"""Regression tests for the fixes and small features of the November 2026 review."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from handlers.alliance_power_handler import AlliancePowerHandler
from handlers.event_handler import EventHandler, SchedulerHealth
from handlers.guild_config_handler import GuildConfigView
from handlers.help_handler import HelpHandler
from handlers.permissions import AccessPolicy
from handlers.player_info_handler import PlayerInfoHandler, ScoutReportView
from handlers.setup_prompts import ChannelSetupView, offer_channel_setup
from handlers.status_handler import StatusHandler
from handlers.translation_handler import TranslationHandler, language_for_locale
from services.guild_configuration_service import GuildConfigurationService, GuildSettings
from services.translation_service import has_translatable_text, looks_clearly_english
from services.usage_stats_service import UsageStatsService
from services.voice_message_service import VoiceMessageService
from tests.sqlite_helpers import SqliteDb
from tests.test_chatbot_service import CONTEXT, TOOL_MODEL, bad_request, make_registry, make_service
from tests.test_translation_handler_auto import FakeGuildConfig, FakeMessage, FakeTranslation, make_handler

# ------------------------------------------------------------ command usage


def test_usage_counters_are_buffered_flushed_and_summed():
    async def check():
        db = SqliteDb()
        stats = UsageStatsService(db)
        today = datetime.now(timezone.utc)
        for _ in range(3):
            stats.record("stats", 10)
        stats.record("stats", 20)
        stats.record("alliance", 10)
        stats.record("help", None)  # direct message
        stats.record("stats", 10, when=today - timedelta(days=45))  # outside the 30-day window
        stats.record("", 10)
        assert stats.buffered == 7

        # Buffered counts are already visible, and a flush moves them to the database.
        assert await stats.top_commands(30) == [("stats", 4), ("alliance", 1), ("help", 1)]
        assert await stats.flush() == 7 and stats.buffered == 0
        stats.record("stats", 10)
        assert await stats.flush() == 1
        assert await stats.top_commands(30) == [("stats", 5), ("alliance", 1), ("help", 1)]
        assert await stats.top_commands(30, guild_id=10) == [("stats", 4), ("alliance", 1)]
        assert await stats.top_commands(30, limit=1) == [("stats", 5)]

        stats._retention_days = 30
        assert await stats.purge_old() == 1

    asyncio.run(check())


def test_usage_counters_survive_a_failed_flush():
    async def check():
        class BrokenDb:
            def session(self):
                raise RuntimeError("database is down")

            async def run_in_transaction(self, operation, **kwargs):
                raise RuntimeError("database is down")

        stats = UsageStatsService(BrokenDb())
        stats.record("stats", 10)
        assert await stats.flush() == 0 and stats.buffered == 1

    asyncio.run(check())


def test_command_tree_counts_commands_but_not_autocomplete():
    from main import BotCommandTree

    stats = SimpleNamespace(calls=[], record=lambda name, guild_id: stats.calls.append((name, guild_id)))
    tree = SimpleNamespace(usage_stats=stats)
    command = SimpleNamespace(qualified_name="stats")
    BotCommandTree._count_usage(tree, SimpleNamespace(
        command=command, guild_id=5, type=discord.InteractionType.application_command))
    BotCommandTree._count_usage(tree, SimpleNamespace(
        command=command, guild_id=5, type=discord.InteractionType.autocomplete))
    BotCommandTree._count_usage(tree, SimpleNamespace(
        command=None, guild_id=5, type=discord.InteractionType.application_command))
    assert stats.calls == [("stats", 5)]


def test_status_shows_usage_and_hides_scheduler_errors_from_non_operators():
    async def check():
        usage = SimpleNamespace(top_commands=AsyncMock(return_value=[("stats", 12), ("alliance", 3)]))
        health = SchedulerHealth(
            running=True, last_run_at=datetime.now(timezone.utc), last_success_at=datetime.now(timezone.utc),
            last_error="Could not load due reminders: password authentication failed for host db.internal",
            last_error_at=datetime.now(timezone.utc), failed_count=1, sent_count=4, missed_count=0,
        )
        bot = SimpleNamespace(is_closed=lambda: False, latency=0.05, guilds=[1, 2])
        config = SimpleNamespace(
            admin_user_ids={1}, nvidia_chat_model="model", chat_history_limit=5, random_reply_chance=0.0,
            auto_redeem_channels=set(),
        )
        handler = StatusHandler(
            bot, config,
            database_health_service=SimpleNamespace(check_database=AsyncMock(return_value=(True, "ok"))),
            event_handler=SimpleNamespace(is_scheduler_running=lambda: True, scheduler_health=lambda: health),
            gift_code_handler=SimpleNamespace(is_polling_running=lambda: True),
            started_at=datetime.now(timezone.utc),
            access_policy=AccessPolicy({1}),
            usage_stats_service=usage,
        )

        async def run(user_id):
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=user_id), guild_id=77,
                response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
            )
            await handler._handle_status(interaction)
            embed = interaction.followup.send.call_args.kwargs["embed"]
            return {field.name: field.value for field in embed.fields}

        member = await run(2)
        assert "Sent 4 · Failed 1" in member["Scheduler"] and "password" not in member["Scheduler"]
        assert member["Most used commands · 30 days · this server"] == "`/stats` 12 · `/alliance` 3"
        usage.top_commands.assert_awaited_with(30, guild_id=77, limit=8)

        operator = await run(1)
        assert "password authentication failed" in operator["Scheduler"]
        assert "Most used commands · 30 days · all servers" in operator
        usage.top_commands.assert_awaited_with(30, guild_id=None, limit=8)

    asyncio.run(check())


# --------------------------------------------------------------- translation


def test_english_detection_ignores_homographs_and_game_loanwords():
    for text in ("no time do leader", "o time do server", "No me come", "Come no", "may gift code na", "war so"):
        assert not looks_clearly_english(text), text
    for text in ("come to the rally now please", "do you have the gift code?", "war starts in 10 minutes",
                 "the server is down again", "ok", "no", "gg"):
        assert looks_clearly_english(text), text
    # One CJK or Hangul character is a word; one Latin letter is not.
    assert has_translatable_text("好") and has_translatable_text("네")
    assert not has_translatable_text("k") and not has_translatable_text("😀 123")


def test_auto_translation_treats_question_marks_and_prices_as_chat_but_skips_bot_commands():
    translation = FakeTranslation({"language": "Korean", "text": "Why attack?"})
    handler, _ = make_handler(translation)
    for content in ("?? 왜 공격해요", "$100 es demasiado caro"):
        asyncio.run(handler._handle_auto_translation(FakeMessage(content)))
    assert [call for call in translation.calls] == ["?? 왜 공격해요", "$100 es demasiado caro"]
    for content in ("!rank ahora", "?help por favor", "/roll veinte"):
        asyncio.run(handler._handle_auto_translation(FakeMessage(content)))
    assert len(translation.calls) == 2


def test_long_quoted_translations_are_split_without_dropping_lines():
    handler, _ = make_handler()
    text = "\n".join(f"linea {index:03d}" for index in range(400))
    chunks = handler._quoted_chunks(text)
    assert all(len(chunk) <= 2000 for chunk in chunks) and len(chunks) <= handler.MAX_TRANSLATION_MESSAGES
    lines = [line for chunk in chunks for line in chunk.splitlines()]
    assert all(line.startswith("> ") for line in lines)
    # Every line of the first two messages is complete and in order: nothing was cut out mid-list.
    kept = [line[2:] for line in lines if line.startswith("> linea")]
    assert kept[: len(kept) - 1] == [f"linea {index:03d}" for index in range(len(kept) - 1)]
    assert handler._quoted_chunks("hola") == ["> hola"]


def test_translate_message_command_answers_privately_in_the_callers_language():
    async def check():
        translation = FakeTranslation({"text": "Привіт, ралі о 20:00"})
        handler, tracking = make_handler(translation)
        message = SimpleNamespace(
            content="Hello, rally at 20:00", jump_url="https://discord.com/channels/1/2/3",
            author=SimpleNamespace(display_name="Leader"), channel=SimpleNamespace(id=789),
        )
        interaction = SimpleNamespace(
            locale=discord.Locale.ukrainian,
            user=SimpleNamespace(id=10, name="user", discriminator="0", display_name="User"),
            guild=SimpleNamespace(id=456), channel=SimpleNamespace(id=789),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await handler._handle_translate_message_menu(interaction, message)

        assert translation.calls == [("Hello, rally at 20:00", "Ukrainian")]
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        sent = interaction.followup.send.call_args.kwargs
        assert sent["ephemeral"] is True and sent["embed"].title == "Translated to Ukrainian"
        assert "> Привіт, ралі о 20:00" in sent["embed"].description
        assert "Message by Leader" in sent["embed"].footer.text
        assert sent["view"].children[0].url == message.jump_url
        assert tracking.calls[0]["translation_type"] == "context_menu"
        assert tracking.calls[0]["target_language"] == "Ukrainian"

        # Nothing to translate: answered at once, without a model call.
        empty = SimpleNamespace(content="😀 123", jump_url=None, author=None, channel=None)
        interaction.response.send_message.reset_mock()
        await handler._handle_translate_message_menu(interaction, empty)
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
        assert len(translation.calls) == 1

    asyncio.run(check())
    assert language_for_locale(discord.Locale.american_english) == "English"
    assert language_for_locale("xx-unknown") == "English" and language_for_locale(None) == "English"


def test_translate_message_command_is_registered_as_a_context_menu():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    TranslationHandler(FakeTranslation(), SimpleNamespace(), bot).register_commands()
    menu = bot.tree.get_command("Translate", type=discord.AppCommandType.message)
    assert isinstance(menu, app_commands.ContextMenu)


def test_long_model_language_names_do_not_lose_the_translation_log_row():
    handler, tracking = make_handler()
    author = SimpleNamespace(id=1, name="u", discriminator="0", display_name="U")
    asyncio.run(handler._track_translation(
        author, original_text="a", translated_text="b", target_language="en",
        source_language="Indonesian (informal, mixed with Javanese slang and English loanwords)",
        translation_type="auto", guild=None, channel=SimpleNamespace(id=1),
    ))
    assert len(tracking.calls[0]["source_language"]) == 50


def test_ai_and_voice_replies_stay_off_when_the_settings_lookup_fails():
    class BrokenGuildConfig:
        async def get_or_create_for_guild(self, guild_id):
            raise RuntimeError("database is down")

    handler, _ = make_handler(guild_config=BrokenGuildConfig(), enable_voice_replies=True)
    assert asyncio.run(handler._ai_replies_enabled_for_guild(456, default_enabled=True)) is False
    assert asyncio.run(handler._voice_replies_enabled_for_guild(456)) is False


def test_a_declined_random_reply_does_not_use_up_the_users_direct_mention():
    async def check():
        chatbot = SimpleNamespace(calls=[])

        async def generate(message, history, **kwargs):
            chatbot.calls.append(kwargs["force_reply"])
            return None if not kwargs["force_reply"] else "Here you go."

        chatbot.generate_contextual_reply = generate
        handler, _ = make_handler(
            chatbot=chatbot, guild_config=FakeGuildConfig(), random_reply_chance=1.0, chat_user_cooldown_seconds=10,
        )
        # A random candidate the model declines...
        await handler._handle_contextual_chat(FakeMessage("what do you all think about this", roles=()))
        # ...then the same user asks the bot directly a second later.
        direct = FakeMessage("<@42> when is bear trap?", roles=(), mentions=[SimpleNamespace(id=42)])
        await handler._handle_contextual_chat(direct)
        assert chatbot.calls == [False, True]
        assert direct.replies and direct.replies[0]["content"] == "Here you go."
        # The channel was claimed before generating, so a second candidate is not answered too.
        await handler._handle_contextual_chat(FakeMessage("and another long chat message here", roles=()))
        assert chatbot.calls == [False, True]

    asyncio.run(check())


def test_system_messages_are_not_chat():
    async def check():
        bot = SimpleNamespace(user=SimpleNamespace(id=42), command_prefix="!", events={})
        bot.event = lambda fn: bot.events.setdefault(fn.__name__, fn)
        bot.process_commands = AsyncMock()
        handler = TranslationHandler(FakeTranslation(), SimpleNamespace(), bot)
        handler._handle_auto_translation = AsyncMock()
        handler._handle_contextual_chat = AsyncMock()
        handler.register_events()

        pinned = FakeMessage("")
        pinned.is_system = lambda: True
        await bot.events["on_message"](pinned)
        handler._handle_contextual_chat.assert_not_awaited()
        bot.process_commands.assert_not_awaited()

        normal = FakeMessage("hola a todos")
        normal.is_system = lambda: False
        await bot.events["on_message"](normal)
        handler._handle_auto_translation.assert_awaited_once()

    asyncio.run(check())


def test_unsupported_languages_get_no_audio_instead_of_the_english_voice():
    service = VoiceMessageService(
        backend=SimpleNamespace(), default_voice="Magpie-Multilingual.EN-US.Aria", default_language_code="en-US"
    )
    assert service._resolve_language_code(None) == "en-US"
    assert service._resolve_language_code("Spanish (Mexico)") == "es-US"
    assert service._resolve_language_code("Ukrainian") is None
    assert asyncio.run(service.generate_audio("Привіт усім", language_hint="Ukrainian")) is None


# --------------------------------------------------------------------- chat


def test_one_unrelated_bad_request_does_not_switch_tools_off():
    service, client = make_service(
        [
            bad_request("maximum context length exceeded"),
            '{"should_reply":true,"reply":"Plain answer"}',
            '{"reply":"With tools again","language":"English"}',
        ],
        tool_model=TOOL_MODEL,
        tools=make_registry([]),
    )
    first = asyncio.run(service.generate_contextual_reply("power?", [], force_reply=True, tool_context=CONTEXT))
    second = asyncio.run(service.generate_contextual_reply("power?", [], force_reply=True, tool_context=CONTEXT))
    assert (first, second) == ("Plain answer", "With tools again")
    assert [call["model"] for call in client.completions.calls][2] == TOOL_MODEL


def test_a_direct_mention_gets_an_answer_when_every_model_slot_stays_busy():
    async def check():
        service, client = make_service([], max_concurrent_requests=1)
        service.QUEUE_WAIT_SECONDS = 0.01
        await service._semaphore.acquire()
        try:
            reply = await service.generate_contextual_reply("hello?", [], force_reply=True)
            assert reply.is_fallback and client.completions.calls == []
        finally:
            service._semaphore.release()
        # The slot is not leaked by the timed-out wait.
        assert not service._semaphore.locked()

    asyncio.run(check())


# ----------------------------------------------------------------- settings


def test_empty_environment_values_fall_back_to_defaults(monkeypatch):
    from config.bot_config import BotConfig

    for name, value in {
        "DISCORD_TOKEN": "t", "COCKROACHDB_URL": "cockroachdb+asyncpg://u:p@h/db", "NVIDIA_API_KEY": "k",
        "RANDOM_REPLY_CHANCE": "", "MAX_PLAYERS_PER_GUILD": " ", "COMMAND_PREFIX": "",
        "MAX_CHAT_RESPONSE_CHARS": "5000",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("config.bot_config.load_dotenv", lambda: None)
    config = BotConfig.from_env()
    assert config.random_reply_chance == 0 and config.max_players_per_guild == 300
    assert config.command_prefix == "!"
    assert config.max_chat_response_chars == 2000  # Discord's message limit

    monkeypatch.setenv("CHAT_HISTORY_LIMIT", "many")
    with pytest.raises(ValueError, match="CHAT_HISTORY_LIMIT must be a number"):
        BotConfig.from_env()


def test_log_file_keeps_one_name_so_rotation_bounds_the_directory(tmp_path, monkeypatch):
    import logging

    from config.logging_config import setup_logging

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_INSTANCE", "testbot")
    root = logging.getLogger()
    previous_handlers, previous_level = list(root.handlers), root.level
    try:
        setup_logging("INFO")
        setup_logging("INFO")
        assert [path.name for path in (tmp_path / "logs").iterdir()] == ["testbot.log"]
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)


def test_settings_cache_does_not_restore_a_row_read_before_an_update():
    async def check():
        service = GuildConfigurationService(default_use_voice_replies=False, db_manager=object())
        row = {"use_random_replies": False}
        read_started, release_read = asyncio.Event(), asyncio.Event()

        def settings():
            return GuildSettings(
                guild_id=5, use_voice_replies=False, use_random_replies=row["use_random_replies"],
                manager_role_id=None, gift_code_channel_id=None, alliance_report_channel_id=None,
            )

        async def slow_read(operation, *, retry_on_integrity_error=False):
            snapshot = settings()
            read_started.set()
            await release_read.wait()
            return snapshot

        async def write(operation, *, retry_on_integrity_error=False):
            row["use_random_replies"] = True
            return settings()

        service._run = slow_read
        reader = asyncio.create_task(service.get_or_create_for_guild(5))
        await read_started.wait()
        service._run = write
        updated = await service.update_for_guild(5, use_random_replies=True)
        release_read.set()
        stale = await reader
        assert updated.use_random_replies is True and stale.use_random_replies is False
        # The stale read finished last but must not overwrite the fresh cache entry.
        assert service._cached(5).use_random_replies is True

    asyncio.run(check())


def test_manager_role_lookup_is_bounded(monkeypatch):
    async def check():
        async def never(guild_id):
            await asyncio.sleep(60)

        monkeypatch.setattr("handlers.permissions.MANAGER_ROLE_LOOKUP_TIMEOUT_SECONDS", 0.01)
        policy = AccessPolicy(guild_configuration_service=SimpleNamespace(get_settings=never))
        assert await policy.manager_role_id(5) is None
        member = SimpleNamespace(id=9, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
                                 roles=[SimpleNamespace(id=1)])
        assert await policy.is_guild_manager(member, 5) is False

    asyncio.run(check())


def test_configure_shows_saved_values_in_menus_and_rechecks_manager_rights():
    async def check():
        settings = SimpleNamespace(
            guild_id=999, use_voice_replies=False, use_random_replies=False,
            gift_code_channel_id=111, alliance_report_channel_id=None, manager_role_id=222,
        )
        service = SimpleNamespace(
            get_or_create_for_guild=AsyncMock(return_value=settings),
            get_settings=AsyncMock(return_value=settings),
        )
        view = GuildConfigView(
            guild_id=999, author_id=7, guild_name="Guild", guild_config_service=service,
            access_policy=AccessPolicy(guild_configuration_service=service),
        )
        embed = await view.build_embed()
        assert [value.id for value in view.gift_channel_select.default_values] == [111]
        assert view.report_channel_select.default_values == []
        assert [value.id for value in view.manager_role_select.default_values] == [222]
        assert "Setup 1/2 — no channel yet for: alliance reports" in embed.description

        def click(*, roles):
            sent = AsyncMock()
            return SimpleNamespace(
                user=SimpleNamespace(
                    id=7, roles=roles, guild_permissions=SimpleNamespace(manage_guild=False, administrator=False)),
                guild_id=999, guild=SimpleNamespace(id=999),
                response=SimpleNamespace(send_message=sent, is_done=lambda: False),
                followup=SimpleNamespace(send=AsyncMock()),
            )

        assert await view.interaction_check(click(roles=[SimpleNamespace(id=222)])) is True
        # The role was taken away while the panel stayed open.
        demoted = click(roles=[])
        assert await view.interaction_check(demoted) is False
        assert "Manage Server" in demoted.response.send_message.call_args.kwargs["embed"].description

    asyncio.run(check())


def test_channel_setup_is_offered_once_missing_and_saved_on_click():
    async def check():
        saved = []
        settings = SimpleNamespace(gift_code_channel_id=None, manager_role_id=None)

        async def update_for_guild(guild_id, **changes):
            saved.append((guild_id, changes))

        service = SimpleNamespace(get_settings=AsyncMock(return_value=settings), update_for_guild=update_for_guild)
        policy = AccessPolicy(guild_configuration_service=service)

        def interaction(*, manage):
            return SimpleNamespace(
                user=SimpleNamespace(
                    id=7, roles=[], guild_permissions=SimpleNamespace(manage_guild=manage, administrator=False)),
                guild_id=5, guild=SimpleNamespace(id=5), channel=SimpleNamespace(id=321),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False),
                followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
                edit_original_response=AsyncMock(),
            )

        member = interaction(manage=False)
        assert not await offer_channel_setup(
            member, setting="gift_code_channel_id", guild_config_service=service, access_policy=policy)
        member.followup.send.assert_not_awaited()

        manager = interaction(manage=True)
        followed = []

        async def on_saved(channel_id):
            followed.append(channel_id)

        assert await offer_channel_setup(
            manager, setting="gift_code_channel_id", guild_config_service=service, access_policy=policy,
            on_saved=on_saved,
        )
        offer = manager.followup.send.call_args.kwargs
        assert offer["ephemeral"] is True and "<#321>" in offer["embed"].description
        view = offer["view"]
        assert isinstance(view, ChannelSetupView) and view.use_button.label == "Announce gift codes here"
        await view.use_button.callback(manager)
        assert saved == [(5, {"gift_code_channel_id": 321})] and followed == [321]
        assert manager.edit_original_response.call_args.kwargs["embed"].title == "✅ Channel saved"

        # Already configured: nothing is offered.
        settings.gift_code_channel_id = 321
        again = interaction(manage=True)
        assert not await offer_channel_setup(
            again, setting="gift_code_channel_id", guild_config_service=service, access_policy=policy)

    asyncio.run(check())


# ----------------------------------------------------- help, events, alliance


def test_help_forms_respect_command_cooldowns():
    async def check():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        runs = []

        @bot.tree.command(name="kvk_prep")
        @app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.user.id)
        async def kvk_prep(interaction: discord.Interaction, kingdom_a: int, kingdom_b: int):
            runs.append((kingdom_a, kingdom_b))

        handler = HelpHandler(bot, {1})

        def click():
            return SimpleNamespace(
                user=SimpleNamespace(id=5), guild=SimpleNamespace(id=10), guild_id=10,
                created_at=datetime.now(timezone.utc),
                response=SimpleNamespace(send_message=AsyncMock()),
            )

        await handler.run_command(click(), "kvk_prep", {"kingdom_a": 1, "kingdom_b": 2})
        second = click()
        await handler.run_command(second, "kvk_prep", {"kingdom_a": 1, "kingdom_b": 2})
        assert runs == [(1, 2)]
        assert "Slow down" in second.response.send_message.call_args.args[0]

    asyncio.run(check())


def test_reminders_cannot_be_scheduled_where_the_bot_cannot_post():
    def interaction(**permissions):
        return SimpleNamespace(
            guild=SimpleNamespace(me=SimpleNamespace(id=1)),
            channel=SimpleNamespace(permissions_for=lambda member: discord.Permissions(**permissions)),
        )

    check = EventHandler._channel_permission_problems
    blocked, warning = check(interaction(view_channel=True, send_messages=False), ["everyone"])
    assert "can't post in this channel" in blocked and warning is None
    blocked, warning = check(interaction(view_channel=True, send_messages=True), ["everyone"])
    assert blocked is None and "without notifying anyone" in warning
    assert check(interaction(view_channel=True, send_messages=True, mention_everyone=True), ["everyone"]) == (None, None)
    assert check(interaction(view_channel=True, send_messages=True), ["Officers"]) == (None, None)
    # Test doubles without permission data are not blocked.
    assert check(SimpleNamespace(guild=None, channel=None), ["everyone"]) == (None, None)


def test_scheduler_shutdown_waits_for_the_running_pass():
    async def check():
        release = asyncio.Event()

        class Service:
            async def claim_due_events(self, grace):
                await release.wait()
                return SimpleNamespace(missed=[], due=[])

        handler = EventHandler(Service(), SimpleNamespace(), admin_user_ids=set())
        cancelled = []
        handler._scheduler_loop = SimpleNamespace(cancel=lambda: cancelled.append(handler._iteration_active))
        running = asyncio.create_task(handler.run_scheduler_iteration())
        await asyncio.sleep(0)
        assert handler._iteration_active
        stopping = asyncio.create_task(handler.shutdown_scheduler(timeout=1.0))
        await asyncio.sleep(0.06)
        assert cancelled == []  # still waiting for the pass
        release.set()
        await running
        await stopping
        assert cancelled == [False]  # cancelled only after the pass had finished

    asyncio.run(check())


def test_three_character_alliance_input_is_always_a_tag():
    parse = AlliancePowerHandler._parse_selection
    assert parse(830, "777") == (None, "777")
    assert parse(830, "AQM") == (None, "AQM")
    assert parse(830, "83900004") == (83900004, None)
    assert parse(830, "ab") is None and parse(None, "AQM") is None


# ------------------------------------------------------------- player lookups


def test_scout_next_from_the_summary_opens_the_top_player():
    async def check():
        handler = SimpleNamespace(
            _build_player_data_from_kingshot=PlayerInfoHandler._build_player_data_from_kingshot,
            _format_number=PlayerInfoHandler._format_number,
        )
        entries = [{"rank": index + 1, "fid": index + 100, "name": f"P{index}", "score": 1} for index in range(3)]
        view = ScoutReportView(handler, 830, entries, 1)
        shown = []

        async def show(interaction):
            shown.append(view.current_player)
            view.showing_summary = False
            view._update_controls()

        view._show_player = show
        assert view.previous_button.disabled and not view.next_button.disabled
        await view.next_button.callback(None)
        await view.next_button.callback(None)
        await view.previous_button.callback(None)
        assert shown == [0, 1, 0]

        single = ScoutReportView(handler, 830, entries[:1], 1)
        assert not single.next_button.disabled  # the only player is still reachable with Next

    asyncio.run(check())


def test_null_filled_profiles_are_retried_and_never_shown_or_cached():
    async def check():
        empty = {"success": True, "data": {"uid": 200, "name": None, "fid": None, "power": None, "kid": None}}
        full = {"success": True, "data": {"uid": 200, "fid": 100, "name": "Player", "power": 5, "kid": 830}}
        service = SimpleNamespace(get_player=AsyncMock(side_effect=[empty, full]), get_player_by_fid=AsyncMock())
        handler = PlayerInfoHandler(object(), object(), object(), service)
        handler.EMPTY_PROFILE_RETRY_SECONDS = 0
        assert (await handler._get_kingshot_data_player(None, uid="200"))["name"] == "Player"
        assert service.get_player.await_count == 2

        service.get_player = AsyncMock(return_value=empty)
        entry = {"uid": 200, "rank": 1}
        assert await handler._resolve_scout_player(entry) is None
        assert service.get_player.await_count == handler.EMPTY_PROFILE_ATTEMPTS
        assert "player" not in entry  # opening the player again retries the lookup

    asyncio.run(check())


def test_usage_dates_are_utc_days():
    stats = UsageStatsService(object())
    late = datetime(2026, 11, 10, 23, 30, tzinfo=timezone(timedelta(hours=-3)))  # already Nov 11 in UTC
    stats.record("stats", 1, when=late)
    assert list(stats._buffer) == [(date(2026, 11, 11), 1, "stats")]


def test_configure_does_not_preselect_deleted_or_unsupported_channels_and_roles():
    async def check():
        settings = SimpleNamespace(
            guild_id=999, use_voice_replies=False, use_random_replies=False,
            gift_code_channel_id=111, alliance_report_channel_id=333, manager_role_id=222,
        )
        service = SimpleNamespace(get_or_create_for_guild=AsyncMock(return_value=settings))
        channels = {
            111: SimpleNamespace(type=discord.ChannelType.text, mention="#gifts",
                                 permissions_for=lambda member: discord.Permissions.all()),
            333: SimpleNamespace(type=discord.ChannelType.public_thread, mention="#thread",
                                 permissions_for=lambda member: discord.Permissions.all()),
        }
        guild = SimpleNamespace(get_channel=channels.get, get_role=lambda role_id: None, me=object())
        view = GuildConfigView(
            guild_id=999, author_id=7, guild_name="Guild", guild_config_service=service, guild=guild,
        )
        await view.build_embed()
        assert [value.id for value in view.gift_channel_select.default_values] == [111]
        assert view.report_channel_select.default_values == []  # a thread is not a menu choice
        assert view.manager_role_select.default_values == []  # the role was deleted

    asyncio.run(check())


def test_reminder_permission_check_uses_the_thread_permission_in_threads():
    class FakeThread(discord.Thread):
        __slots__ = ()

        def permissions_for(self, member):
            return discord.Permissions(
                view_channel=True, send_messages=False, send_messages_in_threads=True, mention_everyone=True)

    interaction = SimpleNamespace(guild=SimpleNamespace(me=object()), channel=object.__new__(FakeThread))
    assert EventHandler._channel_permission_problems(interaction, ["everyone"]) == (None, None)
