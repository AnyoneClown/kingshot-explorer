import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import aiohttp

from services.kingshot_api import KingshotAPIClient

logger = logging.getLogger(__name__)


class IPlayerInfoService(ABC):
    """Interface for player info service - Interface Segregation Principle."""

    @abstractmethod
    async def get_player_info(self, player_id: str) -> Optional[Dict[str, Any]]:
        """Fetch player information by ID."""
        pass


class PlayerInfoService(IPlayerInfoService):
    """Service responsible for fetching player information from external API."""

    def __init__(self):
        """
        Initialize player info service.
        """
        logger.info("PlayerInfoService initialized using original Kingshot API")

    async def get_player_info(self, player_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch player information from the API.

        Args:
            player_id: The player ID to look up

        Returns:
            Dictionary containing player information, or None if request failed
        """
        logger.info(f"Fetching player info for ID: {player_id}")
        try:
            async with KingshotAPIClient() as client:
                response = await client.get_player(player_id)
                # Success response should have code 0
                if response.get("code") == 0:
                    raw_data = response.get("data", {})
                    if raw_data:
                        # Normalize to old format to minimize handler changes
                        player_data = {
                            "name": raw_data.get("nickname"),
                            "playerId": str(raw_data.get("fid", player_id)),
                            "playerUid": str(raw_data.get("uid")) if raw_data.get("uid") is not None else None,
                            "level": raw_data.get("stove_lv"),
                            "kingdom": raw_data.get("kid"),
                            "profilePhoto": raw_data.get("avatar_image"),
                        }
                        logger.info(
                            f"Successfully fetched player info for ID {player_id}: "
                            f"{player_data.get('name', 'Unknown')} (Kingdom {player_data.get('kingdom', 'N/A')})"
                        )
                    else:
                        player_data = {}
                        logger.warning(f"API returned code 0 but no data for player ID: {player_id}")
                    return player_data
                elif response.get("err_code") == 40004 or "not exist" in str(response.get("msg", "")).lower():
                    logger.warning(f"Player not found: {player_id}")
                    return None
                else:
                    logger.error(f"API error for player {player_id}: {response}")
                    return None
        except Exception as e:
            logger.error(
                f"Unexpected error fetching player info for {player_id}: {e}",
                exc_info=True,
            )
            return None

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
