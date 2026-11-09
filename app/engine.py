from datetime import datetime, timezone, timedelta
from threading import RLock, Lock
from sqlalchemy import select, desc, func
from sqlalchemy.orm import Session
from .models import Room, Franchise, Player, Lot, Bid, Sale, Event
from .seed import weighted_random_order

# One-process lock is a useful guard for this 10-person application.
# Database transactions remain the persistent source of truth.
_room_locks: dict[int, RLock] = {}
_room_locks_guard = Lock()

def lock_for(room_id: int):
    # Protect first-use creation too: two simultaneous first requests must not
    # accidentally receive two different locks for the same room.
    with _room_locks_guard:
        lock = _room_locks.get(room_id)
        if lock is None:
            lock = RLock()
            _room_locks[room_id] = lock
        return lock

def now_utc():
    return datetime.now(timezone.utc)

def seconds_until(value):
    if value is None:
        return 0.0
    # SQLite may return a naive datetime even for timezone=True columns.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value - now_utc()).total_seconds()

def increment_for(current_bid: int) -> int:
    # User's rule: +10 through 200, +20 from 201 through 500, +25 above 500.
    if current_bid < 200:
        return 10
    if current_bid < 500:
        return 20
    return 25

def next_bid_amount(lot: Lot) -> int:
    if lot.current_bid == 0:
        return lot.base_price
    return lot.current_bid + increment_for(lot.current_bid)

def event(db: Session, room_id: int, kind: str, message: str, data=None):
    db.add(Event(room_id=room_id, kind=kind, message=message, data=data or {}))

def get_room(db: Session, code: str):
    room = db.scalar(select(Room).where(Room.code == code.upper()))
    if not room:
        raise ValueError("Room not found")
    return room

def get_franchise_by_token(db: Session, room_id: int, token_hash: str):
    return db.scalar(select(Franchise).where(
        Franchise.room_id == room_id, Franchise.invite_hash == token_hash
    ))

def current_lot(db: Session, room: Room):
    if not room.current_lot_id:
        return None
    return db.get(Lot, room.current_lot_id)

def squad_count(db: Session, franchise_id: int):
    return db.scalar(select(Sale).where(
        Sale.franchise_id == franchise_id, Sale.reversed == False
    ).with_only_columns(Sale.id))  # replaced by count below

def sold_count(db: Session, franchise_id: int):
    # COUNT in PostgreSQL instead of transferring every sale row to Python.
    return db.scalar(select(func.count(Sale.id)).where(
        Sale.franchise_id == franchise_id, Sale.reversed == False
    )) or 0

def purse_reserve(db: Session, room: Room, franchise: Franchise):
    remaining_slots = max(0, room.min_squad - sold_count(db, franchise.id))
    # Reserve the minimum bid for each still-required slot.
    return remaining_slots * 10

def create_queue(db: Session, room: Room, players: list[Player], round_no=1):
    ordered = weighted_random_order([
        {"id": p.id, "importance_weight": p.importance_weight} for p in players
    ])
    by_id = {p.id: p for p in players}
    start_order = db.scalar(select(Lot.queue_order).where(
        Lot.room_id == room.id, Lot.round_no == round_no
    ).order_by(desc(Lot.queue_order)).limit(1))
    offset = (start_order + 1) if start_order is not None else 0
    for i, item in enumerate(ordered):
        p = by_id[item["id"]]
        db.add(Lot(room_id=room.id, player_id=p.id, round_no=round_no,
                   queue_order=offset+i, status="QUEUED", base_price=p.base_price))

def open_next_lot(db: Session, room: Room):
    lot = db.scalar(select(Lot).where(
        Lot.room_id == room.id, Lot.status == "QUEUED"
    ).order_by(Lot.round_no, Lot.queue_order).limit(1))
    if not lot:
        room.current_lot_id = None
        room.status = "ROUND_COMPLETE"
        event(db, room.id, "ROUND_COMPLETE", "No more queued players.")
        return None
    lot.status = "LIVE"
    lot.current_bid = 0
    lot.highest_franchise_id = None
    lot.bid_sequence = 0
    lot.ends_at = now_utc() + timedelta(seconds=room.timer_seconds)
    lot.remaining_ms = None
    room.current_lot_id = lot.id
    room.status = "LIVE"
    event(db, room.id, "PLAYER_STARTED", "Next player is up.", {"lot_id": lot.id})
    return lot

def settle_lot(db: Session, room: Room):
    lot = current_lot(db, room)
    if not lot or lot.status != "LIVE":
        return False
    if lot.ends_at and seconds_until(lot.ends_at) > 0:
        return False
    # A new settled player creates a new history branch and invalidates redo of
    # any previously undone player, regardless of whether that player was sold.
    db.query(Lot).filter(Lot.room_id == room.id, Lot.settlement_redo_available == True).update(
        {Lot.settlement_redo_available: False}, synchronize_session=False
    )
    db.query(Sale).filter(Sale.room_id == room.id, Sale.redo_available == True).update(
        {Sale.redo_available: False}, synchronize_session=False
    )
    if lot.highest_franchise_id is not None:
        franchise = db.get(Franchise, lot.highest_franchise_id)
        player = db.get(Player, lot.player_id)
        franchise.purse_remaining -= lot.current_bid
        sale = Sale(room_id=room.id, lot_id=lot.id, player_id=player.id,
                    franchise_id=franchise.id, price=lot.current_bid)
        db.add(sale)
        lot.status = "SOLD"
        event(db, room.id, "SOLD", f"{player.name} sold to {franchise.name} for {lot.current_bid}.",
              {"player_id": player.id, "franchise_id": franchise.id, "franchise_name": franchise.name, "price": lot.current_bid})
    else:
        player = db.get(Player, lot.player_id)
        lot.status = "UNSOLD"
        event(db, room.id, "UNSOLD", f"{player.name} went unsold.", {"player_id": player.id})
    lot.ends_at = None
    lot.settled_at = now_utc()
    lot.settlement_redo_available = False
    room.current_lot_id = None
    room.status = "BETWEEN_LOTS"
    db.flush()
    return True

class BidRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def place_bid(db: Session, room: Room, franchise: Franchise, amount: int | None = None):
    with lock_for(room.id):
        db.refresh(room)
        if franchise is not None:
            db.refresh(franchise)
        lot = current_lot(db, room)
        if room.status != "LIVE" or not lot or lot.status != "LIVE":
            raise BidRejected("AUCTION_NOT_LIVE", "Bidding is not currently open")
        if not lot.ends_at or seconds_until(lot.ends_at) <= 0:
            raise BidRejected("BID_WINDOW_EXPIRED", "The bidding timer has expired")
        if sold_count(db, franchise.id) >= room.max_squad:
            raise BidRejected("SQUAD_FULL", "Your squad is already full")
        if lot.highest_franchise_id == franchise.id:
            raise BidRejected("ALREADY_HIGHEST_BIDDER", "Your franchise is already the highest bidder")
        expected = next_bid_amount(lot)
        # A bid is the exact amount the participant consciously selected.
        # Never silently turn a stale click into a higher bid.
        if amount is None or int(amount) != expected:
            raise BidRejected("STALE_BID", f"This bid is outdated. The next required bid is {expected}.")
        amount = int(amount)
        # After winning this player, reserve only for the remaining required squad slots.
        slots_after_this_purchase = max(0, room.min_squad - sold_count(db, franchise.id) - 1)
        reserve = slots_after_this_purchase * 10
        if franchise.purse_remaining - amount < reserve:
            raise BidRejected("INSUFFICIENT_RESERVE", "This bid would leave insufficient purse to complete your minimum squad")
        if franchise.purse_remaining < amount:
            raise BidRejected("INSUFFICIENT_PURSE", "Insufficient remaining purse")
        # A new bid after an undo creates a new branch; discard the old redo path.
        db.query(Bid).filter(Bid.lot_id == lot.id, Bid.redo_available == True).update(
            {Bid.redo_available: False}, synchronize_session=False
        )
        lot.current_bid = amount
        lot.highest_franchise_id = franchise.id
        lot.bid_sequence += 1
        db.add(Bid(room_id=room.id, lot_id=lot.id, franchise_id=franchise.id,
                   amount=amount, sequence_no=lot.bid_sequence))
        if room.bid_extension:
            # Add half the configured timer, but never extend beyond one full timer window from now.
            old_remaining = max(0.0, seconds_until(lot.ends_at))
            new_remaining = min(room.timer_seconds, old_remaining + room.timer_seconds / 2)
            lot.ends_at = now_utc() + timedelta(seconds=new_remaining)
        event(db, room.id, "BID", f"{franchise.name} bid {amount}.",
              {"lot_id": lot.id, "franchise_id": franchise.id, "amount": amount})
        room.version += 1
        db.flush()
        return lot

def host_advance(db: Session, room: Room):
    with lock_for(room.id):
        if room.status == "LIVE":
            lot = current_lot(db, room)
            if lot:
                lot.ends_at = now_utc()
                settle_lot(db, room)
        return open_next_lot(db, room)

def pause_room(db: Session, room: Room):
    if room.status != "LIVE":
        raise ValueError("Only a live auction can be paused")
    lot = current_lot(db, room)
    lot.remaining_ms = max(0, int(seconds_until(lot.ends_at) * 1000))
    lot.ends_at = None
    lot.status = "PAUSED"
    room.status = "PAUSED"
    event(db, room.id, "PAUSED", "Auction paused.")

def resume_room(db: Session, room: Room):
    if room.status != "PAUSED":
        raise ValueError("Auction is not paused")
    lot = current_lot(db, room)
    lot.status = "LIVE"
    lot.ends_at = now_utc() + timedelta(milliseconds=lot.remaining_ms or 1000)
    lot.remaining_ms = None
    room.status = "LIVE"
    event(db, room.id, "RESUMED", "Auction resumed.")

def undo_last_sale(db: Session, room: Room):
    """Undo the most recently settled player, whether sold or unsold."""
    if room.status == "COMPLETED":
        raise ValueError("The auction has ended")
    lot = db.scalar(select(Lot).where(
        Lot.room_id == room.id, Lot.status.in_(["SOLD", "UNSOLD"]),
        Lot.settled_at.is_not(None)
    ).order_by(desc(Lot.settled_at), desc(Lot.id)).limit(1))
    if not lot:
        raise ValueError("There is no settled player to undo")
    player = db.get(Player, lot.player_id)
    sale = db.scalar(select(Sale).where(Sale.lot_id == lot.id, Sale.reversed == False).order_by(desc(Sale.id)).limit(1))
    if sale:
        franchise = db.get(Franchise, sale.franchise_id)
        franchise.purse_remaining += sale.price
        sale.reversed = True
        sale.redo_available = True
        for old_bid in db.scalars(select(Bid).where(Bid.lot_id == lot.id, Bid.active == True)).all():
            old_bid.active = False
    lot.status = "QUEUED"
    lot.current_bid = 0
    lot.highest_franchise_id = None
    lot.ends_at = None
    lot.settlement_redo_available = True
    # Do not disturb a different player currently being auctioned.
    if room.current_lot_id is None and room.status not in ("LIVE", "PAUSED"):
        room.status = "BETWEEN_LOTS"
    event(db, room.id, "PLAYER_UNDONE", f"Undo last player: {player.name}.",
          {"lot_id": lot.id, "player_id": player.id, "was_sold": bool(sale),
           "sale_id": sale.id if sale else None})
    db.flush()
    return lot

def undo_last_bid(db: Session, room: Room):
    lot = current_lot(db, room)
    if not lot or lot.status != "LIVE":
        raise ValueError("There is no live player with a bid to undo")
    last = db.scalar(select(Bid).where(
        Bid.lot_id == lot.id, Bid.active == True
    ).order_by(desc(Bid.sequence_no), desc(Bid.id)).limit(1))
    if not last:
        raise ValueError("There are no bids to undo")
    last.active = False
    last.redo_available = True
    # This project deliberately uses autoflush=False. Flush the deactivation
    # before querying again, otherwise SQLAlchemy can return the same bid as
    # "previous" and the first undo appears to do nothing.
    db.flush()
    previous = db.scalar(select(Bid).where(
        Bid.lot_id == lot.id, Bid.active == True
    ).order_by(desc(Bid.sequence_no), desc(Bid.id)).limit(1))
    if previous:
        lot.current_bid = previous.amount
        lot.highest_franchise_id = previous.franchise_id
        lot.bid_sequence = previous.sequence_no
    else:
        lot.current_bid = 0
        lot.highest_franchise_id = None
        lot.bid_sequence = 0
    lot.ends_at = now_utc() + timedelta(seconds=room.timer_seconds)
    event(db, room.id, "BID_UNDONE", "Host removed the most recent bid.",
          {"removed_bid_id": last.id, "restored_bid_id": previous.id if previous else None})
    return last

def redo_last_bid(db: Session, room: Room):
    lot = current_lot(db, room)
    if not lot or lot.status != "LIVE" or room.status != "LIVE":
        raise ValueError("A live player is required to redo a bid")
    bid = db.scalar(select(Bid).where(
        Bid.room_id == room.id, Bid.lot_id == lot.id,
        Bid.active == False, Bid.redo_available == True
    ).order_by(Bid.sequence_no, Bid.id).limit(1))
    if not bid:
        raise ValueError("There is no bid available to redo")
    bid.active = True
    bid.redo_available = False
    lot.current_bid = bid.amount
    lot.highest_franchise_id = bid.franchise_id
    lot.bid_sequence = bid.sequence_no
    lot.ends_at = now_utc() + timedelta(seconds=room.timer_seconds)
    franchise = db.get(Franchise, bid.franchise_id)
    event(db, room.id, "BID_REDONE", f"Host restored {franchise.name}'s bid of {bid.amount}.",
          {"restored_bid_id": bid.id, "franchise_id": franchise.id, "amount": bid.amount})
    db.flush()
    return bid


def redo_last_sale(db: Session, room: Room):
    """Redo the most recently undone settlement (sold OR unsold)."""
    if room.status == "COMPLETED":
        raise ValueError("The auction has ended")
    lot = db.scalar(select(Lot).where(
        Lot.room_id == room.id, Lot.settlement_redo_available == True,
        Lot.status.in_(["QUEUED", "LIVE", "PAUSED"])
    ).order_by(desc(Lot.settled_at), desc(Lot.id)).limit(1))
    if not lot:
        raise ValueError("There is no player available to redo")
    sale = db.scalar(select(Sale).where(Sale.lot_id == lot.id, Sale.reversed == True, Sale.redo_available == True).order_by(desc(Sale.id)).limit(1))
    if sale:
        franchise = db.get(Franchise, sale.franchise_id)
        if franchise.purse_remaining < sale.price:
            raise ValueError("The franchise no longer has enough purse to restore this sale")
        franchise.purse_remaining -= sale.price
        sale.reversed = False
        sale.redo_available = False
        lot.status = "SOLD"
        lot.current_bid = sale.price
        lot.highest_franchise_id = sale.franchise_id
        player = db.get(Player, sale.player_id)
        event(db, room.id, "PLAYER_REDONE", f"Restored {player.name}'s sale to {franchise.name} for {sale.price}.",
              {"lot_id": lot.id, "player_id": player.id, "franchise_id": franchise.id,
               "franchise_name": franchise.name, "price": sale.price, "was_sold": True})
    else:
        lot.status = "UNSOLD"
        lot.current_bid = 0
        lot.highest_franchise_id = None
        player = db.get(Player, lot.player_id)
        event(db, room.id, "PLAYER_REDONE", f"Restored {player.name} as unsold.",
              {"lot_id": lot.id, "player_id": player.id, "was_sold": False})
    lot.ends_at = None
    lot.remaining_ms = None
    lot.settlement_redo_available = False
    # If the undone player had already been reopened as the current lot,
    # advance immediately after restoring its settlement.
    if room.current_lot_id == lot.id:
        room.current_lot_id = None
        room.status = "BETWEEN_LOTS"
    db.flush()
    return lot

def nominated_unsold(db: Session, room: Room, player_ids: list[int], price_overrides=None):
    """Skip remaining queued players and immediately begin a selected unsold-only round."""
    if room.status in ("SETUP", "COMPLETED"):
        raise ValueError("Start the auction before creating an unsold-only round")
    price_overrides = price_overrides or {}
    eligible = {}
    latest_by_player = db.scalars(select(Lot).where(
        Lot.room_id == room.id
    ).order_by(desc(Lot.id))).all()
    for candidate in latest_by_player:
        if candidate.player_id not in eligible and candidate.status == "UNSOLD":
            eligible[candidate.player_id] = candidate
    selected = list(dict.fromkeys(pid for pid in player_ids if pid in eligible))
    if not selected:
        raise ValueError("Select at least one eligible unsold player")

    # Settle the player currently on screen immediately, preserving the bid winner.
    # If that player becomes unsold, include them automatically in this unsold-only round.
    if room.current_lot_id:
        active = db.get(Lot, room.current_lot_id)
        if active and active.status in ("LIVE", "PAUSED"):
            active.status = "LIVE"
            active.ends_at = now_utc()
            settle_lot(db, room)
            if active.status == "UNSOLD" and active.player_id not in selected:
                selected.append(active.player_id)
                eligible[active.player_id] = active

    skipped = db.query(Lot).filter(Lot.room_id == room.id, Lot.status == "QUEUED").all()
    for queued in skipped:
        queued.status = "SKIPPED"

    max_round = db.scalar(select(Lot.round_no).where(Lot.room_id == room.id).order_by(desc(Lot.round_no)).limit(1)) or 1
    new_round = max_round + 1
    for i, pid in enumerate(selected):
        old_lot = eligible[pid]
        price = int(price_overrides.get(str(pid), old_lot.base_price))
        db.add(Lot(room_id=room.id, player_id=pid, round_no=new_round, queue_order=i,
                   status="QUEUED", base_price=price))
    event(db, room.id, "UNSOLD_NOMINATED",
          f"Unsold-only round started with {len(selected)} players; {len(skipped)} queued players skipped.",
          {"player_ids": selected, "skipped_lots": len(skipped), "round_no": new_round})
    room.current_lot_id = None
    room.status = "BETWEEN_LOTS"
    db.flush()
    open_next_lot(db, room)


def recover_interrupted_rooms(db: Session):
    """Pause live rooms after restart without changing accepted bids or sales."""
    rooms = db.scalars(select(Room).where(Room.status == "LIVE")).all()
    recovered = []
    for room in rooms:
        lot = current_lot(db, room)
        if lot is not None and lot.status == "LIVE":
            # The timer intentionally restarts at its configured full duration.
            lot.status = "PAUSED"
            lot.ends_at = None
            lot.remaining_ms = room.timer_seconds * 1000
            room.status = "PAUSED"
            room.version += 1
            event(db, room.id, "RECOVERED_PAUSED",
                  "Server restarted. Auction state recovered and paused; resume starts a fresh timer.",
                  {"lot_id": lot.id, "timer_seconds": room.timer_seconds,
                   "current_bid": lot.current_bid,
                   "highest_franchise_id": lot.highest_franchise_id})
        else:
            # Fail closed if the active lot cannot be verified.
            room.status = "PAUSED"
            room.version += 1
            event(db, room.id, "RECOVERY_REVIEW_REQUIRED",
                  "Server restarted; active lot needs host review before bidding can continue.")
        recovered.append(room.code)
    db.commit()
    return recovered

def snapshot(db: Session, room: Room):
    franchises = db.scalars(select(Franchise).where(
        Franchise.room_id == room.id
    ).order_by(Franchise.created_order)).all()
    players = {p.id: p for p in db.scalars(select(Player).where(Player.room_id == room.id)).all()}
    lots = db.scalars(select(Lot).where(Lot.room_id == room.id).order_by(Lot.round_no, Lot.queue_order)).all()
    sales = db.scalars(select(Sale).where(Sale.room_id == room.id, Sale.reversed == False)).all()
    events = db.scalars(select(Event).where(Event.room_id == room.id).order_by(desc(Event.id)).limit(30)).all()
    bids = []
    live = current_lot(db, room)
    if live:
        # Fetch active and redoable bids in one round trip; derive history flags below.
        bids = db.scalars(select(Bid).where(
            Bid.lot_id == live.id,
            (Bid.active == True) | (Bid.redo_available == True)
        ).order_by(Bid.sequence_no)).all()
    active_bids = [b for b in bids if b.active]
    redoable_bid = any((not b.active) and b.redo_available for b in bids)
    undoable_sale = any(l.status in ("SOLD", "UNSOLD") and l.settled_at is not None for l in lots)
    redoable_sale = any(l.settlement_redo_available and l.status in ("QUEUED", "LIVE", "PAUSED") for l in lots)
    sale_by_franchise = {}
    for s in sales:
        sale_by_franchise.setdefault(s.franchise_id, []).append({
            "player": players[s.player_id].name, "role": players[s.player_id].role,
            "state_team": players[s.player_id].state_team, "price": s.price
        })
    current_player = None
    if live:
        p = players[live.player_id]
        current_player = {
            "lot_id": live.id, "player_id": p.id, "name": p.name,
            "role": p.role, "state_team": p.state_team, "base_price": live.base_price,
            "current_bid": live.current_bid, "highest_franchise_id": live.highest_franchise_id,
            "highest_franchise": next((f.name for f in franchises if f.id == live.highest_franchise_id), None),
            # SQLite may strip tzinfo when reading DateTime(timezone=True).
            # All auction deadlines are created in UTC, so explicitly label them UTC
            # before sending them to browsers in different time zones.
            "ends_at": (live.ends_at.replace(tzinfo=timezone.utc) if live.ends_at.tzinfo is None else live.ends_at.astimezone(timezone.utc)).isoformat().replace("+00:00", "Z") if live.ends_at else None,
            "status": live.status, "round_no": live.round_no,
            "next_bid": next_bid_amount(live)
        }
    latest_lot_by_player = {}
    for candidate in sorted(lots, key=lambda item: item.id, reverse=True):
        latest_lot_by_player.setdefault(candidate.player_id, candidate)
    current_unsold = [candidate for candidate in latest_lot_by_player.values() if candidate.status == "UNSOLD"]
    return {
        "room": {"code": room.code, "name": room.name, "status": room.status,
                 "purse": room.purse, "timer_seconds": room.timer_seconds,
                 "min_squad": room.min_squad, "max_squad": room.max_squad,
                 "version": room.version, "host_franchise_id": room.host_franchise_id},
        "franchises": [{
            "id": f.id, "name": f.name, "logo_url": f.logo_url,
            "purse_remaining": f.purse_remaining,
            "squad": sale_by_franchise.get(f.id, []),
            "squad_count": len(sale_by_franchise.get(f.id, []))
        } for f in franchises],
        "current_player": current_player,
        "queue_remaining": sum(1 for l in lots if l.status == "QUEUED"),
        "unsold": [{
            "player_id": l.player_id, "name": players[l.player_id].name,
            "role": players[l.player_id].role, "base_price": l.base_price
        } for l in current_unsold],
        "events": [{"id": e.id, "kind": e.kind, "message": e.message,
                    "data": e.data or {}, "created_at": e.created_at.isoformat()} for e in events],
        "recent_bids": [{"franchise": next((f.name for f in franchises if f.id == b.franchise_id), "?"),
                         "amount": b.amount, "sequence": b.sequence_no} for b in active_bids],
        "history": {
            "can_undo_bid": bool(live and active_bids and room.status == "LIVE"),
            "can_redo_bid": bool(live and room.status == "LIVE" and redoable_bid),
            "can_undo_sale": bool(room.status != "COMPLETED" and undoable_sale),
            "can_redo_sale": bool(room.status != "COMPLETED" and redoable_sale)
        }
    }
