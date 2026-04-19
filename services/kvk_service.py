import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class IKVKService(ABC):
    """Interface for KVK (Kingdom vs Kingdom) service."""

    @abstractmethod
    async def get_kingdom_stats(self, kingdom_number: int) -> Dict[str, Any]:
        """Get Nexus KVK stats for a specific kingdom."""
        pass

    @abstractmethod
    async def compare_kingdoms(self, kingdom_a: int, kingdom_b: int) -> Dict[str, Any]:
        """Compare Nexus KVK stats for two kingdoms."""
        pass


class KVKService(IKVKService):
    """Service responsible for fetching Nexus KVK kingdom stats via external API."""

    def __init__(self, api_base_url: str = "https://v2.kingshot.net/api"):
        """
        Initialize KVK service.

        Args:
            api_base_url: Base URL for the KVK API
        """
        self._api_base_url = api_base_url
        self._endpoint_template = f"{api_base_url}/nexus/kingdoms/{{kingdom_number}}"
        self._matches_endpoint_template = "https://kingshot.net/api/kvk/matches?kingdom_a={kingdom_number}"
        self._session: Optional[aiohttp.ClientSession] = None
        logger.info(f"KVKService initialized with endpoint template: {self._endpoint_template}")

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
        Get Nexus KVK stats for a specific kingdom.

        Args:
            kingdom_number: The kingdom number to fetch matches for

        Returns:
            Dictionary containing the API response with kingdom stats
        """
        endpoint = self._endpoint_template.format(kingdom_number=kingdom_number)
        matches_endpoint = self._matches_endpoint_template.format(kingdom_number=kingdom_number)
        logger.info(f"Fetching KVK stats for kingdom {kingdom_number} from {endpoint} and {matches_endpoint}")

        try:
            http_session = await self.ensure_session()

            nexus_task = http_session.get(endpoint, timeout=aiohttp.ClientTimeout(total=10))
            matches_task = http_session.get(matches_endpoint, timeout=aiohttp.ClientTimeout(total=10))

            responses = await asyncio.gather(nexus_task, matches_task, return_exceptions=True)
            nexus_response, matches_response = responses

            if isinstance(nexus_response, Exception):
                raise nexus_response

            response_data = await nexus_response.json(content_type=None)

            if nexus_response.status == 200 and response_data.get("status") == "success":
                data = response_data.get("data", {})
                
                # Fetch and merge matches
                if not isinstance(matches_response, Exception) and matches_response.status == 200:
                    matches_json = await matches_response.json(content_type=None)
                    if matches_json.get("status") == "success":
                        raw_matches = matches_json.get("data", [])
                        history = data.get("history", [])
                        
                        history_map = {item.get("kvk"): item for item in history if item.get("kvk") is not None}
                        merged_history = []
                        
                        for m in raw_matches:
                            kvk_number = m.get("season_id")
                            if not kvk_number:
                                continue
                                
                            if kvk_number in history_map:
                                merged_history.append(history_map.pop(kvk_number))
                            else:
                                kingdom_a = m.get("kingdom_a")
                                kingdom_b = m.get("kingdom_b")
                                opponent = kingdom_b if kingdom_a == kingdom_number else kingdom_a
                                
                                winner = m.get("castle_winner") or m.get("prep_winner")
                                if winner == kingdom_number:
                                    result = "win"
                                elif winner == opponent:
                                    result = "loss"
                                else:
                                    result = "draw"

                                merged_history.append({
                                    "kvk": kvk_number,
                                    "kingdom": kingdom_number,
                                    "opponent": opponent,
                                    "ratingBefore": None,
                                    "ratingAfter": None,
                                    "result": result,
                                    "ratingChange": None
                                })
                        
                        # Add remaining nexus history that wasn't in matches
                        merged_history.extend(history_map.values())
                        # Sort by kvk descending
                        merged_history.sort(key=lambda x: x.get("kvk", 0), reverse=True)
                        data["history"] = merged_history

                logger.info(f"Successfully fetched KVK stats for kingdom {kingdom_number}")
                return {
                    "success": True,
                    "data": data,
                    "message": response_data.get("message", "Kingdom stats retrieved successfully"),
                }

            error_message = response_data.get("message", "Failed to fetch kingdom stats")
            logger.warning(f"Failed to fetch KVK stats for kingdom {kingdom_number}: {error_message}")
            return {
                "success": False,
                "message": error_message,
                "status_code": nexus_response.status,
            }

        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching KVK stats for kingdom {kingdom_number}: {e}", exc_info=True)
            return {
                "success": False,
                "message": "Network error occurred while fetching kingdom stats.",
                "error_code": "NETWORK_ERROR",
            }
        except Exception as e:
            logger.error(
                f"Unexpected error fetching KVK stats for kingdom {kingdom_number}: {e}",
                exc_info=True,
            )
            return {
                "success": False,
                "message": "An unexpected error occurred.",
                "error_code": "UNEXPECTED_ERROR",
            }

    async def compare_kingdoms(self, kingdom_a: int, kingdom_b: int) -> Dict[str, Any]:
        """Compare two kingdoms by fetching both Nexus KVK stat payloads."""
        result_a, result_b = await asyncio.gather(
            self.get_kingdom_stats(kingdom_a),
            self.get_kingdom_stats(kingdom_b),
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

        advantages = {
            "rating": self._compare_high_value(data_a.get("rating"), data_b.get("rating"), kingdom_a, kingdom_b),
            "winRate": self._compare_high_value(data_a.get("winRate"), data_b.get("winRate"), kingdom_a, kingdom_b),
            "wins": self._compare_high_value(data_a.get("wins"), data_b.get("wins"), kingdom_a, kingdom_b),
            "percentile": self._compare_high_value(
                data_a.get("percentile"), data_b.get("percentile"), kingdom_a, kingdom_b
            ),
            "rank": self._compare_low_value(data_a.get("rank"), data_b.get("rank"), kingdom_a, kingdom_b),
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
            },
        }

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
