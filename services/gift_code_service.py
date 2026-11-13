"""Gift code redemption service."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import (
    GiftCodeAnnouncementRepository,
    GiftCodeRedemptionRepository,
    GiftCodeRepository,
)
from services.kingshot_api import KingshotAPIClient
from services.transactions import run_db_operation

logger = logging.getLogger(__name__)


class IGiftCodeService(ABC):
    """Interface for gift code service - Interface Segregation Principle."""

    @abstractmethod
    async def redeem_gift_code_remote(
        self,
        player_id: int,
        gift_code: str,
        kingdom_id: str | int | None = None,
    ) -> Dict[str, Any]:
        """Redeem a gift code for a player through the remote API only (one attempt)."""

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
        """Track a gift code from the external API and return whether it was newly discovered.

        New codes are stored with ``auto_redeem_status="pending"`` until the automatic
        run has handled every eligible player.
        """

        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).add_or_update(
                code_id=code_id,
                code=code,
                created_at_api=created_at_api,
                expires_at=expires_at,
            )

        if session is not None:
            return await _inner(session)
        return await run_db_operation(await self._get_db_manager(), _inner, retry_on_integrity_error=True)

    async def add_submitted_gift_code(self, code: str) -> tuple[bool, Any]:
        """Track a member-submitted code as pending; returns (newly added, row)."""

        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).add_submitted(code)

        return await run_db_operation(await self._get_db_manager(), _inner, retry_on_integrity_error=True)

    async def get_gift_code(self, code: str) -> Any:
        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).get_by_code(code)

        return await self._with_session(None, _inner)

    async def get_announcements(self, gift_code: str) -> dict[int, int]:
        """Announcement message IDs already posted for ``gift_code``, by channel ID."""

        async def _inner(db_session: AsyncSession):
            return await GiftCodeAnnouncementRepository(db_session).for_code(gift_code)

        return await self._with_session(None, _inner)

    async def save_announcement(self, gift_code: str, channel_id: int, message_id: int) -> None:
        async def _inner(db_session: AsyncSession):
            await GiftCodeAnnouncementRepository(db_session).save(gift_code, channel_id, message_id)

        await run_db_operation(await self._get_db_manager(), _inner, retry_on_integrity_error=True)

    async def purge_announcements(self, older_than_days: int = 14) -> int:
        """Forget announcement messages of codes that are long finished."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

        async def _inner(db_session: AsyncSession):
            return await GiftCodeAnnouncementRepository(db_session).delete_older_than(cutoff)

        return await run_db_operation(await self._get_db_manager(), _inner)

    async def list_pending_gift_codes(self) -> list[Any]:
        """Codes whose automatic redemption has not finished (oldest first)."""

        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).list_pending()

        return await self._with_session(None, _inner)

    async def mark_gift_code_completed(self, code_id: int) -> bool:
        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).mark_completed(code_id)

        return await run_db_operation(await self._get_db_manager(), _inner)

    async def reopen_gift_code(self, code_id: int) -> bool:
        """Queue a finished code for another automatic pass; True when it was reopened."""

        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).mark_pending(code_id)

        return await run_db_operation(await self._get_db_manager(), _inner)

    async def get_recent_gift_codes(self, limit: int = 5) -> list[Any]:
        async def _inner(db_session: AsyncSession):
            return await GiftCodeRepository(db_session).list_recent(limit)

        return await self._with_session(None, _inner)

    async def get_recorded_outcomes(self, gift_code: str, final_error_codes) -> list[Any]:
        """Successes and final failures already stored for ``gift_code`` (oldest first)."""

        async def _inner(db_session: AsyncSession):
            return await GiftCodeRedemptionRepository(db_session).recorded_outcomes(gift_code, final_error_codes)

        return await self._with_session(None, _inner)

    async def get_redemption_rows(self, gift_codes: list[str], player_ids: list[str]) -> list[Any]:
        async def _inner(db_session: AsyncSession):
            return await GiftCodeRedemptionRepository(db_session).rows_for_codes(gift_codes, player_ids)

        return await self._with_session(None, _inner)

    async def redeem_gift_code_remote(
        self,
        player_id: int,
        gift_code: str,
        kingdom_id: str | int | None = None,
    ) -> Dict[str, Any]:
        """Redeem a gift code through the current kingdom-aware upstream API.

        Makes exactly one paced HTTP attempt. Retries (transient errors, rate limits)
        are decided by ``GiftRedemptionService.redeem_with_retries`` only.
        """
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

                    if not isinstance(data, dict) or data.get("status") != "success":
                        status = data.get("status") if isinstance(data, dict) else type(data).__name__
                        logger.error("kingshot.net API returned non-success status: %s", status)
                        return {
                            "success": False,
                            "message": (data.get("message") if isinstance(data, dict) else None) or "API Error",
                        }

                    payload = data.get("data")
                    codes = payload.get("giftCodes") if isinstance(payload, dict) else None
                    if not isinstance(codes, list):
                        codes = []
                    logger.debug("Successfully fetched %s gift codes from kingshot.net", len(codes))

                    return {
                        "success": True,
                        "data": codes,
                    }
        except asyncio.TimeoutError:
            # ClientTimeout(total=...) raises a bare TimeoutError, not an aiohttp.ClientError.
            # The polling loop retries every minute, so this is a warning, not a crash.
            logger.warning("Timed out fetching gift codes from kingshot.net")
            return {
                "success": False,
                "message": "Gift code API timed out",
            }
        except aiohttp.ClientError as e:
            logger.error("Failed to fetch gift codes from kingshot.net: %s", e)
            return {
                "success": False,
                "message": f"Gift code API network error: {e}",
            }
        except ValueError as e:
            # Not JSON (for example an HTML error page served with status 200).
            logger.error("kingshot.net returned an unreadable gift code list: %s", e)
            return {
                "success": False,
                "message": "Gift code API returned an invalid response",
            }
        except Exception as e:
            logger.error("Unexpected error fetching gift codes from kingshot.net: %s", e, exc_info=True)
            return {
                "success": False,
                "message": "An unexpected error occurred",
            }
