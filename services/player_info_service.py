import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from services.kingshot_data_service import KingshotDataService

logger = logging.getLogger(__name__)


class IPlayerInfoService(ABC):
    """Interface for player info service - Interface Segregation Principle."""

    @abstractmethod
    async def get_player_info(self, player_id: str) -> Optional[Dict[str, Any]]:
        """Fetch player information by ID."""
        pass


class PlayerInfoService(IPlayerInfoService):
    """Resolve in-game Governor IDs through the KingShot Data API."""

    def __init__(self, kingshot_data_service: KingshotDataService):
        self._kingshot_data_service = kingshot_data_service

    async def get_player_info(self, player_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch player information from the API.

        Args:
            player_id: The player ID to look up

        Returns:
            Dictionary containing player information, or None when the ID is unknown.
        """
        logger.info("Fetching player info for Governor ID %s", player_id)
        response = await self._kingshot_data_service.get_player_by_fid(player_id)
        if not response.get("success"):
            raise PlayerInfoUnavailableError(
                response.get("error_message") or "KingShot Data API request failed"
            )

        profile = response.get("data")
        if not isinstance(profile, dict):
            raise PlayerInfoUnavailableError("KingShot Data API returned an invalid player profile")
        if profile.get("error") == "fid not found":
            return None
        if profile.get("error"):
            raise PlayerInfoUnavailableError(str(profile["error"]))
        if not profile.get("uid"):
            raise PlayerInfoUnavailableError("KingShot Data API returned an incomplete player profile")

        avatar = profile.get("avatar_image") or profile.get("avatar_url")
        player_data = {
            "name": profile.get("name") or f"Player {player_id}",
            "playerId": str(profile.get("fid") or player_id),
            "playerUid": str(profile["uid"]),
            "level": profile.get("stove_lv") if profile.get("stove_lv") is not None else profile.get("lv"),
            "kingdom": profile.get("kid"),
            "profilePhoto": avatar if isinstance(avatar, str) and avatar.startswith(("https://", "http://")) else None,
            "_profile": profile,
        }
        logger.info("Resolved Governor ID %s in kingdom %s", player_id, player_data["kingdom"])
        return player_data

    def format_player_stats(self, player_data: Dict[str, Any]) -> str:
        """
        Format player data into a readable string.

        Args:
            player_data: Raw player data from API

        Returns:
            Formatted string with player statistics
        """
        if not player_data:
            return "No data available"

        # Format with emojis
        lines = []

        # Name
        if "name" in player_data:
            lines.append(f"👤 **Name:** {player_data['name']}")

        # Player ID
        if "playerId" in player_data:
            lines.append(f"🆔 **ID:** {player_data['playerId']}")

        # Castle Level
        if "levelRendered" in player_data:
            level_info = player_data["levelRendered"]
            if "levelRenderedDetailed" in player_data:
                level_info = player_data["levelRenderedDetailed"]
            lines.append(f"🏰 **Castle Level:** {level_info}")
        elif "level" in player_data:
            lines.append(f"🏰 **Castle Level:** Level {player_data['level']}")

        # Kingdom
        if "kingdom" in player_data:
            lines.append(f"🌍 **Kingdom:** {player_data['kingdom']}")

        return "\n".join(lines)


class PlayerInfoUnavailableError(RuntimeError):
    """The profile lookup failed for a reason other than an unknown Governor ID."""
