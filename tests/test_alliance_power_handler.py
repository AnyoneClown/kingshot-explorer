import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from handlers.alliance_power_handler import AlliancePowerHandler, AlliancePowerView, _change


def _interaction(user_id=1, guild_id=10):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), guild_id=guild_id, namespace=SimpleNamespace(kid=830),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: True),
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


def _handler(report=None, bot=None):
    service = SimpleNamespace(
        get_report=AsyncMock(return_value=report), track_alliance=AsyncMock(), stop_tracking=AsyncMock(return_value=True),
        collect_due_snapshots=AsyncMock(),
    )
    return AlliancePowerHandler(service, bot, [1], alliance_choices=AsyncMock(return_value=[]), kid_choices=AsyncMock(return_value=[]))


def test_power_report_deltas_pagination_and_stale_stop():
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
        interaction = _interaction()
        assert view.summary_button.disabled and view.previous_button.disabled and view.next_button.disabled
        summary = view.build_embed()
        fields = {field.name: field.value for field in summary.fields}
        assert fields["Total member power"] == "500"
        assert fields["Daily change"] == "-100 (-16.7%)"
        assert fields["Weekly change"] == "+100 (+25.0%)"
        assert "Roster changes" in summary.description
        assert "UTC" in summary.footer.text
        await view.members_button.callback(interaction)
        assert len(view.build_embed().fields) == 10
        assert "Weekly: New history" in view.build_embed().fields[0].value
        await view.next_button.callback(interaction)
        await view.next_button.callback(interaction)
        assert view.page == 2 and view.next_button.disabled and not view.previous_button.disabled
        assert len(view.build_embed().fields) == 1
        assert "baseline 0" in view.build_embed().fields[0].value
        await view.previous_button.callback(interaction)
        assert view.page == 1
        handler.service.get_report.assert_not_awaited()
        handler.service.track_alliance.assert_not_awaited()
        stranger = _interaction(user_id=2)
        assert not await view.interaction_check(stranger)
        await view.stop_button.callback(stranger)
        handler.service.stop_tracking.assert_not_awaited()
        handler.service.stop_tracking.return_value = False
        await view.stop_button.callback(interaction)
        handler.service.stop_tracking.assert_awaited_once_with(10, 830, 83900004)
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        assert not view.stop_button.disabled
        handler.service.stop_tracking.reset_mock()
        handler.service.stop_tracking.return_value = True
        await view.stop_button.callback(interaction)
        handler.service.stop_tracking.assert_awaited_once_with(10, 830, 83900004)
        handler.service.get_report.assert_not_awaited()
        assert all(item.disabled for item in view.children)
        public_view = AlliancePowerView(handler, 10, report, 2)
        assert public_view.stop_button not in public_view.children
        public_view.message = SimpleNamespace(edit=AsyncMock())
        await public_view.on_timeout()
        assert all(item.disabled for item in public_view.children)
        first = {**report, "daily": None, "weekly": None, "history": [report["latest"]]}
        assert "First snapshot saved" in AlliancePowerView(handler, 10, first, 1).build_embed().description
        report["latest"].members = {"new": {"name": "*" * 10000, "power": None}}
        missing = AlliancePowerView(handler, 10, report, 1)
        await missing.members_button.callback(interaction)
        assert "Unavailable" in missing.build_embed().fields[0].value
        assert len(missing.build_embed()) <= 6000 and len(missing.build_embed().fields[0].name) <= 256
    asyncio.run(check())


def test_alliance_command_permissions_validation_and_first_report():
    async def check():
        report = _report()
        handler = _handler(report)
        for user_id, guild_id, kid, alliance in (
            (1, None, None, None), (2, 10, 830, "83900004"), (1, 10, None, "83900004"),
            (1, 10, 830, None), (1, 10, -1, "83900004"), (1, 10, 830, "TST"), (1, 10, 830, "0"),
            (1, 10, 830, "9" * 5000), (1, 10, 830, str(2**63)), (1, 10, 2**63, "83900004"),
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
        receipt_failed = _interaction()
        receipt_failed.delete_original_response.side_effect = discord.HTTPException(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"
        )
        await handler._handle_alliance(receipt_failed)
        receipt_failed.followup.send.assert_awaited_once()
        handler.service.track_alliance.reset_mock()
        await handler._handle_alliance(_interaction(user_id=2))
        handler.service.track_alliance.assert_not_awaited()
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
        await handler._collect_snapshots()
        handler.service.collect_due_snapshots.assert_awaited_once_with(set())
        handler.service.collect_due_snapshots.side_effect = RuntimeError("Temporary failure")
        await handler._collect_snapshots()  # A failed collection must not kill future daily runs.
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
