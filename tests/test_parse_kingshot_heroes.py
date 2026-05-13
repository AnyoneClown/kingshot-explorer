import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "parse_kingshot_heroes.py"
SPEC = importlib.util.spec_from_file_location("parse_kingshot_heroes", SCRIPT_PATH)
parser = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = parser
SPEC.loader.exec_module(parser)


def test_parse_generation_hero_page():
    html = """
    <h1>Amadeus</h1>
    <p>Generation 1 \u00b7 Mythic</p>
    <p>Infantry</p>
    <p>Kingshot Gen 1 Infantry hero.</p>
    <h3>Sources</h3><ul><li>VIP Package</li></ul>
    <div><h2>Conquest</h2>
      <div><div class="bg-gray-50"><p>Hero Attack</p><p>2,128</p></div></div>
      <div><img alt="Skill 1" src="/skill.webp"/><p>Deals damage.</p><p>Damage Up: 1% / 2%</p></div>
    </div>
    <div><h2>Expedition</h2>
      <div><div class="bg-gray-50"><p>Infantry Attack</p><p>+260.20%</p></div></div>
    </div>
    <div><h2>Exclusive Gear</h2>
      <h3>Aegis of Fate</h3>
      <div><div class="bg-gray-50"><p>Power</p><p>281,250</p></div></div>
      <div><img alt="Gear Skill 1" src="/gear.webp"/><p>Reduces damage.</p><p>Damage Taken Down: 10% / 30%</p></div>
    </div>
    """

    entity = parser.parse_hero_page("https://www.kingshotguide.org/heroes/amadeus", html)

    assert entity["entity_type"] == "hero"
    assert entity["slug"] == "amadeus"
    assert entity["name"] == "Amadeus"
    assert entity["data"]["class"] == "infantry"
    assert entity["data"]["generation"] == 1
    assert entity["data"]["rarity"] == "Mythic"
    assert entity["data"]["conquest"]["stats"] == {"Hero Attack": "2,128"}
    assert entity["data"]["conquest"]["skills"][0]["progression"]["values"] == ["1%", "2%"]
    assert entity["data"]["exclusive_gear"]["stats"] == {"Power": "281,250"}


def test_parse_non_generation_hero_page():
    html = """
    <h1>Forrest</h1>
    <p>Infantry</p>
    <p>Growth-type R-grade Infantry hero.</p>
    <div><h2>Conquest</h2></div>
    <div><h2>Expedition</h2></div>
    """

    entity = parser.parse_hero_page("https://www.kingshotguide.org/heroes/forrest", html)

    assert entity["slug"] == "forrest"
    assert entity["data"]["class"] == "infantry"
    assert entity["data"]["generation"] is None
    assert entity["data"]["rarity"] == "R"
    assert entity["data"]["description"] == "Growth-type R-grade Infantry hero."
