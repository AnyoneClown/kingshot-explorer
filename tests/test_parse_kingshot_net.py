import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import parse_kingshot_net as parser  # noqa: E402


def page_html(main: str, *, faq=None, payload=None) -> str:
    scripts = ""
    if faq:
        data = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq
            ],
        }
        scripts += f'<script type="application/ld+json">{json.dumps(data)}</script>'
    if payload is not None:
        compact = json.dumps(payload, separators=(",", ":"))  # the real payload has no spaces
        scripts += f"<script>self.__next_f.push([1,{json.dumps(compact)}])</script>"
    return f"<html><body><nav>Home</nav><main>{main}</main>{scripts}</body></html>"


def build(path: str, html: str) -> dict:
    page = parser.parse_page(path, html)
    assert page is not None
    entity = parser.build_entity(page)
    assert entity is not None
    return entity


def contents(entity: dict) -> str:
    return "\n".join(chunk["content"] for chunk in entity["chunks"])


def test_skips_decorative_parts_and_uses_exact_numbers_from_titles():
    html = page_html(
        '<p><span aria-hidden="true"><span>3</span></span><span>Battle</span></p>'
        "<h1>Assault Techniques I</h1>"
        '<h2>Total to max: level 0<svg><path d="M0"/></svg>3</h2>'
        '<p>Bread</p><p><span title="46,700">46.7K</span></p>'
    )
    entity = build("/database/academy-research/assault-techniques-i", html)
    text = contents(entity)
    assert "Bread: 46,700" in text
    assert "level 0 → 3" in text
    assert entity["data"]["tags"] == "Battle"


def test_tables_align_colspan_totals_and_band_rows_and_keep_nested_tables():
    html = page_html(
        "<h1>Governor Gear</h1><h2>Levels and costs</h2>"
        "<table><caption>Gear levels</caption>"
        "<thead><tr><th>Level</th><th>Satin</th><th>Power</th></tr></thead>"
        '<tbody><tr><th colspan="3">Uncommon · 2 levels</th></tr>'
        "<tr><th>Uncommon · 0 stars</th><td>—</td><td>1,000</td></tr>"
        "<tr><th>Mythic T2 · 3 stars</th><td>9,000</td><td>2,000</td></tr>"
        '<tr><td colspan="3"><details><summary>4 steps after Mythic T2 · 3 stars</summary>'
        "<table><thead><tr><th>Level</th><th>Satin</th></tr></thead>"
        "<tbody><tr><th>Status: 1</th><td>12,000</td></tr></tbody></table></details></td></tr>"
        '<tr><th colspan="1">Total</th><td>5,990</td><td>3,000</td></tr></tbody></table>'
    )
    entity = build("/database/governor-gear", html)
    text = contents(entity)
    assert "Levels and costs — Gear levels — Uncommon · 2 levels" in text
    assert "Uncommon · 0 stars: Power: 1,000" in text  # "—" dropped
    assert "Total: Satin: 5,990, Power: 3,000" in text
    assert "4 steps after Mythic T2 · 3 stars (already counted in the next level's cost)" in text
    assert "Mythic T2 · 3 stars: Satin: 9,000, Power: 2,000" in text  # container row is not a band label
    assert "Status: 1: Satin: 12,000" in text


def test_faq_comes_from_json_ld_one_chunk_per_question():
    html = page_html(
        "<p>Mythic Cavalry Generation 7</p><h1>Ava</h1>"
        "<h2>FAQ</h2><h3>Should I invest in Ava?</h3><p>Yes</p>",
        faq=[("Should I invest in Ava?", "Yes, for <b>Bear Hunt</b>."), ("How do I get Ava?", "Hall of Heroes.")],
    )
    entity = build("/heroes/ava", html)
    faq = [c["content"] for c in entity["chunks"] if c["metadata"].get("section") == "FAQ"]
    assert faq == [
        "Ava (Kingshot hero) — FAQ: Should I invest in Ava?: Yes, for Bear Hunt.",
        "Ava (Kingshot hero) — FAQ: How do I get Ava?: Hall of Heroes.",
    ]


def test_hero_skills_use_every_level_from_page_data_and_keep_their_group():
    levels = [
        {"level": n, "description": f"Raises Lethality by {5 * n}%.", "requirement": "$undefined" if n == 1 else f"Requires {n - 1} stars"}
        for n in range(1, 6)
    ]
    html = page_html(
        "<p>Mythic Infantry Generation 5</p><h1>Long Fei</h1>"
        "<h2>Skills</h2><h3>Expedition</h3><h4>Dragon Heart</h4>"
        "<p>Requires 4 stars</p><p>Raises Lethality by 25%.</p>",
        payload=[{"name": "Dragon Heart", "levels": levels, "defaultLevel": 5}],
    )
    entity = build("/heroes/long-fei", html)
    assert entity["slug"] == "longfei"
    assert entity["data"]["class"] == "infantry" and entity["data"]["generation"] == 5
    text = contents(entity)
    assert "Expedition — Dragon Heart: Raises Lethality by 5%/10%/15%/20%/25%." in text
    assert "level 2 needs 1★" in text and "level 5 needs 4★" in text
    assert "Requires 4 stars" not in text


def test_tier_list_lists_heroes_per_tier_including_new_ones():
    tile = '<button type="button" aria-pressed="false" aria-label="{}"><span aria-hidden="true">x</span></button>'
    html = page_html(
        "<h1>Bear Rally Joiner Tier List</h1>"
        "<h3>Tier S</h3><div>" + tile.format("Amadeus, Infantry") + tile.format("Luna, Archer, new this generation")
        + "</div><h3>Tier D</h3><div>" + tile.format("Seth, Infantry") + "</div>"
    )
    entity = build("/hero-tier-list/bear-rally-joiner", html)
    text = contents(entity)
    assert "Tier S: Amadeus (Infantry); Luna (Archer, new in this generation)" in text
    assert "Tier D: Seth (Infantry)" in text


def test_truegold_tiers_get_their_own_slugs():
    html = page_html("<h1>Truegold Requirements</h1><p>Truegold to TG8</p><p>29,927</p>")
    assert build("/database/truegold-requirements?tg=5", html)["slug"] == "truegold-requirements-tg5"
    assert build("/database/truegold-requirements", html)["slug"] == "truegold-requirements-tg1"


def test_chunks_stay_under_the_tool_clip_limit():
    rows = "".join(f"<tr><th>{n}</th><td>{n * 1000}</td><td>{'Academy 5, ' * 3}</td></tr>" for n in range(1, 80))
    html = page_html(
        "<h1>Lion</h1><h2>All pet levels</h2>"
        f"<table><thead><tr><th>Level</th><th>Pet Food</th><th>Requirements</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    entity = build("/database/pets/lion", html)
    assert len(entity["chunks"]) > 3
    assert max(len(c["content"]) for c in entity["chunks"]) <= parser.MAX_CHUNK_CHARS + 10
    assert all("Lion (Kingshot pet)" in c["content"] for c in entity["chunks"])
