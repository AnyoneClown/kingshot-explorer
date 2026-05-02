import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class IKVKService(ABC):
    """Interface for KVK (Kingdom vs Kingdom) service."""

    @abstractmethod
    async def get_kingdom_stats(self, kingdom_number: int) -> Dict[str, Any]:
        """Get KVK match stats for a specific kingdom."""
        pass

    @abstractmethod
    async def compare_kingdoms(self, kingdom_a: int, kingdom_b: int) -> Dict[str, Any]:
        """Compare KVK match stats for two kingdoms."""
        pass


class KVKService(IKVKService):
    """Service responsible for fetching KVK match history via external API."""

    def __init__(self, api_base_url: str = "https://kingshot.net/api"):
        """
        Initialize KVK service.

        Args:
            api_base_url: Base URL for the KVK API
        """
        self._api_base_url = api_base_url
        self._matches_endpoint = f"{api_base_url}/kvk/matches"
        self._session: Optional[aiohttp.ClientSession] = None
        logger.info(f"KVKService initialized with matches endpoint: {self._matches_endpoint}")

    async def __aenter__(self):
        """Enter async context manager - initialize shared session."""
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager - cleanup session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure session is initialized. Called if service is used without context manager."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Manually close the session if not using context manager."""
        if self._session:
            await self._session.close()
            self._session = None

    async def get_kingdom_stats(self, kingdom_number: int) -> Dict[str, Any]:
        """
        Get KVK match stats for a specific kingdom.

        Args:
            kingdom_number: The kingdom number to fetch matches for

        Returns:
            Dictionary containing normalized KVK match stats
        """
        logger.info(f"Fetching KVK match stats for kingdom {kingdom_number}")

        result = await self._fetch_matches(kingdom_number, limit=100)
        if not result.get("success"):
            return result

        matches = self._normalize_matches(kingdom_number, result.get("data", []))
        summary = self._build_kingdom_summary(
            kingdom_number,
            matches,
            pagination=result.get("pagination", {}),
        )

        logger.info(f"Successfully fetched KVK match stats for kingdom {kingdom_number}")
        return {
            "success": True,
            "data": summary,
            "message": result.get("message", "KVK matches retrieved successfully"),
        }

    async def compare_kingdoms(self, kingdom_a: int, kingdom_b: int) -> Dict[str, Any]:
        """Compare two kingdoms by fetching their KVK match histories."""
        result_a, result_b, h2h_result = await asyncio.gather(
            self.get_kingdom_stats(kingdom_a),
            self.get_kingdom_stats(kingdom_b),
            self._fetch_matches(kingdom_a, opponent_number=kingdom_b, limit=100),
        )

        if not result_a.get("success") or not result_b.get("success"):
            errors = []
            if not result_a.get("success"):
                errors.append(f"Kingdom {kingdom_a}: {result_a.get('message', 'Unknown error')}")
            if not result_b.get("success"):
                errors.append(f"Kingdom {kingdom_b}: {result_b.get('message', 'Unknown error')}")
            return {
                "success": False,
                "message": " | ".join(errors) if errors else "Failed to compare kingdoms.",
            }

        data_a = result_a.get("data", {})
        data_b = result_b.get("data", {})
        h2h_matches_a: List[Dict[str, Any]] = []
        h2h_summary_a = self._build_kingdom_summary(kingdom_a, h2h_matches_a)
        h2h_summary_b = self._build_kingdom_summary(kingdom_b, [])

        if h2h_result.get("success"):
            h2h_matches_a = self._normalize_matches(kingdom_a, h2h_result.get("data", []))
            h2h_matches_b = self._normalize_matches(kingdom_b, h2h_result.get("data", []))
            h2h_summary_a = self._build_kingdom_summary(kingdom_a, h2h_matches_a)
            h2h_summary_b = self._build_kingdom_summary(kingdom_b, h2h_matches_b)
        else:
            logger.warning(
                f"Failed to fetch head-to-head KVK matches for {kingdom_a} vs {kingdom_b}: "
                f"{h2h_result.get('message')}"
            )

        advantages = {
            "winRate": self._compare_high_value(data_a.get("winRate"), data_b.get("winRate"), kingdom_a, kingdom_b),
            "wins": self._compare_high_value(data_a.get("wins"), data_b.get("wins"), kingdom_a, kingdom_b),
            "prepWinRate": self._compare_high_value(
                data_a.get("prepWinRate"), data_b.get("prepWinRate"), kingdom_a, kingdom_b
            ),
            "prepWins": self._compare_high_value(data_a.get("prepWins"), data_b.get("prepWins"), kingdom_a, kingdom_b),
            "castleCaptures": self._compare_high_value(
                data_a.get("castleCaptures"), data_b.get("castleCaptures"), kingdom_a, kingdom_b
            ),
            "defensesHeld": self._compare_high_value(
                data_a.get("defensesHeld"), data_b.get("defensesHeld"), kingdom_a, kingdom_b
            ),
            "headToHeadWins": self._compare_high_value(
                h2h_summary_a.get("wins"), h2h_summary_b.get("wins"), kingdom_a, kingdom_b
            ),
            "losses": self._compare_low_value(data_a.get("losses"), data_b.get("losses"), kingdom_a, kingdom_b),
        }

        score_a = sum(1 for winner in advantages.values() if winner == kingdom_a)
        score_b = sum(1 for winner in advantages.values() if winner == kingdom_b)

        return {
            "success": True,
            "data": {
                "kingdom_a": data_a,
                "kingdom_b": data_b,
                "advantages": advantages,
                "score": {
                    str(kingdom_a): score_a,
                    str(kingdom_b): score_b,
                },
                "head_to_head": {
                    "matches": h2h_matches_a,
                    "kingdom_a": h2h_summary_a,
                    "kingdom_b": h2h_summary_b,
                    "available": h2h_result.get("success", False),
                    "message": h2h_result.get("message"),
                },
            },
        }

    async def _fetch_matches(
        self,
        kingdom_number: int,
        opponent_number: Optional[int] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Fetch raw KVK matches from the Kingshot matches API."""
        params = {
            "kingdom_a": kingdom_number,
            "limit": limit,
        }
        if opponent_number is not None:
            params["kingdom_b"] = opponent_number

        try:
            http_session = await self.ensure_session()
            async with http_session.get(
                self._matches_endpoint,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                response_data = await response.json(content_type=None)

            if response.status == 200 and response_data.get("status") == "success":
                raw_matches = response_data.get("data", [])
                return {
                    "success": True,
                    "data": raw_matches if isinstance(raw_matches, list) else [],
                    "pagination": response_data.get("pagination", {}),
                    "message": response_data.get("message", "Matches retrieved successfully"),
                }

            error_message = response_data.get("message", "Failed to fetch KVK matches")
            logger.warning(f"Failed to fetch KVK matches for kingdom {kingdom_number}: {error_message}")
            return {
                "success": False,
                "message": error_message,
                "status_code": response.status,
            }

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Network error fetching KVK matches for kingdom {kingdom_number}: {e}", exc_info=True)
            return {
                "success": False,
                "message": "Network error occurred while fetching KVK matches.",
                "error_code": "NETWORK_ERROR",
            }
        except Exception as e:
            logger.error(
                f"Unexpected error fetching KVK matches for kingdom {kingdom_number}: {e}",
                exc_info=True,
            )
            return {
                "success": False,
                "message": "An unexpected error occurred.",
                "error_code": "UNEXPECTED_ERROR",
            }

    @classmethod
    def _normalize_matches(cls, kingdom_number: int, raw_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize API match rows into kingdom-centered history entries."""
        matches = [cls._normalize_match(kingdom_number, match) for match in raw_matches]
        matches.sort(key=lambda match: (match.get("season_date") or "", match.get("season_id") or 0), reverse=True)
        return matches

    @classmethod
    def _normalize_match(cls, kingdom_number: int, match: Dict[str, Any]) -> Dict[str, Any]:
        kingdom_a = cls._to_int(match.get("kingdom_a"))
        kingdom_b = cls._to_int(match.get("kingdom_b"))
        attacker = cls._to_int(match.get("attacker"))
        defender = cls._to_int(match.get("defender"))
        prep_winner = cls._to_int(match.get("prep_winner"))
        castle_winner = cls._to_int(match.get("castle_winner"))

        if kingdom_a == kingdom_number:
            opponent = kingdom_b
        elif kingdom_b == kingdom_number:
            opponent = kingdom_a
        else:
            opponent = None

        if attacker == kingdom_number:
            side = "attacker"
        elif defender == kingdom_number:
            side = "defender"
        else:
            side = "unknown"

        return {
            "kvk_id": match.get("kvk_id"),
            "season_id": cls._to_int(match.get("season_id")),
            "kvk": cls._to_int(match.get("season_id")),
            "title": match.get("kvk_title") or match.get("description") or "KvK",
            "season_date": match.get("season_date"),
            "kingdom": kingdom_number,
            "kingdom_a": kingdom_a,
            "kingdom_b": kingdom_b,
            "opponent": opponent,
            "attacker": attacker,
            "defender": defender,
            "side": side,
            "prep_winner": prep_winner,
            "castle_winner": castle_winner,
            "prepResult": cls._outcome(prep_winner, kingdom_number),
            "castleResult": cls._outcome(castle_winner, kingdom_number),
            "result": cls._outcome(castle_winner, kingdom_number),
            "castle_captured": bool(match.get("castle_captured")),
            "created_at": match.get("created_at"),
            "updated_at": match.get("updated_at"),
        }

    @classmethod
    def _build_kingdom_summary(
        cls,
        kingdom_number: int,
        matches: List[Dict[str, Any]],
        pagination: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build aggregate stats from normalized match history."""
        wins = cls._count_result(matches, "result", "win")
        losses = cls._count_result(matches, "result", "loss")
        prep_wins = cls._count_result(matches, "prepResult", "win")
        prep_losses = cls._count_result(matches, "prepResult", "loss")
        attacks = sum(1 for match in matches if match.get("side") == "attacker")
        defenses = sum(1 for match in matches if match.get("side") == "defender")
        castle_captures = sum(
            1
            for match in matches
            if match.get("side") == "attacker" and match.get("result") == "win" and match.get("castle_captured")
        )
        defenses_held = sum(
            1
            for match in matches
            if match.get("side") == "defender" and match.get("result") == "win" and not match.get("castle_captured")
        )
        decided = wins + losses
        prep_decided = prep_wins + prep_losses

        return {
            "kingdom": kingdom_number,
            "history": matches,
            "latestMatch": matches[0] if matches else None,
            "matchCount": len(matches),
            "wins": wins,
            "losses": losses,
            "undecided": len(matches) - decided,
            "winRate": (wins / decided * 100) if decided else 0,
            "prepWins": prep_wins,
            "prepLosses": prep_losses,
            "prepWinRate": (prep_wins / prep_decided * 100) if prep_decided else 0,
            "attacks": attacks,
            "defenses": defenses,
            "castleCaptures": castle_captures,
            "defensesHeld": defenses_held,
            "currentStreak": cls._current_streak(matches),
            "pagination": pagination or {},
        }

    @staticmethod
    def _count_result(matches: List[Dict[str, Any]], key: str, value: str) -> int:
        return sum(1 for match in matches if match.get(key) == value)

    @staticmethod
    def _current_streak(matches: List[Dict[str, Any]]) -> str:
        if not matches:
            return "N/A"

        latest_result = matches[0].get("result")
        if latest_result not in {"win", "loss"}:
            return "N/A"

        count = 0
        for match in matches:
            if match.get("result") != latest_result:
                break
            count += 1

        prefix = "W" if latest_result == "win" else "L"
        return f"{prefix}{count}"

    @staticmethod
    def _outcome(winner: Optional[int], kingdom_number: int) -> str:
        if winner is None:
            return "unknown"
        return "win" if winner == kingdom_number else "loss"

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _compare_high_value(value_a: Any, value_b: Any, kingdom_a: int, kingdom_b: int) -> Optional[int]:
        """Return winner kingdom for metrics where higher is better."""
        try:
            numeric_a = float(value_a)
            numeric_b = float(value_b)
        except (TypeError, ValueError):
            return None

        if numeric_a > numeric_b:
            return kingdom_a
        if numeric_b > numeric_a:
            return kingdom_b
        return None

    @staticmethod
    def _compare_low_value(value_a: Any, value_b: Any, kingdom_a: int, kingdom_b: int) -> Optional[int]:
        """Return winner kingdom for metrics where lower is better."""
        try:
            numeric_a = float(value_a)
            numeric_b = float(value_b)
        except (TypeError, ValueError):
            return None

        if numeric_a < numeric_b:
            return kingdom_a
        if numeric_b < numeric_a:
            return kingdom_b
        return None
