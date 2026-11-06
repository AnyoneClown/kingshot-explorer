import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PlayerLookupUnavailable(RuntimeError):
    """The player lookup service could not answer (outage, auth, bad payload).

    Distinct from "player not found" (``get_player_info`` returns ``None``), so
    callers can tell users to retry later instead of telling them the ID is wrong.
    """


# Name used by the /stats UID fallback; both names refer to the same exception.
PlayerInfoUnavailableError = PlayerLookupUnavailable


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
    """Validates Governor IDs and fetches profiles through the KingShot Data API.

    The original gift-code API is reserved for redemptions (it is rate limited to
    30 requests/minute), so lookups go through ``/v1/players/by-fid/{fid}``. The raw
    profile is returned under ``_profile`` so /stats needs no second profile call.
    """

    def __init__(self, kingshot_data_service=None):
        self._kingshot_data_service = kingshot_data_service
        logger.info("PlayerInfoService initialized using the KingShot Data API")

    @staticmethod
    def _normalize(profile: Dict[str, Any], player_id: str) -> Dict[str, Any]:
        alliance = profile.get("alliance") if isinstance(profile.get("alliance"), dict) else {}
        avatar = profile.get("avatar_image") or profile.get("avatar_url")
        level = profile.get("stove_lv") if profile.get("stove_lv") is not None else profile.get("lv")
        return {
            "name": profile.get("name") or f"Player {player_id}",
            "playerId": str(profile.get("fid") or player_id),
            "playerUid": str(profile["uid"]),
            "level": level,
            "kingdom": profile.get("kid"),
            "power": profile.get("power"),
            "allianceTag": alliance.get("abbr"),
            "profilePhoto": avatar if isinstance(avatar, str) and avatar.startswith(("https://", "http://")) else None,
            "_profile": profile,
        }

    async def get_player_info(self, player_id: str) -> Optional[Dict[str, Any]]:
        player_id = str(player_id).strip()
        logger.info("Fetching player info for Governor ID %s", player_id)
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
            if result.get("status_code") == 404:
                return None
            logger.warning(
                "Player lookup unavailable for %s: %s %s",
                player_id,
                result.get("error_code"),
                result.get("error_message"),
            )
            raise PlayerLookupUnavailable(result.get("error_message") or "KingShot Data API request failed")

        profile = result.get("data")
        if not isinstance(profile, dict):
            raise PlayerLookupUnavailable("KingShot Data API returned an invalid player profile")
        if profile.get("error") == "fid not found":
            logger.info("Player not found: %s", player_id)
            return None
        if profile.get("error"):
            raise PlayerLookupUnavailable(str(profile["error"]))
        if not profile.get("uid"):
            raise PlayerLookupUnavailable("KingShot Data API returned an incomplete player profile")

        player_data = self._normalize(profile, player_id)
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

