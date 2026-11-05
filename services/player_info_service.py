import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PlayerLookupUnavailable(Exception):
    """The player lookup service could not answer (outage, auth, missing key).

    Distinct from "player not found" (``get_player_info`` returns ``None``), so
    callers can tell users to retry later instead of telling them the ID is wrong.
    """


class IPlayerInfoService(ABC):
    """Interface for player info service - Interface Segregation Principle."""

    @abstractmethod
    async def get_player_info(self, player_id: str) -> Optional[Dict[str, Any]]:
        """Fetch player information by Governor ID.

        Returns the normalized profile, ``None`` when the player does not exist, and
        raises ``PlayerLookupUnavailable`` when the lookup service is unavailable.
        """
        pass


class PlayerInfoService(IPlayerInfoService):
    """Validates Governor IDs and fetches basic profiles through the KingShot Data API.

    The original gift-code API is reserved for redemptions (it is rate limited to
    30 requests/minute), so lookups go through ``/v1/players/by-fid/{fid}``.
    """

    def __init__(self, kingshot_data_service=None):
        self._kingshot_data_service = kingshot_data_service
        logger.info("PlayerInfoService initialized using the KingShot Data API")

    @staticmethod
    def _normalize(raw_data: Dict[str, Any], player_id: str) -> Dict[str, Any]:
        uid = raw_data.get("uid")
        alliance = raw_data.get("alliance") if isinstance(raw_data.get("alliance"), dict) else {}
        return {
            "name": raw_data.get("name"),
            "playerId": str(raw_data.get("fid") or player_id),
            "playerUid": str(uid) if uid not in (None, "", 0, "0") else None,
            "level": raw_data.get("stove_lv"),
            "kingdom": raw_data.get("kid"),
            "power": raw_data.get("power"),
            "allianceTag": alliance.get("abbr"),
            "profilePhoto": raw_data.get("avatar_image"),
        }

    async def get_player_info(self, player_id: str) -> Optional[Dict[str, Any]]:
        player_id = str(player_id).strip()
        logger.info("Fetching player info for ID: %s", player_id)
        if not player_id.isdigit():
            return None
        if self._kingshot_data_service is None:
            raise PlayerLookupUnavailable("KingShot Data API is not configured.")

        try:
            result = await self._kingshot_data_service.get_player_by_fid(player_id)
        except Exception as exc:  # network stack errors are already mapped, but be safe
            logger.error("Player lookup failed for %s: %s", player_id, exc, exc_info=True)
            raise PlayerLookupUnavailable(str(exc)) from exc

        if not result.get("success"):
            status_code = result.get("status_code")
            if status_code == 404:
                return None
            logger.warning(
                "Player lookup unavailable for %s: %s %s",
                player_id,
                result.get("error_code"),
                result.get("error_message"),
            )
            raise PlayerLookupUnavailable(result.get("error_message") or "Player lookup failed.")

        raw_data = result.get("data")
        if not isinstance(raw_data, dict) or raw_data.get("error"):
            logger.info("Player not found: %s", player_id)
            return None
        # Unknown ids can come back as null-filled profiles.
        if raw_data.get("uid") in (None, 0, "0") and not raw_data.get("name"):
            logger.info("Player not found (empty profile): %s", player_id)
            return None

        player_data = self._normalize(raw_data, player_id)
        logger.info(
            "Fetched player info for ID %s: %s (Kingdom %s)",
            player_id,
            player_data.get("name") or "Unknown",
            player_data.get("kingdom") or "N/A",
        )
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
