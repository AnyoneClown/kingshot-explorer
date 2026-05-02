"""Shared Discord UI helpers."""

from __future__ import annotations

import discord


class EmbedColors:
    """Consistent embed colors across commands."""

    SUCCESS = discord.Color.green()
    WARNING = discord.Color.orange()
    ERROR = discord.Color.red()
    INFO = discord.Color.blue()
    NEUTRAL = discord.Color.blurple()


def build_status_embed(
    *,
    title: str,
    description: str,
    color: discord.Color = EmbedColors.INFO,
    footer: str | None = None,
) -> discord.Embed:
    """Build a compact status embed with optional footer text."""
    embed = discord.Embed(title=title, description=description, color=color)
    if footer:
        embed.set_footer(text=footer)
    return embed


def status_value(ok: bool, success_text: str = "Online", failure_text: str = "Unavailable") -> str:
    """Format a boolean health state for embeds."""
    return f"OK - {success_text}" if ok else f"ISSUE - {failure_text}"
