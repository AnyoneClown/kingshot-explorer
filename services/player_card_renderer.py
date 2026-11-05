"""Pure, thread-safe rendering of the /stats player card.

The card stacks the Arena defense heroes (portrait, hero level, stars, exclusive
weapon, hero gear) above the governor gear strip (six equipment slots, each with
up to three charms). Everything here is CPU-bound pure Python, so callers on the
event loop must run :func:`render_player_card` through ``asyncio.to_thread``.

Static artwork under ``images/`` is decoded and resized once per process and kept
in bounded LRU caches; repeated renders only composite already-decoded pixels.
Unknown or unreadable ids are skipped (heroes, hero gear) or drawn as empty slots
(governor gear, charms) instead of failing the whole card.

Id -> file mapping (verified against the files in ``images/`` and the examples in
``docs/SCHEMAS.md``):

- heroes:            ``images/heroes/{hero id}.png`` (e.g. 50009)
- exclusive weapons
  and hero gear:     ``images/exclusive_weapons/{id}.png`` (e.g. 1050024, 1011501)
- governor gear:     ``images/gear/{equipid}.png``; ids are ``slot * 1000 + tier``
                     (1001-1008 hats ... 6001-6008 staves, so ``equipid`` 1005 is a
                     slot-1 hat). ``images/gear/0{slot}.png`` are the empty-slot
                     silhouettes.
- charms:            ``images/charms/{charm id}.png``; ids are
                     ``troop type * 100 + level`` (1xx infantry/shield,
                     2xx cavalry/horse, 3xx archer/bow; levels 1-22), so charm 201
                     is a level-1 cavalry charm. The level is inferred from the
                     artwork progression, not from an official table.
"""

from __future__ import annotations

import functools
import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

IMAGES_DIR = Path(__file__).resolve().parents[1] / "images"

HERO_MAX_WIDTH = 76
HERO_MAX_HEIGHT = 134
HERO_GEAR_ICON_SIZE = 56
STAR_ICON_SIZE = 20

GEAR_SLOTS = 6
GEAR_ICON_SIZE = 72
CHARM_SLOTS = 3
CHARM_ICON_SIZE = 30
CHARM_GAP = 3
GEAR_COLUMN_SPACING = 8
GEAR_CHARM_TOP_GAP = 4
SECTION_GAP = 14

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RESIZED_CACHE_SIZE = 1024
_ANNOTATED_CACHE_SIZE = 512


@dataclass(frozen=True)
class AssetDirs:
    """Where the static artwork lives. Override in tests."""

    heroes: Path = IMAGES_DIR / "heroes"
    hero_gear: Path = IMAGES_DIR / "exclusive_weapons"
    governor_gear: Path = IMAGES_DIR / "gear"
    charms: Path = IMAGES_DIR / "charms"


@dataclass(frozen=True)
class Image:
    """Immutable RGBA image; safe to share between threads and cache entries."""

    width: int
    height: int
    pixels: bytes


@dataclass(frozen=True)
class PlayerCard:
    """Rendered card plus what made it in, so callers can label missing parts."""

    png: bytes | None
    heroes_drawn: int = 0
    gear_drawn: int = 0


class _Canvas:
    """Mutable RGBA working buffer."""

    __slots__ = ("width", "height", "pixels")

    def __init__(self, width: int, height: int, pixels: bytes | bytearray | None = None):
        self.width = width
        self.height = height
        self.pixels = bytearray(pixels) if pixels is not None else bytearray(width * height * 4)

    @classmethod
    def from_image(cls, image: Image) -> "_Canvas":
        return cls(image.width, image.height, image.pixels)

    def freeze(self) -> Image:
        return Image(self.width, self.height, bytes(self.pixels))


# --------------------------------------------------------------------------- API


def render_player_card(
    heroes: Sequence[Any] | None,
    gear: Sequence[Any] | None,
    assets: AssetDirs | None = None,
) -> PlayerCard:
    """Render Arena heroes and governor gear into one PNG.

    ``heroes`` is the Arena ``heroes[]`` list and ``gear`` the profile ``gear[]``
    list, both as returned by the KingShot Data API. Returns ``PlayerCard(None)``
    when nothing could be drawn.
    """
    assets = assets or AssetDirs()
    sections: list[Image] = []

    loadouts = build_hero_loadouts(heroes or [], assets)
    if loadouts:
        sections.append(build_hero_strip(loadouts))

    gear_strip, gear_drawn = build_gear_strip(gear or [], assets)
    if gear_strip is not None:
        sections.append(gear_strip)

    if not sections:
        return PlayerCard(None)
    image = sections[0] if len(sections) == 1 else _stack_sections(sections)
    return PlayerCard(encode_png(image), heroes_drawn=len(loadouts), gear_drawn=gear_drawn)


def clear_caches() -> None:
    """Drop every cached decoded image (tests, or after replacing artwork)."""
    for cached in (
        _load_resized,
        _hero_portrait,
        _exclusive_icon,
        _hero_gear_icon,
        _star_row,
        _governor_gear_icon,
        _charm_icon,
    ):
        cached.cache_clear()


def charm_level(charm_id: Any) -> int | None:
    """Charm level inferred from its id (``type * 100 + level``)."""
    value = positive_int(charm_id)
    if value is None or value < 100:
        return None
    level = value % 100
    return level or None


# ------------------------------------------------------------------ hero strip


def build_hero_loadouts(heroes: Iterable[Any], assets: AssetDirs) -> list[dict[str, Any]]:
    """Resolve Arena heroes to cached, annotated images; skip unknown ids."""
    loadouts = []
    for hero in sorted(heroes, key=_arena_hero_sort_key):
        if not isinstance(hero, dict):
            continue
        hero_id = hero.get("id")
        if hero_id in (None, ""):
            continue
        portrait = _hero_portrait(str(assets.heroes / f"{hero_id}.png"), _coerce_level(hero.get("lv")))
        if portrait is None:
            continue
        loadouts.append(
            {
                "portrait": portrait,
                "stars": _star_row(_coerce_level(hero.get("star"))),
                "exclusive": _hero_exclusive_image(hero, assets),
                "gear": _hero_gear_images(hero, assets),
            }
        )
    return loadouts


def build_hero_strip(loadouts: list[dict[str, Any]]) -> Image:
    """Lay heroes out in columns: portrait, stars, exclusive weapon, 2x2 gear grid."""
    column_spacing = 10
    gear_spacing = 5
    star_top_gap = 6
    exclusive_top_gap = 8
    gear_top_gap = 8
    icon = HERO_GEAR_ICON_SIZE

    max_hero_height = max(loadout["portrait"].height for loadout in loadouts)
    column_widths = []
    for loadout in loadouts:
        gears = loadout["gear"]
        gear_width = 0
        if gears:
            gear_columns = min(2, len(gears))
            gear_width = gear_columns * icon + gear_spacing * (gear_columns - 1)
        star_width = loadout["stars"].width if loadout["stars"] else 0
        exclusive_width = loadout["exclusive"].width if loadout["exclusive"] else 0
        column_widths.append(max(loadout["portrait"].width, star_width, exclusive_width, gear_width))

    width = sum(column_widths) + column_spacing * (len(column_widths) - 1)
    has_star = any(loadout["stars"] for loadout in loadouts)
    has_exclusive = any(loadout["exclusive"] for loadout in loadouts)
    has_gear = any(loadout["gear"] for loadout in loadouts)
    max_gear_rows = max(((len(loadout["gear"]) + 1) // 2 for loadout in loadouts), default=0)
    gear_grid_height = max_gear_rows * icon + gear_spacing * (max_gear_rows - 1) if max_gear_rows else 0

    height = max_hero_height
    if has_star:
        height += star_top_gap + STAR_ICON_SIZE
    if has_exclusive:
        height += exclusive_top_gap + icon
    if has_gear:
        height += gear_top_gap + gear_grid_height
    canvas = _Canvas(width, height)

    offset_x = 0
    for column_width, loadout in zip(column_widths, loadouts, strict=True):
        portrait = loadout["portrait"]
        _paste(canvas, portrait, offset_x + (column_width - portrait.width) // 2, 0)

        gear_top = max_hero_height
        if has_star:
            stars = loadout["stars"]
            if stars:
                _paste(canvas, stars, offset_x + (column_width - stars.width) // 2, max_hero_height + star_top_gap)
            gear_top += star_top_gap + STAR_ICON_SIZE

        if has_exclusive:
            exclusive = loadout["exclusive"]
            if exclusive:
                _paste(
                    canvas,
                    exclusive,
                    offset_x + (column_width - exclusive.width) // 2,
                    gear_top + exclusive_top_gap,
                )
            gear_top += exclusive_top_gap + icon

        gears = loadout["gear"]
        if gears:
            gear_columns = min(2, len(gears))
            gear_width = gear_columns * icon + gear_spacing * (gear_columns - 1)
            gear_left = offset_x + (column_width - gear_width) // 2
            gear_top += gear_top_gap
            for index, gear_image in enumerate(gears):
                column = index % gear_columns
                row = index // gear_columns
                _paste(
                    canvas,
                    gear_image,
                    gear_left + column * (icon + gear_spacing),
                    gear_top + row * (icon + gear_spacing),
                )

        offset_x += column_width + column_spacing

    return canvas.freeze()


def _hero_exclusive_image(hero: dict[str, Any], assets: AssetDirs) -> Image | None:
    eid = hero.get("exclusive_equip")
    if eid in (None, ""):
        return None
    return _exclusive_icon(str(assets.hero_gear / f"{eid}.png"), _coerce_level(hero.get("exclusive_equip_lv")))


def _hero_gear_images(hero: dict[str, Any], assets: AssetDirs) -> list[Image]:
    equipment = hero.get("equipment")
    if not isinstance(equipment, list):
        return []

    images = []
    for item in sorted((item for item in equipment if isinstance(item, dict)), key=_equipment_sort_key):
        eid = item.get("eid")
        if eid in (None, ""):
            continue
        image = _hero_gear_icon(
            str(assets.hero_gear / f"{eid}.png"),
            _coerce_level(item.get("slv")),
            _coerce_level(item.get("rlv")),
        )
        if image is not None:
            images.append(image)
    return images[:4]


@functools.lru_cache(maxsize=_ANNOTATED_CACHE_SIZE)
def _hero_portrait(path: str, level: int) -> Image | None:
    portrait = _load_resized(path, HERO_MAX_WIDTH, HERO_MAX_HEIGHT, True)
    if portrait is None or level <= 0:
        return portrait
    canvas = _Canvas.from_image(portrait)
    text = f"LV{level}"
    _, badge_height = badge_size(text)
    draw_badge(canvas, 1, canvas.height - badge_height - 1, text, (24, 46, 86, 225), (214, 234, 255, 255))
    return canvas.freeze()


@functools.lru_cache(maxsize=_ANNOTATED_CACHE_SIZE)
def _exclusive_icon(path: str, level: int) -> Image | None:
    image = _load_resized(path, HERO_GEAR_ICON_SIZE, HERO_GEAR_ICON_SIZE, False)
    return annotate_exclusive_image(image, level) if image is not None else None


@functools.lru_cache(maxsize=_ANNOTATED_CACHE_SIZE)
def _hero_gear_icon(path: str, slv: int, rlv: int) -> Image | None:
    image = _load_resized(path, HERO_GEAR_ICON_SIZE, HERO_GEAR_ICON_SIZE, False)
    return annotate_gear_image(image, slv, rlv) if image is not None else None


@functools.lru_cache(maxsize=64)
def _star_row(star: int) -> Image | None:
    return build_star_row_image(star)


def build_star_row_image(star: int) -> Image | None:
    """Five six-part stars; ``star`` counts filled parts (0-30)."""
    if star <= 0:
        return None
    gap = 2
    size = STAR_ICON_SIZE
    width = 5 * size + 4 * gap
    canvas = _Canvas(width, size)
    parts = max(0, min(star, 30))
    for index in range(5):
        _draw_six_part_star(canvas, index * (size + gap), 0, max(0, min(parts - index * 6, 6)))
    return canvas.freeze()


def _draw_six_part_star(canvas: _Canvas, left: int, top: int, filled_parts: int) -> None:
    filled = (255, 238, 142, 245)
    empty = (73, 86, 88, 180)
    outline = (116, 111, 70, 160)
    size = STAR_ICON_SIZE
    center_x = left + size // 2
    center_y = top + size // 2
    petals = (
        (center_x, top + 3),
        (left + size - 4, top + 6),
        (left + size - 4, top + size - 6),
        (center_x, top + size - 3),
        (left + 4, top + size - 6),
        (left + 4, top + 6),
    )
    for index, (x, y) in enumerate(petals):
        _draw_diamond(canvas, x, y, 4, filled if index < filled_parts else empty)
    _draw_diamond(canvas, center_x, center_y, 3, filled if filled_parts else empty)
    for x, y in petals:
        _draw_diamond_outline(canvas, x, y, 4, outline)


def annotate_exclusive_image(image: Image, level: int) -> Image:
    canvas = _Canvas.from_image(image)
    _draw_border(canvas, (245, 190, 68, 235))
    text = f"LV{level}"
    text_width, text_height = badge_size(text)
    draw_badge(
        canvas,
        canvas.width - text_width - 1,
        canvas.height - text_height - 1,
        text,
        (88, 53, 15, 235),
        (255, 237, 176, 255),
    )
    return canvas.freeze()


def annotate_gear_image(image: Image, slv: int, rlv: int) -> Image:
    canvas = _Canvas.from_image(image)
    if slv > 100:
        _tint_visible_pixels(canvas, (180, 24, 32, 255), opacity=0.42)
    general_text, general_background, general_foreground = format_general_gear_level(slv)
    draw_badge(canvas, 1, 1, general_text, general_background, general_foreground)
    mystery_text = f"LV{rlv}"
    mystery_width, mystery_height = badge_size(mystery_text)
    draw_badge(
        canvas,
        canvas.width - mystery_width - 1,
        canvas.height - mystery_height - 1,
        mystery_text,
        (62, 38, 86, 230),
        (232, 214, 255, 255),
    )
    return canvas.freeze()


def format_general_gear_level(slv: int) -> tuple[str, tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Hero gear above +100 wraps into the red stage (+101 shows as red +1)."""
    if slv > 100:
        return f"+{slv - 100}", (115, 28, 34, 235), (255, 235, 220, 255)
    return f"+{slv}", (35, 39, 52, 230), (255, 236, 160, 255)


# ------------------------------------------------------------------ gear strip


def build_gear_strip(gear: Iterable[Any], assets: AssetDirs) -> tuple[Image | None, int]:
    """Six governor equipment slots, each with its charms underneath.

    Returns ``(None, 0)`` when no equipped item has known artwork (hidden gear is
    an empty list; unknown ids fall back to the empty-slot silhouette).
    """
    by_slot: dict[int, dict[str, Any]] = {}
    for item in gear:
        if not isinstance(item, dict):
            continue
        slot = positive_int(item.get("slot"))
        if slot is None or slot > GEAR_SLOTS:
            continue
        by_slot.setdefault(slot, item)
    if not by_slot:
        return None, 0

    columns: list[tuple[Image | None, list[Image | None]]] = []
    drawn = 0
    for slot in range(1, GEAR_SLOTS + 1):
        item = by_slot.get(slot, {})
        equip_id = positive_int(item.get("equipid"))
        icon = _governor_gear_icon(str(assets.governor_gear / f"{equip_id}.png")) if equip_id else None
        if icon is not None:
            drawn += 1
        else:
            if equip_id:
                logger.debug("Governor gear image missing for equipid %s", equip_id)
            icon = _governor_gear_icon(str(assets.governor_gear / f"0{slot}.png"))

        charm_ids = item.get("charms") if isinstance(item.get("charms"), list) else []
        charms: list[Image | None] = []
        for charm_id in charm_ids[:CHARM_SLOTS]:
            parsed = positive_int(charm_id)
            charms.append(_charm_icon(str(assets.charms / f"{parsed}.png"), charm_level(parsed)) if parsed else None)
        charms.extend([None] * (CHARM_SLOTS - len(charms)))
        columns.append((icon, charms))

    if drawn == 0:
        return None, 0

    charm_row_width = CHARM_SLOTS * CHARM_ICON_SIZE + (CHARM_SLOTS - 1) * CHARM_GAP
    column_width = max(GEAR_ICON_SIZE, charm_row_width)
    width = GEAR_SLOTS * column_width + (GEAR_SLOTS - 1) * GEAR_COLUMN_SPACING
    height = GEAR_ICON_SIZE + GEAR_CHARM_TOP_GAP + CHARM_ICON_SIZE
    canvas = _Canvas(width, height)

    for index, (icon, charms) in enumerate(columns):
        left = index * (column_width + GEAR_COLUMN_SPACING)
        if icon is not None:
            _paste(canvas, icon, left + (column_width - icon.width) // 2, 0)
        charm_left = left + (column_width - charm_row_width) // 2
        charm_top = GEAR_ICON_SIZE + GEAR_CHARM_TOP_GAP
        for charm_index, charm in enumerate(charms):
            x = charm_left + charm_index * (CHARM_ICON_SIZE + CHARM_GAP)
            if charm is None:
                _draw_rect(canvas, x + 3, charm_top + 3, CHARM_ICON_SIZE - 6, CHARM_ICON_SIZE - 6, (70, 78, 92, 150))
            else:
                _paste(canvas, charm, x, charm_top)

    return canvas.freeze(), drawn


@functools.lru_cache(maxsize=_ANNOTATED_CACHE_SIZE)
def _governor_gear_icon(path: str) -> Image | None:
    return _load_resized(path, GEAR_ICON_SIZE, GEAR_ICON_SIZE, False)


@functools.lru_cache(maxsize=_ANNOTATED_CACHE_SIZE)
def _charm_icon(path: str, level: int | None) -> Image | None:
    image = _load_resized(path, CHARM_ICON_SIZE, CHARM_ICON_SIZE, False)
    if image is None or not level:
        return image
    canvas = _Canvas.from_image(image)
    text = str(level)
    text_width, text_height = badge_size(text, scale=2)
    draw_badge(
        canvas,
        canvas.width - text_width,
        canvas.height - text_height,
        text,
        (20, 24, 32, 220),
        (255, 255, 255, 255),
        scale=2,
    )
    return canvas.freeze()


def _stack_sections(sections: list[Image]) -> Image:
    """Center sections vertically with a thin separator between them."""
    width = max(section.width for section in sections)
    height = sum(section.height for section in sections) + SECTION_GAP * (len(sections) - 1)
    canvas = _Canvas(width, height)
    top = 0
    for index, section in enumerate(sections):
        if index:
            _draw_rect(canvas, 0, top - SECTION_GAP // 2 - 1, width, 2, (128, 132, 148, 110))
        _paste(canvas, section, (width - section.width) // 2, top)
        top += section.height + SECTION_GAP
    return canvas.freeze()


# ----------------------------------------------------------------- image cache


@functools.lru_cache(maxsize=_RESIZED_CACHE_SIZE)
def _load_resized(path: str, width: int, height: int, fit: bool) -> Image | None:
    """Decode a static PNG once and keep only the resized result.

    ``fit`` keeps the aspect ratio within ``width x height`` (never upscales);
    otherwise the image is stretched to exactly ``width x height``. Missing or
    unreadable files are cached as ``None``.
    """
    file_path = Path(path)
    if not file_path.is_file():
        logger.warning("Player card image missing: %s", file_path)
        return None
    try:
        image = read_png(file_path)
    except (OSError, ValueError, zlib.error, struct.error):
        logger.warning("Player card image unreadable: %s", file_path, exc_info=True)
        return None
    return resize_fit(image, width, height) if fit else resize_nearest(image, width, height)


# ---------------------------------------------------------------- PNG codec


def read_png(path: Path) -> Image:
    return decode_png(path.read_bytes(), name=str(path))


def decode_png(data: bytes, *, name: str = "image") -> Image:
    """Decode a non-interlaced 8-bit RGBA PNG (the only format in ``images/``)."""
    if data[:8] != _PNG_SIGNATURE:
        raise ValueError(f"{name} is not a PNG")

    pos = 8
    width = height = None
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos += 12 + length

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB",
                chunk_data,
            )
            if (bit_depth, color_type, compression, filter_method, interlace) != (8, 6, 0, 0, 0):
                raise ValueError(f"{name} must be non-interlaced 8-bit RGBA PNG")
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    if width is None or height is None:
        raise ValueError(f"{name} is missing IHDR")

    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    pixels = bytearray()
    previous = bytearray(stride)
    source = 0
    for _ in range(height):
        filter_type = raw[source]
        source += 1
        row = bytearray(raw[source : source + stride])
        source += stride
        _unfilter_png_row(row, previous, filter_type, 4)
        pixels.extend(row)
        previous = row

    return Image(width, height, bytes(pixels))


def encode_png(image: Image) -> bytes:
    return write_rgba_png(image.width, image.height, image.pixels)


def write_rgba_png(width: int, height: int, pixels: bytes) -> bytes:
    raw = bytearray()
    stride = width * 4
    for row_index in range(height):
        raw.append(0)
        start = row_index * stride
        raw.extend(pixels[start : start + stride])

    def chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
        checksum = zlib.crc32(chunk_type)
        checksum = zlib.crc32(chunk_data, checksum)
        return struct.pack(">I", len(chunk_data)) + chunk_type + chunk_data + struct.pack(">I", checksum & 0xFFFFFFFF)

    return (
        _PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def _unfilter_png_row(row: bytearray, previous: bytearray, filter_type: int, bytes_per_pixel: int) -> None:
    if filter_type == 0:
        return
    if filter_type == 1:
        for index in range(bytes_per_pixel, len(row)):
            row[index] = (row[index] + row[index - bytes_per_pixel]) & 0xFF
        return
    if filter_type == 2:
        for index in range(len(row)):
            row[index] = (row[index] + previous[index]) & 0xFF
        return
    if filter_type == 3:
        for index in range(len(row)):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            row[index] = (row[index] + ((left + previous[index]) // 2)) & 0xFF
        return
    if filter_type == 4:
        for index in range(len(row)):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            up = previous[index]
            up_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            row[index] = (row[index] + _paeth_predictor(left, up, up_left)) & 0xFF
        return
    raise ValueError(f"Unsupported PNG filter type {filter_type}")


def _paeth_predictor(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_up_left = abs(estimate - up_left)
    if distance_left <= distance_up and distance_left <= distance_up_left:
        return left
    if distance_up <= distance_up_left:
        return up
    return up_left


# -------------------------------------------------------------- pixel helpers


def resize_nearest(image: Image, width: int, height: int) -> Image:
    source_width = image.width
    source_height = image.height
    source_pixels = image.pixels
    resized = bytearray(width * height * 4)
    for y in range(height):
        source_y = min(source_height - 1, y * source_height // height)
        for x in range(width):
            source_x = min(source_width - 1, x * source_width // width)
            src = (source_y * source_width + source_x) * 4
            dst = (y * width + x) * 4
            resized[dst : dst + 4] = source_pixels[src : src + 4]
    return Image(width, height, bytes(resized))


def resize_fit(image: Image, max_width: int, max_height: int) -> Image:
    scale = min(max_width / image.width, max_height / image.height, 1)
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    if width == image.width and height == image.height:
        return image
    return resize_nearest(image, width, height)


def _paste(canvas: _Canvas, image: Image, left: int, top: int) -> None:
    """Copy ``image`` rows into the canvas (no blending; regions never overlap)."""
    row_bytes = image.width * 4
    for row_index in range(image.height):
        src_start = row_index * row_bytes
        dst_start = ((top + row_index) * canvas.width + left) * 4
        canvas.pixels[dst_start : dst_start + row_bytes] = image.pixels[src_start : src_start + row_bytes]


def _blend_pixel(pixels: bytearray, offset: int, color: tuple[int, int, int, int]) -> None:
    src_r, src_g, src_b, src_a = color
    if src_a == 255:
        pixels[offset : offset + 4] = bytes(color)
        return

    dst_r, dst_g, dst_b, dst_a = pixels[offset : offset + 4]
    alpha = src_a / 255
    inv_alpha = 1 - alpha
    out_a = src_a + dst_a * inv_alpha
    if out_a <= 0:
        pixels[offset : offset + 4] = b"\x00\x00\x00\x00"
        return

    pixels[offset] = round((src_r * src_a + dst_r * dst_a * inv_alpha) / out_a)
    pixels[offset + 1] = round((src_g * src_a + dst_g * dst_a * inv_alpha) / out_a)
    pixels[offset + 2] = round((src_b * src_a + dst_b * dst_a * inv_alpha) / out_a)
    pixels[offset + 3] = round(out_a)


def _draw_rect(canvas: _Canvas, x: int, y: int, width: int, height: int, color: tuple[int, int, int, int]) -> None:
    for yy in range(max(0, y), min(canvas.height, y + height)):
        for xx in range(max(0, x), min(canvas.width, x + width)):
            _blend_pixel(canvas.pixels, (yy * canvas.width + xx) * 4, color)


def _draw_border(canvas: _Canvas, color: tuple[int, int, int, int]) -> None:
    _draw_rect(canvas, 0, 0, canvas.width, 2, color)
    _draw_rect(canvas, 0, canvas.height - 2, canvas.width, 2, color)
    _draw_rect(canvas, 0, 0, 2, canvas.height, color)
    _draw_rect(canvas, canvas.width - 2, 0, 2, canvas.height, color)


def _draw_diamond(canvas: _Canvas, center_x: int, center_y: int, radius: int, color: tuple[int, int, int, int]) -> None:
    for y in range(center_y - radius, center_y + radius + 1):
        for x in range(center_x - radius, center_x + radius + 1):
            if abs(x - center_x) + abs(y - center_y) <= radius:
                _draw_rect(canvas, x, y, 1, 1, color)


def _draw_diamond_outline(
    canvas: _Canvas,
    center_x: int,
    center_y: int,
    radius: int,
    color: tuple[int, int, int, int],
) -> None:
    for y in range(center_y - radius, center_y + radius + 1):
        for x in range(center_x - radius, center_x + radius + 1):
            if abs(x - center_x) + abs(y - center_y) == radius:
                _draw_rect(canvas, x, y, 1, 1, color)


def _tint_visible_pixels(canvas: _Canvas, color: tuple[int, int, int, int], *, opacity: float) -> None:
    pixels = canvas.pixels
    tint_r, tint_g, tint_b, _ = color
    opacity = max(0.0, min(1.0, opacity))
    for offset in range(0, len(pixels), 4):
        if pixels[offset + 3] == 0:
            continue
        pixels[offset] = round(pixels[offset] * (1 - opacity) + tint_r * opacity)
        pixels[offset + 1] = round(pixels[offset + 1] * (1 - opacity) + tint_g * opacity)
        pixels[offset + 2] = round(pixels[offset + 2] * (1 - opacity) + tint_b * opacity)


def draw_badge(
    canvas: _Canvas,
    x: int,
    y: int,
    text: str,
    background: tuple[int, int, int, int],
    foreground: tuple[int, int, int, int],
    *,
    scale: int = 3,
) -> None:
    width, height = badge_size(text, scale=scale)
    _draw_rect(canvas, x, y, width, height, background)
    padding_x, padding_y = _badge_padding(scale)
    _draw_text(canvas, x + padding_x, y + padding_y, text, foreground, scale=scale)


def badge_size(text: str, *, scale: int = 3) -> tuple[int, int]:
    padding_x, padding_y = _badge_padding(scale)
    return text_width(text, scale=scale) + padding_x * 2, 5 * scale + padding_y * 2


def _badge_padding(scale: int) -> tuple[int, int]:
    return (2, 1) if scale >= 3 else (1, 1)


def text_width(text: str, *, scale: int) -> int:
    width = 0
    for char in text.upper():
        glyph = _GLYPHS.get(char)
        width += ((len(glyph[0]) if glyph else 1) + 1) * scale
    return max(0, width - scale)


def _draw_text(canvas: _Canvas, x: int, y: int, text: str, color: tuple[int, int, int, int], *, scale: int = 1) -> None:
    cursor = x
    for char in text.upper():
        glyph = _GLYPHS.get(char)
        if glyph is None:
            cursor += 2 * scale
            continue
        for row_index, row in enumerate(glyph):
            for column_index, enabled in enumerate(row):
                if enabled:
                    _draw_rect(canvas, cursor + column_index * scale, y + row_index * scale, scale, scale, color)
        cursor += (len(glyph[0]) + 1) * scale


_GLYPHS: dict[str, tuple[tuple[int, ...], ...]] = {
    "0": ((1, 1, 1), (1, 0, 1), (1, 0, 1), (1, 0, 1), (1, 1, 1)),
    "1": ((0, 1, 0), (1, 1, 0), (0, 1, 0), (0, 1, 0), (1, 1, 1)),
    "2": ((1, 1, 1), (0, 0, 1), (1, 1, 1), (1, 0, 0), (1, 1, 1)),
    "3": ((1, 1, 1), (0, 0, 1), (0, 1, 1), (0, 0, 1), (1, 1, 1)),
    "4": ((1, 0, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1), (0, 0, 1)),
    "5": ((1, 1, 1), (1, 0, 0), (1, 1, 1), (0, 0, 1), (1, 1, 1)),
    "6": ((1, 1, 1), (1, 0, 0), (1, 1, 1), (1, 0, 1), (1, 1, 1)),
    "7": ((1, 1, 1), (0, 0, 1), (0, 1, 0), (0, 1, 0), (0, 1, 0)),
    "8": ((1, 1, 1), (1, 0, 1), (1, 1, 1), (1, 0, 1), (1, 1, 1)),
    "9": ((1, 1, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1), (1, 1, 1)),
    "L": ((1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 1, 1)),
    "M": ((1, 0, 1), (1, 1, 1), (1, 1, 1), (1, 0, 1), (1, 0, 1)),
    "V": ((1, 0, 1), (1, 0, 1), (1, 0, 1), (1, 0, 1), (0, 1, 0)),
    "+": ((0, 0, 0), (0, 1, 0), (1, 1, 1), (0, 1, 0), (0, 0, 0)),
}


# ------------------------------------------------------------------- parsing


def _arena_hero_sort_key(hero: Any) -> tuple[int, int]:
    if not isinstance(hero, dict):
        return (999, 0)
    try:
        slot = int(hero.get("slot") or hero.get("pos") or 999)
    except (TypeError, ValueError):
        slot = 999
    try:
        hero_id = int(hero.get("id") or 0)
    except (TypeError, ValueError):
        hero_id = 0
    return (slot, hero_id)


def _equipment_sort_key(item: dict[str, Any]) -> tuple[int, int]:
    try:
        sid = int(item.get("sid") or 999)
    except (TypeError, ValueError):
        sid = 999
    try:
        eid = int(item.get("eid") or 0)
    except (TypeError, ValueError):
        eid = 0
    return (sid, eid)


def _coerce_level(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
