"""Gift code redemption service."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import GiftCodeRedemptionRepository, GiftCodeRepository
from services.kingshot_api import KingshotAPIClient

logger = logging.getLogger(__name__)


class IGiftCodeService(ABC):
    """Interface for gift code service - Interface Segregation Principle."""

    @abstractmethod
    async def redeem_gift_code(
        self, session: AsyncSession | None, player_id: int, gift_code: str
    ) -> Dict[str, Any]:
        """Redeem a gift code for a player."""

    @abstractmethod
    async def check_already_redeemed(
        self, session: AsyncSession | None, player_id: int, gift_code: str
    ) -> Optional[dict[str, Any]]:
        """Check if a gift code has already been successfully redeemed for a player."""

    @abstractmethod
    async def get_redeemed_players(self, session: AsyncSession | None, gift_code: str) -> set[str]:
        """Get set of player IDs who have already redeemed this gift code."""

    @abstractmethod
    async def get_available_gift_codes(self) -> Dict[str, Any]:
        """Get available gift codes from the API."""


class GiftCodeService(IGiftCodeService):
    """Service responsible for redeeming gift codes via external API."""

    def __init__(self, db_manager: DatabaseManager | None = None):
        """
        Initialize gift code service.
        """
        self._db_manager = db_manager
        self._client: Optional[KingshotAPIClient] = None
        logger.info("GiftCodeService initialized using original Kingshot API")

    async def _get_db_manager(self):
        return self._db_manager or get_db()

    async def _with_session(self, session: AsyncSession | None, fn: Callable[[AsyncSession], Any]) -> Any:
        if session is not None:
            return await fn(session)

        db = await self._get_db_manager()
        async with db.session() as db_session:
            return await fn(db_session)

    async def __aenter__(self):
        """Enter async context manager - initialize shared client."""
        client = KingshotAPIClient()
        self._client = client
        await client.ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager - cleanup client."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def ensure_client(self) -> KingshotAPIClient:
        """Ensure client is initialized. Called if service is used without context manager."""
        if self._client is None:
            self._client = KingshotAPIClient()
        return self._client

    async def close(self) -> None:
        """Manually close the client if not using context manager."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def check_already_redeemed(
        self, session: AsyncSession | None, player_id: int, gift_code: str
    ) -> Optional[dict[str, Any]]:
        """
        Check if a gift code has already been successfully redeemed for a player.
        """

        async def _inner(db_session: AsyncSession):
            return await GiftCodeRedemptionRepository(db_session).find_successful_redemption(
                player_id=player_id,
                gift_code=gift_code,
            )

        return await self._with_session(session, _inner)

    async def get_redeemed_players(self, session: AsyncSession | None, gift_code: str) -> set[str]:
        """
        Get set of player IDs who have already successfully redeemed this gift code.
        This includes both successful redemptions and failed attempts where the API
        indicated the code was already redeemed.
        This is more efficient than checking each player individually.
        """

        async def _inner(db_session: AsyncSession) -> set[str]:
            return await GiftCodeRedemptionRepository(db_session).redeemed_player_ids(gift_code)

        return await self._with_session(session, _inner)

    async def add_or_update_gift_code(
        self,
        code_id: int,
        code: str,
        created_at_api: Any,
        expires_at: Optional[Any] = None,
        session: AsyncSession | None = None,
    ) -> tuple[bool, Any]:
        """Track a gift code from the external API and return whether it was newly discovered."""

        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).add_or_update(
                code_id=code_id,
                code=code,
                created_at_api=created_at_api,
                expires_at=expires_at,
            )

        return await self._with_session(session, _inner)

    async def redeem_gift_code(self, session: AsyncSession | None, player_id: int, gift_code: str) -> Dict[str, Any]:
        """
        Redeem a gift code for a player.
        """

        async def _inner(db_session: AsyncSession) -> Dict[str, Any]:
            return await self._redeem_gift_code_with_session(player_id=player_id, gift_code=gift_code)

        return await self._with_session(session, _inner)

    async def _redeem_gift_code_with_session(self, player_id: int, gift_code: str) -> Dict[str, Any]:
        # Check if already redeemed
        existing_redemption = await self.check_already_redeemed(None, player_id, gift_code)
        if existing_redemption:
            player_profile: Optional[Dict[str, Any]] = None
            try:
                api_client = await self.ensure_client()
                player_resp = await api_client.get_player(str(player_id))
                player_profile = self._extract_player_profile(player_resp, str(player_id))
            except Exception as lookup_error:
                logger.debug(
                    "Skipping player metadata refresh for already-redeemed code '%s' and player %s: %s",
                    gift_code,
                    player_id,
                    lookup_error,
                )

            logger.info(
                "Gift code '%s' already redeemed for player %s at %s. Skipping API call.",
                gift_code,
                player_id,
                existing_redemption.created_at,
            )
            return {
                "success": False,
                "message": "This gift code has already been redeemed for this player.",
                "error_code": "ALREADY_REDEEMED",
                "already_redeemed": True,
                "already_redeemed_at": existing_redemption.created_at.isoformat(),
                "player_profile": player_profile,
            }

        logger.info("Redeeming gift code '%s' for player ID: %s", gift_code, player_id)
        player_profile: Optional[Dict[str, Any]] = None

        try:
            # Ensure client is available
            api_client = await self.ensure_client()

            # The API requires an active session with cookies from the get_player call
            # Otherwise we'll receive a 'NOT LOGIN' error during redemption
            player_resp = await api_client.get_player(str(player_id))
            player_profile = self._extract_player_profile(player_resp, str(player_id))
            if player_resp.get("code") != 0:
                logger.warning("Failed to get_player before redeeming for %s: %s", player_id, player_resp)

            response_data = await api_client.redeem_code(str(player_id), gift_code)
            code = response_data.get("code")
            msg = response_data.get("msg", "Unknown error occurred")

            if code == 0:
                logger.info("Successfully redeemed gift code '%s' for player %s", gift_code, player_id)
                return {
                    "success": True,
                    "message": msg,
                    "data": response_data.get("data"),
                    "player_profile": player_profile,
                }

            err_code = str(response_data.get("err_code", ""))

            # Check if the error indicates the code was already redeemed
            already_redeemed_phrases = [
                "already redeemed",
                "already been redeemed",
                "already used",
                "already claimed",
                "exceeded the limit",
                "have already received",
                "collected",
                "same type exchange",
                "time error",
                "received.",
            ]
            is_already_redeemed = any(phrase in msg.lower() for phrase in already_redeemed_phrases)

            if is_already_redeemed:
                logger.info(
                    "Gift code '%s' was already redeemed for player %s (detected from API response)",
                    gift_code,
                    player_id,
                )
                return {
                    "success": False,
                    "message": msg,
                    "error_code": "ALREADY_REDEEMED_BY_API",
                    "error_details": {"err_code": err_code},
                    "already_redeemed_by_api": True,
                    "player_profile": player_profile,
                }

            logger.warning(
                "Failed to redeem gift code '%s' for player %s: %s (code: %s)",
                gift_code,
                player_id,
                msg,
                err_code,
            )
            return {
                "success": False,
                "message": msg,
                "error_code": err_code,
                "error_details": {"err_code": err_code},
                "player_profile": player_profile,
            }

        except ValueError as e:
            logger.error(
                "Validation/Parse error redeeming gift code '%s' for player %s: %s",
                gift_code,
                player_id,
                e,
                exc_info=True,
            )
            return {
                "success": False,
                "message": "Error communicating with the API.",
                "error_code": "API_ERROR",
                "player_profile": player_profile,
            }
        except Exception as e:
            logger.error(
                "Unexpected error redeeming gift code '%s' for player %s: %s",
                gift_code,
                player_id,
                e,
                exc_info=True,
            )
            return {
                "success": False,
                "message": "An unexpected error occurred.",
                "error_code": "UNEXPECTED_ERROR",
                "player_profile": player_profile,
            }

    @staticmethod
    def _extract_player_profile(player_resp: Dict[str, Any], fallback_player_id: str) -> Optional[Dict[str, Any]]:
        """Normalize get_player API data into the internal player metadata shape."""
        if not isinstance(player_resp, dict) or player_resp.get("code") != 0:
            return None

        raw_data = player_resp.get("data") or {}
        if not isinstance(raw_data, dict):
            return None

        return {
            "playerId": str(raw_data.get("fid") or fallback_player_id),
            "playerUid": str(raw_data.get("uid")) if raw_data.get("uid") is not None else None,
            "name": raw_data.get("nickname"),
            "kingdom": raw_data.get("kid"),
            "level": raw_data.get("stove_lv"),
        }

    async def get_available_gift_codes(self) -> Dict[str, Any]:
        """
        Get available gift codes from kingshot.net API.

        Returns:
            Dictionary containing the API response with status and giftcodes
        """
        url = "https://kingshot.net/api/gift-codes"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        logger.error("Failed to fetch gift codes from kingshot.net, status: %s", response.status)
                        return {
                            "success": False,
                            "message": f"HTTP Error {response.status}",
                        }

                    data = await response.json()

                    if data.get("status") != "success":
                        logger.error("kingshot.net API returned non-success status: %s", data.get("status"))
                        return {
                            "success": False,
                            "message": data.get("message", "API Error"),
                        }

                    codes = data.get("data", {}).get("giftCodes", [])
                    logger.info("Successfully fetched %s gift codes from kingshot.net", len(codes))

                    return {
                        "success": True,
                        "data": codes,
                    }
        except aiohttp.ClientError as e:
            logger.error("Failed to fetch gift codes from kingshot.net: %s", e)
            return {
                "success": False,
                "message": f"Gift code API network error: {e}",
            }
        except Exception as e:
            logger.error("Unexpected error fetching gift codes from kingshot.net: %s", e, exc_info=True)
            return {
                "success": False,
                "message": "An unexpected error occurred",
            }
