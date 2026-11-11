"""Kingdom and global ranking boards of the KingShot Data API, ready to show.

``/leaderboard`` reads a board through this service. It knows which boards exist, how
each board's score is read (tower boards carry a band offset, Town Center levels have
Truegold names), and how to name the alliances of an alliance board: those boards carry
only alliance ids, and the API does not resolve them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from services.card_drawing import compact_number

logger = logging.getLogger(__name__)

# The API returns at most this many entries, whatever limit is asked for.
BOARD_LIMIT = 100

# Tower boards (types 21-26) encode the tower in the score: (type - 20) * 10000 + stages.
TOWER_BAND = 10_000

TOWN_CENTER_MAX_PLAIN_LEVEL = 30
TRUEGOLD_STEP = 5


@dataclass(frozen=True)
class Board:
    """One ranking board. ``type`` is the game's board number."""

    type: int
    name: str
    unit: str
    alliance: bool = False
    # Small scores are printed whole (3,019); the rest are shortened (772.55M).
    exact: bool = False
    town_center: bool = False

    @property
    def band(self) -> int:
        return (self.type - 20) * TOWER_BAND if 21 <= self.type <= 26 else 0

    def value(self, score: Any) -> Optional[float]:
        """The number players see in the game, or None when the score is unreadable."""
        number = _number(score)
        if number is None:
            return None
        if self.band and number >= self.band:
            number -= self.band
        return number

    def text(self, value: Optional[float]) -> str:
        if value is None:
            return "–"
        if self.town_center:
            return town_center_label(int(value))
        if self.exact:
            return f"{int(value):,}"
        return compact_number(value)


# In the order they are offered. Types 27 and 28 (a seasonal activity) are left out:
# they are empty outside their season.
BOARDS: tuple[Board, ...] = (
    Board(3, "Personal Power", "power"),
    Board(4, "Kill Count", "kills"),
    Board(1, "Alliance Power", "alliance power", alliance=True),
    Board(2, "Alliance Kills", "alliance kills", alliance=True),
    Board(5, "Town Center Level", "level", town_center=True),
    Board(8, "Heroes' Total Power", "all heroes"),
    Board(7, "Hero Power", "strongest hero"),
    Board(16, "Total Pet Power", "pet power"),
    Board(29, "Master Total Power", "master power"),
    Board(18, "Island Prosperity", "prosperity", exact=True),
    Board(6, "Rebel Conquest", "stage", exact=True),
    Board(20, "Mystic Trial", "total stages", exact=True),
    Board(21, "Coliseum", "stages", exact=True),
    Board(22, "Forest of Life", "stages", exact=True),
    Board(23, "Crystal Cave", "stages", exact=True),
    Board(24, "Knowledge Nexus", "stages", exact=True),
    Board(25, "Molten Fort", "stages", exact=True),
    Board(26, "Radiant Spire", "stages", exact=True),
)
BOARDS_BY_TYPE = {board.type: board for board in BOARDS}
DEFAULT_BOARD = BOARDS[0]

# Player boards whose resolved entries name the strongest alliances for free.
_ALLIANCE_SEED_BOARDS = (BOARDS_BY_TYPE[3], BOARDS_BY_TYPE[4])


def town_center_label(level: int) -> str:
    """``28``, ``30-2``, ``TG3`` or ``TG3-4``: a Town Center level as the game names it.

    Above level 30 every fifth level is a Truegold level and the four between are its
    steps: 35 is TG1, 40 is TG2, 70 is TG8.
    """
    if level <= TOWN_CENTER_MAX_PLAIN_LEVEL:
        return str(level)
    truegold, step = divmod(level - TOWN_CENTER_MAX_PLAIN_LEVEL, TRUEGOLD_STEP)
    base = f"TG{truegold}" if truegold else str(TOWN_CENTER_MAX_PLAIN_LEVEL)
    return f"{base}-{step}" if step else base


def guessed_alliance_kingdom(aid: int) -> Optional[int]:
    """The kingdom an alliance was founded in, read from its id.

    Alliance ids look like ``(kingdom + 9) * 100000 + n`` (seen for kingdoms 240, 271,
    555 and 830). It is a guess: a lookup with a wrong kingdom returns no members.
    """
    kingdom = aid // 100_000 - 9
    return kingdom if kingdom >= 1 else None


@dataclass
class BoardEntry:
    """One row of a board. ``id`` is a player uid, or an alliance id on alliance boards."""

    rank: int
    id: Optional[int]
    value: Optional[float]
    text: str
    name: str = ""
    tag: str = ""
    alliance_id: Optional[int] = None
    # An alliance whose name was looked up already, found or not.
    lookup_done: bool = False


@dataclass
class BoardSnapshot:
    """A board as it was fetched. ``kingdom`` is None for the board of all kingdoms."""

    board: Board
    kingdom: Optional[int]
    entries: list[BoardEntry] = field(default_factory=list)
    error: str = ""

    @property
    def scope(self) -> str:
        return f"Kingdom {self.kingdom}" if self.kingdom is not None else "All kingdoms"

    @property
    def title(self) -> str:
        return f"{self.scope} · {self.board.name}"

    @property
    def top_value(self) -> float:
        return max((entry.value or 0 for entry in self.entries), default=0)


class LeaderboardService:
    """Fetches boards, keeps them for a short while, and names alliances."""

    CACHE_SECONDS = 120.0
    CACHE_SIZE = 64
    ALLIANCE_NAME_CACHE_SIZE = 2048
    ALLIANCE_LOOKUP_CONCURRENCY = 6
    # A page of alliances is shown after this long, named or not.
    ALLIANCE_LOOKUP_SECONDS = 12.0

    def __init__(self, kingshot_data_service, *, clock: Callable[[], float] = time.monotonic):
        self._data = kingshot_data_service
        self._clock = clock
        self._boards: dict[tuple[int, Optional[int]], tuple[float, BoardSnapshot]] = {}
        self._alliance_names: dict[int, tuple[str, str]] = {}

    async def get_board(self, board: Board, kingdom: Optional[int]) -> BoardSnapshot:
        """The board of ``kingdom``, or of all kingdoms when it is None."""
        key = (board.type, kingdom)
        cached = self._boards.get(key)
        if cached is not None and self._clock() - cached[0] < self.CACHE_SECONDS:
            return cached[1]
        snapshot = await self._fetch(board, kingdom)
        if not snapshot.error:
            self._boards.pop(key, None)
            while len(self._boards) >= self.CACHE_SIZE:
                self._boards.pop(next(iter(self._boards)))
            self._boards[key] = (self._clock(), snapshot)
        return snapshot

    async def _fetch(self, board: Board, kingdom: Optional[int]) -> BoardSnapshot:
        snapshot = BoardSnapshot(board=board, kingdom=kingdom)
        # Resolving an alliance board would look its alliance ids up as players.
        resolve = not board.alliance
        try:
            if kingdom is None:
                result = await self._data.get_global_board(board.type, limit=BOARD_LIMIT, resolve=resolve)
            else:
                result = await self._data.get_kingdom_board(board.type, kingdom, limit=BOARD_LIMIT, resolve=resolve)
        except Exception:
            logger.warning("Could not fetch board %s for %s", board.type, snapshot.scope, exc_info=True)
            snapshot.error = "The ranking could not be loaded. Try again in a moment."
            return snapshot
        if not isinstance(result, dict) or not result.get("success"):
            message = result.get("error_message") if isinstance(result, dict) else None
            logger.warning("Board %s for %s failed: %s", board.type, snapshot.scope, message)
            snapshot.error = "The ranking could not be loaded. Try again in a moment."
            return snapshot

        payload = result.get("data")
        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        for index, raw in enumerate(raw_entries if isinstance(raw_entries, list) else []):
            if isinstance(raw, dict):
                snapshot.entries.append(self._entry(board, raw, index))
        if not snapshot.entries:
            refused = isinstance(payload, dict) and payload.get("error")
            if refused and kingdom is not None:
                snapshot.error = f"The game returned no ranking for kingdom {kingdom}. Check the kingdom number."
            else:
                snapshot.error = "This board has no entries right now. Seasonal boards are empty between seasons."
        return snapshot

    def _entry(self, board: Board, raw: dict[str, Any], index: int) -> BoardEntry:
        rank = _positive_int(raw.get("rank")) or index + 1
        entry_id = _positive_int(raw.get("uid"))
        value = board.value(raw.get("score"))
        entry = BoardEntry(rank=rank, id=entry_id, value=value, text=board.text(value))
        if board.alliance:
            known = self._alliance_names.get(entry_id) if entry_id is not None else None
            if known is not None:
                entry.tag, entry.name = known
                entry.lookup_done = True
            return entry
        entry.name = _text(raw.get("name"))
        alliance = raw.get("alliance")
        if isinstance(alliance, dict):
            entry.tag = _text(alliance.get("abbr"))
            entry.alliance_id = _positive_int(alliance.get("aid"))
            if entry.alliance_id is not None:
                self._remember_alliance(entry.alliance_id, entry.tag, _text(alliance.get("name")))
        return entry

    def _remember_alliance(self, aid: int, tag: str, name: str) -> None:
        if not tag and not name:
            return
        self._alliance_names.pop(aid, None)
        while len(self._alliance_names) >= self.ALLIANCE_NAME_CACHE_SIZE:
            self._alliance_names.pop(next(iter(self._alliance_names)))
        self._alliance_names[aid] = (tag, name)

    async def name_alliances(self, snapshot: BoardSnapshot, entries: Sequence[BoardEntry]) -> None:
        """Fill in tag and name for ``entries`` of an alliance board, as far as they can be found.

        The strongest alliances are named by the kingdom's top players, whose resolved
        board entries carry their alliance. The rest are looked up one by one through a
        member's profile, within a time limit: an unnamed alliance still shows its id.
        """
        if not snapshot.board.alliance:
            return
        pending = [entry for entry in entries if not entry.lookup_done and entry.id is not None]
        if not pending:
            return
        if not self._apply_known(pending):
            # Reading the player boards remembers the alliances of their players.
            await asyncio.gather(*(self.get_board(board, snapshot.kingdom) for board in _ALLIANCE_SEED_BOARDS))
        pending = [entry for entry in pending if not self._apply_known([entry])]
        if not pending:
            return

        semaphore = asyncio.Semaphore(self.ALLIANCE_LOOKUP_CONCURRENCY)

        async def look_up(entry: BoardEntry) -> None:
            kingdom = snapshot.kingdom if snapshot.kingdom is not None else guessed_alliance_kingdom(entry.id)
            if kingdom is None:
                return
            async with semaphore:
                identity = await self._data.get_alliance_identity(entry.id, kingdom)
            if isinstance(identity, dict):
                self._remember_alliance(entry.id, _text(identity.get("abbr")), _text(identity.get("name")))

        tasks = [asyncio.create_task(look_up(entry)) for entry in pending]
        _, unfinished = await asyncio.wait(tasks, timeout=self.ALLIANCE_LOOKUP_SECONDS)
        for task in unfinished:
            task.cancel()
        for task, entry in zip(tasks, pending):
            if task in unfinished:
                # Not marked done: the next time this page is opened it is tried again.
                continue
            if task.exception() is not None:
                logger.warning("Could not name alliance %s", entry.id, exc_info=task.exception())
            self._apply_known([entry])
            entry.lookup_done = True

    def _apply_known(self, entries: Sequence[BoardEntry]) -> bool:
        """Name the entries whose alliance is remembered; True when all of them are."""
        complete = True
        for entry in entries:
            known = self._alliance_names.get(entry.id)
            if known is None:
                complete = False
                continue
            entry.tag, entry.name = known
            entry.lookup_done = True
        return complete


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
