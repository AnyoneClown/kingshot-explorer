import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "upload_kingshot_heroes.py"
SPEC = importlib.util.spec_from_file_location("upload_kingshot_heroes", SCRIPT_PATH)
uploader = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = uploader
SPEC.loader.exec_module(uploader)


def hero_entity():
    return {
        "entity_type": "hero",
        "slug": "amadeus",
        "name": "Amadeus",
        "data": {
            "class": "infantry",
            "generation": 1,
            "rarity": "Mythic",
            "description": "Kingshot Gen 1 Infantry hero.",
            "sources": ["VIP Package"],
            "conquest": {
                "stats": {"Hero Attack": "2,128"},
                "skills": [
                    {
                        "name": "Skill 1",
                        "description": "Deals Attack * 224% damage.",
                        "progression": {"name": "Damage Up", "values": ["160%", "224%"]},
                    }
                ],
            },
            "expedition": {
                "stats": {"Infantry Attack": "+260.20%"},
                "skills": [],
            },
            "exclusive_gear": {
                "name": "Aegis of Fate",
                "stats": {"Power": "281,250"},
                "skills": [
                    {
                        "name": "Gear Skill 1",
                        "description": "Reduces damage taken by 30%.",
                        "progression": {"name": "Damage Taken Down", "values": ["10%", "30%"]},
                    }
                ],
            },
        },
    }


def test_build_chunks_creates_section_level_knowledge():
    chunks = uploader.build_chunks(hero_entity(), source="https://example.test/heroes/amadeus")

    assert len(chunks) == 6
    assert chunks[0].metadata == {"section": "overview", "slug": "amadeus", "name": "Amadeus"}
    assert "Class: infantry" in chunks[0].content
    assert "Amadeus conquest stats: Hero Attack: 2,128." in chunks[1].content
    assert chunks[2].metadata["kind"] == "skill"
    assert "Damage Up: 160% / 224%" in chunks[2].content
    assert "exclusive gear Aegis of Fate stats" in chunks[4].content
    assert all(chunk.source == "https://example.test/heroes/amadeus" for chunk in chunks)


def test_validate_entity_rejects_non_hero_entity():
    entity = hero_entity()
    entity["entity_type"] = "building"

    try:
        uploader.validate_entity(entity)
    except ValueError as exc:
        assert "Only hero entities" in str(exc)
    else:
        raise AssertionError("validate_entity should reject non-hero entities")
