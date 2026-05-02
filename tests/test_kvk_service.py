from services.kvk_service import KVKService


RAW_MATCHES = [
    {
        "kvk_id": 5075,
        "season_id": 13,
        "kingdom_a": 892,
        "kingdom_b": 830,
        "prep_winner": 892,
        "castle_winner": 830,
        "attacker": 892,
        "defender": 830,
        "castle_captured": False,
        "season_date": "2026-04-25",
        "kvk_title": "KvK #13 (Latest)",
    },
    {
        "kvk_id": 4055,
        "season_id": 12,
        "kingdom_a": 830,
        "kingdom_b": 840,
        "prep_winner": 830,
        "castle_winner": 830,
        "attacker": 830,
        "defender": 840,
        "castle_captured": True,
        "season_date": "2026-03-28",
        "kvk_title": "KvK #12",
    },
    {
        "kvk_id": 3735,
        "season_id": 11,
        "kingdom_a": 739,
        "kingdom_b": 830,
        "prep_winner": 739,
        "castle_winner": 739,
        "attacker": 739,
        "defender": 830,
        "castle_captured": True,
        "season_date": "2026-02-28",
        "kvk_title": "KvK #11",
    },
]


def test_normalize_matches_centers_history_on_requested_kingdom():
    matches = KVKService._normalize_matches(830, RAW_MATCHES)

    assert matches[0]["season_id"] == 13
    assert matches[0]["opponent"] == 892
    assert matches[0]["side"] == "defender"
    assert matches[0]["prepResult"] == "loss"
    assert matches[0]["castleResult"] == "win"

    assert matches[1]["opponent"] == 840
    assert matches[1]["side"] == "attacker"
    assert matches[1]["prepResult"] == "win"
    assert matches[1]["castleResult"] == "win"


def test_build_kingdom_summary_counts_kvk_match_outcomes():
    matches = KVKService._normalize_matches(830, RAW_MATCHES)

    summary = KVKService._build_kingdom_summary(830, matches)

    assert summary["matchCount"] == 3
    assert summary["wins"] == 2
    assert summary["losses"] == 1
    assert summary["winRate"] == 2 / 3 * 100
    assert summary["prepWins"] == 1
    assert summary["prepLosses"] == 2
    assert summary["attacks"] == 1
    assert summary["defenses"] == 2
    assert summary["castleCaptures"] == 1
    assert summary["defensesHeld"] == 1
    assert summary["currentStreak"] == "W2"
    assert summary["latestMatch"]["season_id"] == 13
