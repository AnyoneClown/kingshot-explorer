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
        self,
        session: AsyncSession | None,
        player_id: int,
        gift_code: str,
        kingdom_id: str | int | None = None,
    ) -> Dict[str, Any]:
        """Redeem a gift code for a player."""

    @abstractmethod
    async def redeem_gift_code_remote(
        self,
        player_id: int,
        gift_code: str,
        kingdom_id: str | int | None = None,
    ) -> Dict[str, Any]:
        """Redeem a gift code for a player through the remote API only."""

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

    async def redeem_gift_code(
        self,
        session: AsyncSession | None,
        player_id: int,
        gift_code: str,
        kingdom_id: str | int | None = None,
    ) -> Dict[str, Any]:
        """
        Redeem a gift code for a player.
        """

        async def _inner(db_session: AsyncSession) -> Dict[str, Any]:
            existing_redemption = await GiftCodeRedemptionRepository(db_session).find_successful_redemption(
                player_id=player_id,
                gift_code=gift_code,
            )
            if existing_redemption:
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
                }

            return await self.redeem_gift_code_remote(
                player_id=player_id,
                gift_code=gift_code,
                kingdom_id=kingdom_id,
            )

        return await self._with_session(session, _inner)

    async def redeem_gift_code_remote(
        self,
        player_id: int,
        gift_code: str,
        kingdom_id: str | int | None = None,
    ) -> Dict[str, Any]:
        """Redeem a gift code through the current kingdom-aware upstream API."""
        if kingdom_id in (None, "", 0, "0"):
            return {
                "success": False,
                "message": "Player kingdom is required for gift-code redemption.",
                "error_code": "MISSING_KINGDOM",
            }

        logger.info(
            "Redeeming gift code '%s' for player ID %s in kingdom %s",
            gift_code,
            player_id,
            kingdom_id,
        )

        try:
            api_client = await self.ensure_client()
            response_data = await api_client.redeem_code(str(player_id), str(kingdom_id), gift_code)
            code = response_data.get("code")
            raw_message = str(response_data.get("msg", "Unknown error occurred"))
            api_status = " ".join(raw_message.strip().rstrip(".").upper().split())
            raw_error_code = str(response_data.get("err_code", ""))

            if code == 0 or api_status == "SUCCESS":
                logger.info("Successfully redeemed gift code '%s' for player %s", gift_code, player_id)
                return {
                    "success": True,
                    "message": "Gift code redeemed successfully.",
                    "data": response_data.get("data"),
                    "api_status": api_status,
                }

            if api_status == "SAME TYPE EXCHANGE" or raw_error_code == "40011":
                logger.info(
                    "Successfully redeemed gift code '%s' for player %s via same-type exchange",
                    gift_code,
                    player_id,
                )
                return {
                    "success": True,
                    "message": "Gift code redeemed successfully (same-type exchange).",
                    "data": response_data.get("data"),
                    "api_status": api_status,
                    "error_details": {"err_code": raw_error_code},
                }

            if api_status == "RECEIVED" or raw_error_code == "40008":
                logger.info(
                    "Gift code '%s' was already redeemed for player %s",
                    gift_code,
                    player_id,
                )
                return {
                    "success": False,
                    "message": "Gift code was already redeemed for this player.",
                    "error_code": "ALREADY_REDEEMED_BY_API",
                    "error_details": {"err_code": raw_error_code},
                    "api_status": api_status,
                    "already_redeemed_by_api": True,
                }

            status_mapping = {
                "TIME ERROR": ("GIFT_CODE_EXPIRED", "Gift code has expired.", True),
                "CDK NOT FOUND": (
                    "GIFT_CODE_NOT_FOUND",
                    "Gift code was not found or is incorrect.",
                    True,
                ),
                "USED": (
                    "GIFT_CODE_CLAIM_LIMIT_REACHED",
                    "Gift code claim limit has been reached.",
                    True,
                ),
                "TIMEOUT RETRY": (
                    "TIMEOUT_RETRY",
                    "The gift-code server requested a retry.",
                    False,
                ),
                "TOO FREQUENT": (
                    "RATE_LIMITED",
                    "The player is temporarily rate limited.",
                    False,
                ),
                "USER INFO ERROR": (
                    "KINGDOM_MISMATCH",
                    "The kingdom does not match this player.",
                    False,
                ),
                "ROLE NOT EXIST": ("INVALID_PLAYER_ID", "Player ID does not exist.", False),
                "STOVE_LV ERROR": (
                    "CASTLE_LEVEL_TOO_LOW",
                    "The player's Town Center level is too low for this code.",
                    False,
                ),
                "RECHARGE_MONEY ERROR": (
                    "SPEND_REQUIREMENT_NOT_MET",
                    "The player does not meet this code's spending requirement.",
                    False,
                ),
                "RECHARGE_MONEY_VIP ERROR": (
                    "VIP_REQUIREMENT_NOT_MET",
                    "The player does not meet this code's VIP requirement.",
                    False,
                ),
                "SIGN ERROR": (
                    "SIGN_ERROR",
                    "The gift-code API rejected the request signature.",
                    False,
                ),
                "NOT LOGIN": (
                    "SESSION_REJECTED",
                    "The gift-code API rejected the session.",
                    False,
                ),
            }
            mapped = status_mapping.get(api_status)
            if mapped is not None:
                error_code, message, global_code_error = mapped
                logger.warning(
                    "Gift-code API rejected code '%s' for player %s: %s (code: %s)",
                    gift_code,
                    player_id,
                    api_status,
                    raw_error_code,
                )
                return {
                    "success": False,
                    "message": message,
                    "error_code": error_code,
                    "error_details": {"err_code": raw_error_code},
                    "api_status": api_status,
                    "global_code_error": global_code_error,
                }

            logger.warning(
                "Failed to redeem gift code '%s' for player %s: %s (code: %s)",
                gift_code,
                player_id,
                raw_message,
                raw_error_code,
            )
            return {
                "success": False,
                "message": raw_message,
                "error_code": raw_error_code or "API_REJECTED",
                "error_details": {"err_code": raw_error_code},
                "api_status": api_status,
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
                    logger.debug("Successfully fetched %s gift codes from kingshot.net", len(codes))

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
