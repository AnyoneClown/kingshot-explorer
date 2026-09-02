import asyncio
import hashlib
import time
import urllib.parse
from typing import Any, Dict, Optional

import aiohttp

SALT = "mN4!pQs6JrYwV9"
HOSTNAME = "https://kingshot-giftcode.centurygame.com"
MIN_REQUEST_INTERVAL_SECONDS = 1.0


class KingshotAPIClient:
    """Client for original Kingshot gift code and player API."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_slot_lock = asyncio.Lock()
        self._last_request_started_at: Optional[float] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure session is initialized."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Manually close the session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _sign(self, params: Dict[str, str]) -> str:
        """Sign parameters using MD5 and SALT."""
        sorted_keys = sorted(params.keys())
        query_string = "&".join(f"{k}={params[k]}" for k in sorted_keys)
        string_to_sign = query_string + SALT
        return hashlib.md5(string_to_sign.encode("utf-8")).hexdigest()

    async def _wait_for_request_slot(self) -> None:
        """Keep all CenturyGame HTTP request starts at least one second apart."""
        async with self._request_slot_lock:
            now = time.monotonic()
            if self._last_request_started_at is not None:
                wait_seconds = MIN_REQUEST_INTERVAL_SECONDS - (
                    now - self._last_request_started_at
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                    now = time.monotonic()

            self._last_request_started_at = now

    async def _request(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        """Make a signed POST request to the API."""
        params_with_sign = params.copy()
        params_with_sign["sign"] = self._sign(params)

        body = urllib.parse.urlencode(params_with_sign)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            "Origin": HOSTNAME,
            "Referer": f"{HOSTNAME}/",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
            ),
        }

        url = f"{HOSTNAME}/api{path}"
        session = await self.ensure_session()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self._wait_for_request_slot()
                async with session.post(
                    url, data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 429:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        return {"code": 1, "msg": "API rate limit exceeded (429 Too Many Requests).", "err_code": 429}

                    try:
                        # E.g. {'code': 1, 'msg': 'role not exist.', 'data': [], 'err_code': 40004}
                        return await response.json()
                    except Exception as e:
                        text = await response.text()
                        if response.status != 200:
                            return {"code": 1, "msg": f"HTTP Error {response.status}", "err_code": response.status}
                        raise ValueError(f"Failed to parse JSON response: {text}") from e
            except aiohttp.ClientError as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise ValueError(f"HTTP request failed: {e}") from e
            except Exception as e:
                raise ValueError(f"Unexpected request error: {e}") from e

        return {"code": 1, "msg": "Max retries exceeded.", "err_code": 500}

    async def redeem_code(self, player_id: str, kingdom_id: str, gift_code: str) -> Dict[str, Any]:
        """Redeem a gift code using the current kingdom-aware API payload."""
        timestamp = str(int(time.time()))
        params = {
            "fid": str(player_id),
            "cdk": gift_code,
            "kid": str(kingdom_id),
            "time": timestamp,
        }
        return await self._request("/gift_code", params)
