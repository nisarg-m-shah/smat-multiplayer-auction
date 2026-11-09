import ast, csv, random
from pathlib import Path
from typing import Iterable

def read_squads_from_auction_py(path: str):
    """Safely read only the literal `squads = {...}` assignment; never execute Auction.py."""
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "squads":
                    return ast.literal_eval(node.value)
    raise ValueError("Could not find a literal `squads = {...}` assignment in Auction.py")

def players_from_squads(squads: dict, base_price: int = 20):
    players = []
    order = 0
    for state_team, data in squads.items():
        names = data.get("name", [])
        roles = data.get("role", [])
        if len(names) != len(roles):
            raise ValueError(f"{state_team}: name and role lists have different lengths")
        for name, role in zip(names, roles):
            if not str(name).strip():
                continue
            players.append({
                "name": str(name).strip(),
                "state_team": str(state_team),
                "role": (str(role).strip().upper() or "UNKNOWN"),
                "base_price": int(base_price),
                "importance_weight": 1.0,
                "source_order": order,
            })
            order += 1
    return players

def load_players(auction_py_path: str, base_price: int = 20):
    squads = read_squads_from_auction_py(auction_py_path)
    return players_from_squads(squads, base_price)

def load_players_csv(path: str):
    """Optional later import. Columns: name,state_team,role,base_price,importance_weight."""
    result = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f)):
            result.append({
                "name": row["name"].strip(),
                "state_team": row.get("state_team", "").strip(),
                "role": row.get("role", "UNKNOWN").strip().upper() or "UNKNOWN",
                "base_price": int(row.get("base_price") or 20),
                "importance_weight": max(0.01, float(row.get("importance_weight") or 1)),
                "source_order": i,
            })
    return result

def weighted_random_order(players: Iterable[dict], seed=None):
    """Weighted random order using exponential-race sampling.
    Larger importance_weight makes a player more likely to appear earlier.
    Weight 1.0 means ordinary random chance.
    """
    rng = random.Random(seed)
    scored = []
    for p in players:
        weight = max(0.01, float(p.get("importance_weight", 1.0)))
        u = max(rng.random(), 1e-12)
        key = -__import__("math").log(u) / weight
        scored.append((key, p))
    return [p for _, p in sorted(scored, key=lambda x: x[0])]
