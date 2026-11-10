import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands
from discord.ext import commands

from handlers.alliance_power_handler import (
    INACTIVITY_CAVEAT,
    AlliancePowerHandler,
    AlliancePowerView,
    _bounded_lines,
    _change,
    build_daily_report_embed,
    build_inactive_embed,
)
from services.alliance_power_service import FlatPowerMember, roster_changes


def _interaction(user_id=1, guild_id=10, *, manage_guild=False):
    state = {"done": False}

    async def respond(*args, **kwargs):
        state["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(
            id=user_id, roles=[],
            guild_permissions=SimpleNamespace(manage_guild=manage_guild, administrator=False),
        ),
        guild_id=guild_id, namespace=SimpleNamespace(kid=830),
        response=SimpleNamespace(
            send_message=AsyncMock(side_effect=respond), defer=AsyncMock(side_effect=respond),
            edit_message=AsyncMock(side_effect=respond), is_done=lambda: state["done"],
        ),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
        edit_original_response=AsyncMock(), delete_original_response=AsyncMock(),
    )


def _report(count=21):
    now = datetime.now(timezone.utc)
    members = {str(index): {"name": f"Member {index}", "fid": str(index + 100), "power": index * 100} for index in range(count)}
    def snapshot(days, power, roster):
        return SimpleNamespace(
            snapshot_date=(now - timedelta(days=days)).date(), captured_at=now - timedelta(days=days),
            alliance_name="Test Alliance", alliance_tag="TST", total_power=power, members=roster,
        )
    latest = snapshot(0, 500, members)
    daily = snapshot(1, 600, members)
    weekly = snapshot(7, 400, {})
    return {"kingdom_id": 830, "alliance_id": 83900004, "latest": latest, "daily": daily, "weekly": weekly, "history": [weekly, daily, latest]}


def _handler(report=None, bot=None, guild_configuration_service=None):
    service = SimpleNamespace(
        get_report=AsyncMock(return_value=report), track_alliance=AsyncMock(return_value=False),
        stop_tracking=AsyncMock(return_value=True), collect_due_snapshots=AsyncMock(return_value=[]),
        resolve_alliance_tag=AsyncMock(return_value=None),
    )
    return AlliancePowerHandler(
        service, bot, [1], alliance_choices=AsyncMock(return_value=[]), kid_choices=AsyncMock(return_value=[]),
        guild_configuration_service=guild_configuration_service,
    )


def test_power_report_deltas_and_pagination():
    async def check():
        assert _change(50, 100) == "-50 (-50.0%)"
        assert _change(150, 100) == "+50 (+50.0%)"
        assert _change(0, 0) == "+0 (percentage unavailable: baseline 0)"
        assert _change(50, 0) == "+50 (percentage unavailable: baseline 0)"
        assert _change(None, 100) == "Unavailable"
        assert _change(100, None) == "New history"
        report = _report()
        handler = _handler(report)
        view = AlliancePowerView(handler, 10, report, 1)
        assert view.timeout == 900  # Public browse panels stay usable for ~15 minutes.
        interaction = _interaction()
        assert view.summary_button.disabled and view.previous_button.disabled and view.next_button.disabled
        summary = view.build_embed()
        fields = {field.name: field.value for field in summary.fields}
        assert fields["Total member power"] == "500"
        assert fields["Daily change"] == "-100 (-16.7%)"
        assert fields["Weekly change"] == "+100 (+25.0%)"
        assert "Roster changes" in summary.description
        assert "UTC" in summary.footer.text
        assert view.sort_button.disabled  # sorting applies to the members list only
        await view.members_button.callback(interaction)
        members = view.build_embed()
        assert members.fields == [] and members.description.count("\nDay ") == 10  # compact entries
        assert "**1. Member 20** · 2,000\nDay +0 (+0.0%) · Week new" in members.description
        assert "Sorted by power" in members.footer.text and not view.sort_button.disabled
        await view.next_button.callback(interaction)
        await view.next_button.callback(interaction)
        assert view.page == 2 and view.next_button.disabled and not view.previous_button.disabled
        last = view.build_embed().description
        assert last.count("\nDay ") == 1 and "**21. Member 0** · 0\nDay +0 · Week new" in last
        await view.previous_button.callback(interaction)
        assert view.page == 1
        await view.sort_button.callback(interaction)
        assert view.sort == "daily" and view.page == 0 and view.sort_button.label == "Sort: Daily growth"
        assert "Sorted by daily growth" in view.build_embed().footer.text
        handler.service.get_report.assert_not_awaited()
        handler.service.track_alliance.assert_not_awaited()
        public_view = AlliancePowerView(handler, 10, report, 2, can_manage=False)
        assert public_view.stop_button not in public_view.children
        assert public_view.inactive_button not in public_view.children
        public_view.message = SimpleNamespace(edit=AsyncMock())
        await public_view.on_timeout()
        assert all(item.disabled for item in public_view.children)
        first = {**report, "daily": None, "weekly": None, "history": [report["latest"]]}
        assert "First snapshot saved" in AlliancePowerView(handler, 10, first, 1).build_embed().description
        report["latest"].members = {
            "new": {"name": "*" * 10000, "power": None},
            "rich": {"name": "Rich", "power": 5, "stove_lv": 30, "rank": 4, "vip": 9},
        }
        missing = AlliancePowerView(handler, 10, report, 1)
        await missing.members_button.callback(interaction)
        embed = missing.build_embed()
        assert "**1. Rich** · 5\nDay new · Week new · R4 · TC 30 · VIP 9" in embed.description
        assert "power unavailable\nDay n/a · Week n/a" in embed.description
        assert len(embed) <= 6000 and len(embed.description) <= 4096
    asyncio.run(check())


def test_stop_tracking_requires_manager_and_confirmation():
    async def check():
        report = _report()
        handler = _handler(report)
        view = AlliancePowerView(handler, 10, report, 1)
        view.message = SimpleNamespace(edit=AsyncMock())
        assert not await view.interaction_check(_interaction(user_id=2))
        stranger = _interaction(user_id=2)
        await view.stop_button.callback(stranger)
        assert "Manage Server" in stranger.response.send_message.call_args.kwargs["embed"].description
        other_server = _interaction(guild_id=99)
        await view.stop_button.callback(other_server)
        assert other_server.response.send_message.call_args.kwargs["ephemeral"]

        opened = _interaction()
        await view.stop_button.callback(opened)
        handler.service.stop_tracking.assert_not_awaited()  # Nothing changes before confirming.
        prompt_kwargs = opened.response.send_message.call_args.kwargs
        assert prompt_kwargs["ephemeral"] and "[TST] Test Alliance" in prompt_kwargs["embed"].description
        prompt = prompt_kwargs["view"]
        assert [item.label for item in prompt.children] == ["Stop tracking", "Keep tracking"]

        kept = _interaction()
        await prompt.keep_button.callback(kept)
        handler.service.stop_tracking.assert_not_awaited()
        assert kept.response.edit_message.call_args.kwargs["embed"].title == "Tracking continues"

        handler.service.stop_tracking.return_value = False
        opened = _interaction()
        await view.stop_button.callback(opened)
        confirmed = _interaction()
        await opened.response.send_message.call_args.kwargs["view"].confirm_button.callback(confirmed)
        handler.service.stop_tracking.assert_awaited_once_with(10, 830, 83900004)
        assert confirmed.edit_original_response.call_args.kwargs["embed"].title == "Tracking changed"
        assert not view.stop_button.disabled

        handler.service.stop_tracking.reset_mock()
        handler.service.stop_tracking.return_value = True
        opened = _interaction()
        await view.stop_button.callback(opened)
        confirmed = _interaction()
        await opened.response.send_message.call_args.kwargs["view"].confirm_button.callback(confirmed)
        handler.service.stop_tracking.assert_awaited_once_with(10, 830, 83900004)
        assert confirmed.edit_original_response.call_args.kwargs["embed"].title == "Power tracking stopped"
        assert all(item.disabled for item in view.children)
        assert view.message.edit.call_args.kwargs["embed"].title == "Power tracking stopped"
        handler.service.get_report.assert_not_awaited()
    asyncio.run(check())


def test_alliance_command_permissions_validation_and_first_report():
    async def check():
        report = _report()
        handler = _handler(report)
        for user_id, guild_id, kid, alliance in (
            (1, None, None, None), (2, 10, 830, "83900004"), (2, 10, 830, "TST"), (1, 10, None, "83900004"),
            (1, 10, 830, None), (1, 10, -1, "83900004"), (1, 10, 830, "TOOLONG"), (1, 10, 830, "T$T"),
            (1, 10, 830, "0"), (1, 10, 830, "9" * 5000), (1, 10, 830, str(2**63)), (1, 10, 2**63, "83900004"),
        ):
            interaction = _interaction(user_id, guild_id)
            await handler._handle_alliance(interaction, kid, alliance)
            assert interaction.response.send_message.call_args.kwargs["ephemeral"]
            interaction.response.defer.assert_not_awaited()
        handler.service.track_alliance.assert_not_awaited()
        interaction = _interaction()
        await handler._handle_alliance(interaction, 830, "83900004")
        handler.service.track_alliance.assert_awaited_once_with(10, 830, 83900004)
        assert interaction.response.defer.call_args.kwargs["ephemeral"]
        assert not interaction.followup.send.call_args.kwargs["ephemeral"]
        assert isinstance(interaction.followup.send.call_args.kwargs["view"], AlliancePowerView)
        interaction.delete_original_response.assert_awaited_once()
        handler.service.track_alliance.reset_mock()
        manager = _interaction(user_id=3, manage_guild=True)  # Manage Server, not a bot operator.
        await handler._handle_alliance(manager, 830, "83900004")
        handler.service.track_alliance.assert_awaited_once_with(10, 830, 83900004)
        assert manager.followup.send.call_args.kwargs["view"].can_manage
        receipt_failed = _interaction()
        receipt_failed.delete_original_response.side_effect = discord.HTTPException(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"
        )
        await handler._handle_alliance(receipt_failed)
        receipt_failed.followup.send.assert_awaited_once()
        handler.service.track_alliance.reset_mock()
        member = _interaction(user_id=2)
        await handler._handle_alliance(member)
        handler.service.track_alliance.assert_not_awaited()
        assert not member.followup.send.call_args.kwargs["view"].can_manage
        handler.service.get_report.return_value = None
        missing = _interaction()
        await handler._handle_alliance(missing)
        assert "No alliance" in missing.followup.send.call_args.args[0]
        assert missing.followup.send.call_args.kwargs["ephemeral"]
        handler.service.track_alliance.side_effect = ValueError("A complete snapshot is unavailable. Try again later.")
        failed = _interaction()
        await handler._handle_alliance(failed, 830, "83900004")
        assert failed.followup.send.call_args.kwargs["ephemeral"]
        assert "complete snapshot" in failed.followup.send.call_args.args[0]
    asyncio.run(check())


def test_alliance_command_accepts_exact_tags():
    async def check():
        handler = _handler(_report())
        handler._alliance_choices.return_value = [
            app_commands.Choice(name="[TSTX] Not exact - 10 members", value="83900001"),
            app_commands.Choice(name="[TST] Test Alliance - 50 members", value="83900004"),
        ]
        typed = _interaction()
        await handler._handle_alliance(typed, 830, "tst")
        handler._alliance_choices.assert_awaited_with(830, "tst")
        handler.service.track_alliance.assert_awaited_once_with(10, 830, 83900004)
        handler.service.resolve_alliance_tag.assert_not_awaited()

        # Not in the cached suggestions: resolve through the Data API.
        handler.service.track_alliance.reset_mock()
        handler._alliance_choices.return_value = []
        handler.service.resolve_alliance_tag.return_value = 83900009
        await handler._handle_alliance(_interaction(), 830, "XYZ")
        handler.service.resolve_alliance_tag.assert_awaited_once_with(830, "XYZ")
        handler.service.track_alliance.assert_awaited_once_with(10, 830, 83900009)

        handler.service.track_alliance.reset_mock()
        handler.service.resolve_alliance_tag.return_value = None
        unknown = _interaction()
        await handler._handle_alliance(unknown, 830, "NOP")
        handler.service.track_alliance.assert_not_awaited()
        assert "could not find an alliance tagged `NOP`" in unknown.followup.send.call_args.args[0]
        assert unknown.followup.send.call_args.kwargs["ephemeral"]
    asyncio.run(check())


def _snapshot(day, members, total=None):
    captured = datetime(2026, 9, day, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        snapshot_date=date(2026, 9, day), captured_at=captured, alliance_name="Test Alliance",
        alliance_tag="TST", total_power=total if total is not None else sum(m["power"] for m in members.values()),
        members=members,
    )


def _changes_report():
    older = _snapshot(1, {"1": {"name": "Stays", "power": 10}, "2": {"name": "Leaver", "power": 20},
                          "3": {"name": "Old", "power": 30}})
    newer = _snapshot(3, {"1": {"name": "Stays", "power": 11}, "3": {"name": "New", "power": 30},
                          "4": {"name": "Joiner", "power": 40, "stove_lv": 28}})
    flat = [FlatPowerMember("3", "New", 30, date(2026, 8, 20))]
    return {
        "kingdom_id": 830, "alliance_id": 83900004, "latest": newer, "previous": older, "daily": None,
        "weekly": None, "history": [older, newer], "changes": roster_changes(newer, older),
        "inactive": {7: flat + [FlatPowerMember("1", "Seven only", 11, date(2026, 8, 27), 25)], 14: flat},
    }


def test_changes_view_and_manager_only_inactive_view():
    async def check():
        report = _changes_report()
        handler = _handler(report)
        view = AlliancePowerView(handler, 10, report, 1)
        summary = {field.name: field.value for field in view.build_embed().fields}
        assert summary["Roster"] == (
            "+1 joined · -1 left · 1 renamed since 2026-09-01 (2 days, no complete snapshot in between)"
        )
        await view.changes_button.callback(_interaction())
        assert view.changes_button.disabled and view.next_button.disabled
        fields = {field.name: field.value for field in view.build_embed().fields}
        assert fields["Joined (1)"] == "Joiner · 40 · TC 28"
        assert fields["Left (1)"] == "Leaver · 20"
        assert fields["Renamed (1)"] == "Old → New"
        assert "no complete snapshot in between" in view.build_embed().description

        no_previous = {**report, "previous": None, "changes": None}
        first = AlliancePowerView(handler, 10, no_previous, 1)
        first.mode = "changes"
        assert "Two complete snapshots" in first.build_embed().fields[0].value

        member = _interaction(user_id=2)
        await view.inactive_button.callback(member)  # Re-checked even if a member reaches it.
        assert "Manage Server" in member.response.send_message.call_args.kwargs["embed"].description
        manager = _interaction()
        await view.inactive_button.callback(manager)
        kwargs = manager.response.send_message.call_args.kwargs
        assert kwargs["ephemeral"]
        assert INACTIVITY_CAVEAT in kwargs["embed"].description
        fields = {field.name: field.value for field in kwargs["embed"].fields}
        assert fields["Flat for 14+ days (1)"] == "New · 30"
        assert fields["Flat for 7+ days (1)"] == "Seven only · 11 · TC 25"
        assert "Not enough history" in build_inactive_embed({"inactive": {7: None, 14: None}}).fields[0].name
    asyncio.run(check())


def test_bounded_lines_never_exceed_field_limit():
    assert _bounded_lines([]) == "None"
    lines = [f"Member {index} " + "x" * 40 for index in range(100)]
    value = _bounded_lines(lines)
    assert len(value) <= 1024 and value.endswith("more")
    assert _bounded_lines(["a", "b"]) == "a\nb"


def test_daily_report_is_posted_to_configured_report_channels():
    async def check():
        report = _changes_report()
        settings = {
            10: SimpleNamespace(alliance_report_channel_id=500),
            11: SimpleNamespace(alliance_report_channel_id=None),
            12: SimpleNamespace(alliance_report_channel_id=600),
        }
        config = SimpleNamespace(get_settings=AsyncMock(side_effect=lambda guild_id: settings[guild_id]))
        channel = SimpleNamespace(guild=SimpleNamespace(id=10), send=AsyncMock())
        foreign = SimpleNamespace(guild=SimpleNamespace(id=999), send=AsyncMock())
        bot = SimpleNamespace(
            guilds=[SimpleNamespace(id=10), SimpleNamespace(id=11), SimpleNamespace(id=12)],
            get_channel=lambda channel_id: {500: channel, 600: foreign}.get(channel_id),
            fetch_channel=AsyncMock(),
        )
        handler = _handler(report, bot=bot, guild_configuration_service=config)
        handler.service.guilds_tracking = AsyncMock(return_value=[10, 11, 12, 13])  # 13: bot left that server.
        handler.service.get_alliance_report = AsyncMock(return_value=report)

        assert await handler.post_daily_reports([(830, 83900004)]) == 1
        handler.service.guilds_tracking.assert_awaited_once_with(830, 83900004)
        embed = channel.send.call_args.kwargs["embed"]
        assert embed.title == "Daily alliance report · [TST] Test Alliance"
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Joined (1)"].startswith("Joiner") and fields["Left (1)"].startswith("Leaver")
        assert "2 member(s)" in fields["Possibly inactive"]
        assert fields["Roster changes"] == "Since 2026-09-01 (2 days, no complete snapshot in between)."

        foreign.send.assert_not_awaited()  # Never post into another server's channel.

        channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        assert await handler.post_daily_reports([(830, 83900004)]) == 0  # Errors are contained.

        first = build_daily_report_embed({**report, "changes": None, "inactive": {7: None, 14: None}})
        assert "First snapshot saved" in {field.name: field.value for field in first.fields}["Roster changes"]
        quiet = roster_changes(report["latest"], report["latest"])
        quiet_embed = build_daily_report_embed({**report, "changes": quiet, "inactive": {}})
        assert "No joins" in {field.name: field.value for field in quiet_embed.fields}["Roster changes"]
    asyncio.run(check())


def test_alliance_command_autocomplete_and_polling():
    async def check():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        handler = _handler(bot=bot)
        handler.register_commands()
        command = bot.tree.get_command("alliance")
        assert command.guild_only
        assert not any(parameter.required for parameter in command.parameters)
        interaction = _interaction()
        await command._params["alliance"].autocomplete(interaction, "T")
        handler._alliance_choices.assert_awaited_once_with(830, "T")
        await command._params["kid"].autocomplete(interaction, 830)
        handler._kid_choices.assert_awaited_once_with(interaction, 830)
        handler.post_daily_reports = AsyncMock()
        handler.service.collect_due_snapshots.return_value = [(830, 1)]
        await handler._collect_snapshots()
        handler.service.collect_due_snapshots.assert_awaited_once_with(set())
        handler.post_daily_reports.assert_awaited_once_with([(830, 1)])
        handler.service.collect_due_snapshots.side_effect = RuntimeError("Temporary failure")
        await handler._collect_snapshots()  # A failed collection must not kill future daily runs.
        handler.service.collect_due_snapshots.side_effect = None
        handler.post_daily_reports.side_effect = RuntimeError("Posting failed")
        await handler._collect_snapshots()
        ready = asyncio.Event()
        bot.wait_until_ready = ready.wait
        handler.start_polling_task()
        task = handler._collect_snapshots.get_task()
        handler.start_polling_task()
        assert handler._collect_snapshots.get_task() is task and handler.is_polling_running()
        handler._collect_snapshots.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.close()
    asyncio.run(check())


def test_new_capture_from_setup_posts_daily_reports_in_background():
    async def check():
        handler = _handler(_report())
        handler.service.track_alliance.return_value = True
        handler.post_daily_reports = AsyncMock(return_value=1)
        await handler._handle_alliance(_interaction(), 830, "83900004")
        await asyncio.gather(*handler._background_tasks)
        handler.post_daily_reports.assert_awaited_once_with([(830, 83900004)])
    asyncio.run(check())


def test_setup_with_pending_first_snapshot_explains_the_hourly_retry():
    async def check():
        report = _report()
        report["latest"] = None
        handler = _handler(report)
        handler.service.track_alliance.return_value = None  # tracking saved, capture incomplete
        interaction = _interaction()
        await handler._handle_alliance(interaction, 830, "83900004")
        notes = [call for call in interaction.followup.send.call_args_list if call.kwargs.get("ephemeral")]
        assert notes and "retries automatically every hour" in notes[-1].args[0]
        public = [call for call in interaction.followup.send.call_args_list if "view" in call.kwargs]
        assert public and "Waiting for the first complete snapshot" in public[0].kwargs["embed"].description
    asyncio.run(check())


def test_growth_leaders_trend_and_sorting_by_growth():
    from handlers.alliance_power_handler import _compact, _short_change, sparkline, top_growth

    assert [_compact(v) for v in (999, 1_500, 45_600_000, 1_230_000_000, -2_000_000)] == [
        "999", "1.5K", "45.6M", "1.23B", "-2M",
    ]
    assert _short_change(1_500_000, 1_000_000) == "+500K (+50.0%)"
    assert _short_change(900, 1_000) == "-100 (-10.0%)"
    assert sparkline([1]) == "" and sparkline([5, 5, 5]) == "▄▄▄" and sparkline([0, 50, 100]) == "▁▅█"

    now = datetime.now(timezone.utc)

    def snapshot(days, members):
        return SimpleNamespace(
            snapshot_date=(now - timedelta(days=days)).date(), captured_at=now - timedelta(days=days),
            alliance_name="Test Alliance", alliance_tag="TST",
            total_power=sum(m["power"] or 0 for m in members.values()), members=members,
        )

    latest = snapshot(0, {
        "a": {"name": "Big", "power": 1_000}, "b": {"name": "Riser", "power": 600},
        "c": {"name": "Faller", "power": 100}, "d": {"name": "Joiner", "power": 50},
    })
    daily = snapshot(1, {
        "a": {"name": "Big", "power": 990}, "b": {"name": "Riser", "power": 300}, "c": {"name": "Faller", "power": 200},
    })
    older = snapshot(2, {"a": {"name": "Big", "power": 900}})
    assert [(uid, gain) for uid, _, gain, _ in top_growth(latest, daily)] == [("b", 300), ("a", 10)]
    assert top_growth(latest, None) == []

    report = {
        "kingdom_id": 830, "alliance_id": 1, "latest": latest, "daily": daily, "weekly": None,
        "history": [older, daily, latest], "changes": None, "inactive": {},
    }
    fields = {field.name: field.value for field in build_daily_report_embed(report).fields}
    assert fields["Top growth today"] == "1. Riser · +300 (+100.0%)\n2. Big · +10 (+1.0%)"
    assert "Top growth this week" not in fields
    assert fields["Trend (3 days)"].startswith("`▁") and "900 → 1.8K (+94.4%)" in fields["Trend (3 days)"]

    async def check():
        view = AlliancePowerView(_handler(report), 10, report, 1)
        assert "Top growth today" in {field.name for field in view.build_embed().fields}
        interaction = _interaction()
        await view.members_button.callback(interaction)
        await view.sort_button.callback(interaction)
        # Daily growth: Riser (+300), Big (+10), Faller (-100); Joiner has no baseline and goes last.
        assert [member["name"] for _, member in view.members] == ["Riser", "Big", "Faller", "Joiner"]
        await view.sort_button.callback(interaction)
        await view.sort_button.callback(interaction)
        assert view.sort == "power" and view.members[0][1]["name"] == "Big"

    asyncio.run(check())
