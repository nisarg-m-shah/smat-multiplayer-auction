from app.seed import players_from_squads, weighted_random_order

def test_squads_pair_names_and_roles():
    data = {"Mumbai": {"name": ["A", "B"], "role": ["BAT", "BOWL"]}}
    players = players_from_squads(data)
    assert players[0]["name"] == "A"
    assert players[0]["role"] == "BAT"
    assert players[1]["state_team"] == "Mumbai"

def test_weighted_order_contains_all_players():
    players = [{"id": 1, "importance_weight": 1}, {"id": 2, "importance_weight": 2}]
    ordered = weighted_random_order(players, seed=1)
    assert {x["id"] for x in ordered} == {1, 2}
