import os, secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from threading import RLock, Lock
from sqlalchemy import select, desc, func, exists, and_, or_, update, insert, literal, true, JSON, Integer, Boolean, DateTime, String
from sqlalchemy.orm import Session, aliased
from sqlalchemy.orm.attributes import set_committed_value
from . import config
from .models import Room, Franchise, Player, Lot, Bid, Sale, Event
from .seed import weighted_random_order

# One-process lock is a useful guard for this 10-person application.
# Database transactions remain the persistent source of truth.
# The lock is keyed by ROOM CODE (not room id) so a request can take it before it
# has read anything from the database. Everything that changes room state (bids,
# timer settlement, host actions) must hold it from the state read to the commit.
_room_locks: dict = {}
_room_locks_guard = Lock()

def lock_for(room_key):
    key = room_key.upper() if isinstance(room_key, str) else room_key
    with _room_locks_guard:  # dict creation must itself be thread-safe
        lock = _room_locks.get(key)
        if lock is None:
            lock = _room_locks[key] = RLock()
        return lock

def now_utc():
    return datetime.now(timezone.utc)

def iso_utc(value):
    """UTC ISO-8601 with a trailing Z. SQLite may return naive datetimes; all deadlines are UTC."""
    if value is None:
        return None
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")

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

def next_bid_for(current_bid: int, base_price: int) -> int:
    if current_bid == 0:
        return base_price
    return current_bid + increment_for(current_bid)

def next_bid_amount(lot: Lot) -> int:
    return next_bid_for(lot.current_bid, lot.base_price)

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
              {"player_id": player.id, "player_name": player.name, "franchise_id": franchise.id, "franchise_name": franchise.name, "price": lot.current_bid})
    else:
        player = db.get(Player, lot.player_id)
        lot.status = "UNSOLD"
        event(db, room.id, "UNSOLD", f"{player.name} went unsold.", {"player_id": player.id, "player_name": player.name})
    lot.ends_at = None
    lot.settled_at = now_utc()
    lot.settlement_redo_available = False
    room.current_lot_id = None
    room.status = "BETWEEN_LOTS"
    db.flush()
    return True

# ---------------------------------------------------------------------------
# Bidding
#
# A bid is an instruction to buy the CURRENT player at an EXACT amount that the
# bidder saw on screen. The server never changes that amount: if it is not
# exactly the price required right now, the bid is rejected (STALE_BID) and the
# bidder is told the new price so they can decide again.
# ---------------------------------------------------------------------------

class BidRejected(ValueError):
    """A bid that failed validation. Subclasses ValueError so older callers still work."""
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class BidContext:
    """Everything needed to validate (and describe) one bid, read in ONE query."""
    room: Room
    lot: Lot | None
    franchise: Franchise | None
    is_host: bool
    sold: int = 0
    has_redo: bool = False
    leader_name: str | None = None
    player_name: str | None = None


def load_bid_context(db: Session, code: str, hashed_token: str) -> BidContext:
    """Authenticate the token AND read the live auction state in a single round trip.

    The franchise is derived from the credential inside the database query, never
    from anything the browser claims: a franchise token matches its own franchise
    row; the host token matches the host's chosen franchise (if any).
    Must be called while holding the room lock so the state read is current.
    """
    Leader = aliased(Franchise)
    sold_sq = (select(func.count(Sale.id))
               .where(Sale.franchise_id == Franchise.id, Sale.reversed == False)  # noqa: E712
               .correlate(Franchise).scalar_subquery())
    redo_sq = (exists().where(Bid.lot_id == Lot.id, Bid.redo_available == True)  # noqa: E712
               .correlate(Lot))
    stmt = (
        select(Room, Lot, Franchise, Leader.name, Player.name, sold_sq, redo_sq)
        .select_from(Room)
        .outerjoin(Lot, Lot.id == Room.current_lot_id)
        .outerjoin(Franchise, and_(
            Franchise.room_id == Room.id,
            or_(Franchise.invite_hash == hashed_token,
                and_(Room.host_token_hash == hashed_token, Franchise.id == Room.host_franchise_id))))
        .outerjoin(Leader, Leader.id == Lot.highest_franchise_id)
        .outerjoin(Player, Player.id == Lot.player_id)
        .where(Room.code == code.upper())
        .limit(1)
    )
    row = db.execute(stmt).first()
    if row is None:
        raise AuthError(404, "Room not found")
    room, lot, franchise, leader_name, player_name, sold, has_redo = row
    is_host = secrets.compare_digest(room.host_token_hash, hashed_token)
    if not is_host and franchise is None:
        raise AuthError(401, "Invalid invite token")
    return BidContext(room=room, lot=lot, franchise=franchise, is_host=is_host,
                      sold=int(sold or 0), has_redo=bool(has_redo),
                      leader_name=leader_name, player_name=player_name)


def required_amount(ctx: BidContext) -> int | None:
    return next_bid_amount(ctx.lot) if ctx.lot is not None else None


def validate_bid(ctx: BidContext, amount: int, *, lot_id: int | None = None,
                 player_id: int | None = None, franchise_id: int | None = None,
                 received_at=None) -> int:
    """Pure validation against the latest committed state. Returns the required amount.

    Raises BidRejected(code, ...) with a distinguishable code; never adjusts `amount`.
    """
    room, lot, franchise = ctx.room, ctx.lot, ctx.franchise
    if franchise is None:
        raise BidRejected("FRANCHISE_NOT_AUTHORISED",
                          "Select your franchise in Host controls before bidding.", 403)
    if franchise_id is not None and int(franchise_id) != franchise.id:
        raise BidRejected("FRANCHISE_NOT_AUTHORISED",
                          "This request does not match the franchise your invite link controls.", 403)
    settled_like = ("BETWEEN_LOTS", "ROUND_COMPLETE", "COMPLETED")
    if lot is None:
        if lot_id is not None and room.status in settled_like:
            raise BidRejected("PLAYER_ALREADY_SETTLED", "This player has already been settled.")
        raise BidRejected("AUCTION_NOT_LIVE", "Bidding is not currently open")
    if (lot_id is not None and int(lot_id) != lot.id) or (player_id is not None and int(player_id) != lot.player_id):
        raise BidRejected("PLAYER_ALREADY_SETTLED",
                          "That player has already been settled; a different player is now up.")
    if room.status != "LIVE" or lot.status != "LIVE":
        raise BidRejected("AUCTION_NOT_LIVE", "Bidding is not currently open")
    if not lot.ends_at:
        raise BidRejected("TIMER_EXPIRED", "The bidding timer has expired")
    # Judge by ARRIVAL time (taken when the request reached the server) plus the grace window, so a bid
    # pressed with a fraction of a second left is not lost to network or processing time.
    ends = lot.ends_at if lot.ends_at.tzinfo else lot.ends_at.replace(tzinfo=timezone.utc)
    ref = received_at or now_utc()
    if (ends - ref).total_seconds() + config.BID_GRACE_SECONDS <= 0:
        raise BidRejected("TIMER_EXPIRED", "The bidding timer has expired")
    if ctx.sold >= room.max_squad:
        raise BidRejected("SQUAD_FULL", "Your squad is already full")
    if lot.highest_franchise_id == franchise.id:
        raise BidRejected("ALREADY_HIGHEST_BIDDER", "Your franchise is already the highest bidder")
    expected = next_bid_amount(lot)
    if amount != expected:
        raise BidRejected("STALE_BID",
                          f"Price changed: {expected} is now required (you submitted {amount}). "
                          f"Review the latest bid and bid again if you still want this player.")
    if franchise.purse_remaining < amount:
        raise BidRejected("INSUFFICIENT_PURSE", "Insufficient remaining purse")
    # After winning this player, reserve only for the remaining required squad slots.
    reserve = max(0, room.min_squad - ctx.sold - 1) * 10
    if franchise.purse_remaining - amount < reserve:
        raise BidRejected("INSUFFICIENT_PURSE",
                          "This bid would leave insufficient purse to complete your minimum squad")
    return expected


def compact_state(ctx: BidContext, *, server_time=None) -> dict:
    """Small description of the live bidding state (used in 409 responses)."""
    lot, room = ctx.lot, ctx.room
    return {
        "room_version": room.version, "room_status": room.status,
        "lot_id": lot.id if lot else None, "player_id": lot.player_id if lot else None,
        "player_name": ctx.player_name if lot else None,
        "highest_bid": lot.current_bid if lot else None,
        "leading_franchise_id": lot.highest_franchise_id if lot else None,
        "leading_franchise_name": ctx.leader_name if lot else None,
        "next_bid_amount": next_bid_amount(lot) if lot else None,
        "timer_deadline": iso_utc(lot.ends_at) if lot else None,
        "server_time": iso_utc(server_time or now_utc()),
    }



# PostgreSQL only: perform the lot UPDATE, room version bump, bid INSERT and event INSERT as ONE
# statement (data-modifying CTEs) = one network round trip instead of four. The bid and event rows
# are inserted FROM the lot UPDATE's RETURNING row, so if the guarded lot UPDATE matches nothing
# (superseded bid) nothing at all is written. Disable with BID_FUSED_WRITE=0 to fall back to the
# portable four-statement path (always used on SQLite).
FUSED_WRITE = os.getenv("BID_FUSED_WRITE", "1") != "0"


def _fused_bid_write(db: Session, ctx: "BidContext", amount: int, new_seq: int, new_ends, now, message: str):
    lot, room, franchise = ctx.lot, ctx.room, ctx.franchise
    ts = DateTime(timezone=True)
    upd_lot = (update(Lot)
               .where(Lot.id == lot.id, Lot.bid_sequence == lot.bid_sequence, Lot.status == "LIVE")
               .values(current_bid=amount, highest_franchise_id=franchise.id, bid_sequence=new_seq, ends_at=new_ends)
               .returning(Lot.id).cte("upd_lot"))
    upd_room = (update(Room)
                .where(Room.id == room.id, exists(select(upd_lot.c.id)))
                .values(version=Room.version + 1)
                .returning(Room.version).cte("upd_room"))
    ins_bid = (insert(Bid).from_select(
        ["room_id", "lot_id", "franchise_id", "amount", "sequence_no", "accepted_at", "active", "redo_available"],
        select(literal(room.id, Integer), upd_lot.c.id, literal(franchise.id, Integer), literal(amount, Integer),
               literal(new_seq, Integer), literal(now, ts), literal(True, Boolean), literal(False, Boolean))
        .select_from(upd_lot)).returning(Bid.id).cte("ins_bid"))
    ins_event = (insert(Event).from_select(
        ["room_id", "kind", "message", "data", "created_at"],
        select(literal(room.id, Integer), literal("BID", String), literal(message, String),
               literal({"lot_id": lot.id, "franchise_id": franchise.id, "amount": amount}, JSON),
               literal(now, ts))
        .select_from(upd_lot)).returning(Event.id).cte("ins_event"))
    stmt = (select(upd_room.c.version, ins_event.c.id.label("event_id"))
            .select_from(upd_lot)
            .join(upd_room, true()).join(ins_bid, true()).join(ins_event, true()))
    return db.execute(stmt).first()


def apply_bid(db: Session, ctx: BidContext, amount: int) -> dict:
    """Write an already-validated bid. Does NOT commit. Returns the BID_ACCEPTED event.

    Round trips: [redo reset, only if one exists] + lot UPDATE + room UPDATE + bid INSERT + event INSERT.
    The lot UPDATE is guarded (WHERE bid_sequence = <value we validated against>), so even if
    something outside this process's lock changed the lot, a bid can never overwrite a newer one:
    zero rows matched => STALE_BID and the transaction is rolled back by the caller.
    """
    room, lot, franchise = ctx.room, ctx.lot, ctx.franchise
    old_seq, new_seq = lot.bid_sequence, lot.bid_sequence + 1
    now = now_utc()
    new_ends = lot.ends_at
    if room.bid_extension:
        # Add half the configured timer, but never extend beyond one full timer window from now.
        old_remaining = max(0.0, seconds_until(lot.ends_at))
        new_remaining = min(room.timer_seconds, old_remaining + room.timer_seconds / 2)
        new_ends = now + timedelta(seconds=new_remaining)
    if ctx.has_redo:
        # A new bid after an undo creates a new branch; discard the old redo path.
        db.execute(update(Bid).where(Bid.lot_id == lot.id, Bid.redo_available == True)  # noqa: E712
                   .values(redo_available=False))
    message = f"{franchise.name} bid {amount}."
    if FUSED_WRITE and db.get_bind().dialect.name == "postgresql":
        row = _fused_bid_write(db, ctx, amount, new_seq, new_ends, now, message)
        if row is None:
            raise BidRejected("STALE_BID", "The bid was superseded by a newer bid. Review the latest price.")
        new_version, event_id, event_created = row.version, row.event_id, now
    else:
        result = db.execute(
            update(Lot).where(Lot.id == lot.id, Lot.bid_sequence == old_seq, Lot.status == "LIVE")
            .values(current_bid=amount, highest_franchise_id=franchise.id, bid_sequence=new_seq, ends_at=new_ends))
        if result.rowcount != 1:
            raise BidRejected("STALE_BID", "The bid was superseded by a newer bid. Review the latest price.")
        bump = update(Room).where(Room.id == room.id).values(version=Room.version + 1)
        if db.get_bind().dialect.update_returning:
            new_version = db.execute(bump.returning(Room.version)).scalar_one()
        else:
            db.execute(bump)
            new_version = room.version + 1
        bid_row = Bid(room_id=room.id, lot_id=lot.id, franchise_id=franchise.id, amount=amount, sequence_no=new_seq,
                      accepted_at=now)
        event_row = Event(room_id=room.id, kind="BID", message=message, created_at=now,
                          data={"lot_id": lot.id, "franchise_id": franchise.id, "amount": amount})
        db.add_all([bid_row, event_row])
        db.flush()
        event_id, event_created = event_row.id, now
    # Keep long-lived ORM objects (used by callers that reuse the session) consistent
    # without scheduling another UPDATE.
    for obj, attr, value in ((lot, "current_bid", amount), (lot, "highest_franchise_id", franchise.id),
                             (lot, "bid_sequence", new_seq), (lot, "ends_at", new_ends),
                             (room, "version", new_version)):
        set_committed_value(obj, attr, value)
    return {
        "type": "BID_ACCEPTED",
        "room_version": new_version,
        "lot_id": lot.id, "player_id": lot.player_id, "player_name": ctx.player_name,
        "highest_bid": amount,
        "leading_franchise_id": franchise.id, "leading_franchise_name": franchise.name,
        "next_bid_amount": next_bid_for(amount, lot.base_price),
        "timer_deadline": iso_utc(new_ends),
        "bid_sequence": new_seq,
        "server_time": iso_utc(now),
        "event": {"id": event_id, "kind": "BID", "message": message,
                  "created_at": event_created.isoformat()},
    }


def place_bid(db: Session, room: Room, franchise: Franchise, amount: int, *,
              lot_id: int | None = None):
    """Object-based entry point (used by unit tests and scripts). The HTTP path uses
    load_bid_context + validate_bid + apply_bid directly to minimise round trips."""
    if amount is None:
        raise BidRejected("STALE_BID", "An exact bid amount is required", 422)
    with lock_for(room.code):
        db.refresh(room)
        if franchise is not None:
            db.refresh(franchise)
        lot = current_lot(db, room)
        ctx = BidContext(
            room=room, lot=lot, franchise=franchise, is_host=False,
            sold=sold_count(db, franchise.id) if franchise is not None else 0,
            has_redo=bool(lot and db.scalar(select(exists().where(Bid.lot_id == lot.id, Bid.redo_available == True)))),  # noqa: E712
            leader_name=(db.scalar(select(Franchise.name).where(Franchise.id == lot.highest_franchise_id))
                         if lot and lot.highest_franchise_id else None),
            player_name=(db.scalar(select(Player.name).where(Player.id == lot.player_id)) if lot else None),
        )
        validate_bid(ctx, amount, lot_id=lot_id)
        apply_bid(db, ctx, amount)
        return lot


def host_advance(db: Session, room: Room):
    with lock_for(room.code):
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
    # Pausing never leaves less than half the timer, so everyone has time to settle after the pause
    # (lag, a fix, a reconnect) instead of resuming into the last second.
    floor_ms = int(room.timer_seconds * 500)
    lot.remaining_ms = max(floor_ms, int(seconds_until(lot.ends_at) * 1000))
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
    """Undo the most recently settled player, whether sold or unsold.

    The player is put straight back on the block EXACTLY where the hammer fell: same bids, same
    highest bidder, same price, with a fresh timer. If the next player had already been opened
    (or is paused on screen) that player is returned to the front of the queue, untouched by the
    undo, and comes up next once the restored player has been settled again."""
    if room.status == "COMPLETED":
        raise ValueError("The auction has ended")
    lot = db.scalar(select(Lot).where(
        Lot.room_id == room.id, Lot.status.in_(["SOLD", "UNSOLD"]),
        Lot.settled_at.is_not(None)
    ).order_by(desc(Lot.settled_at), desc(Lot.id)).limit(1))
    if not lot:
        raise ValueError("There is no settled player to undo")
    player = db.get(Player, lot.player_id)
    was_paused = room.status == "PAUSED"

    # 1. A different player already on screen goes back to the front of the queue.
    displaced = None
    if room.current_lot_id and room.current_lot_id != lot.id:
        cur = db.get(Lot, room.current_lot_id)
        if cur is not None and cur.status in ("LIVE", "PAUSED"):
            for b in db.scalars(select(Bid).where(Bid.lot_id == cur.id,
                                (Bid.active == True) | (Bid.redo_available == True))).all():
                b.active = False
                b.redo_available = False
            cur.status = "QUEUED"
            cur.current_bid = 0
            cur.highest_franchise_id = None
            cur.bid_sequence = 0
            cur.ends_at = None
            cur.remaining_ms = None
            displaced = db.get(Player, cur.player_id)
    db.flush()

    # 2. Restore the settled player at his final bid.
    sale = db.scalar(select(Sale).where(Sale.lot_id == lot.id, Sale.reversed == False).order_by(desc(Sale.id)).limit(1))
    franchise = None
    if sale:
        franchise = db.get(Franchise, sale.franchise_id)
        franchise.purse_remaining += sale.price
        sale.reversed = True
        sale.redo_available = True
        last_bid = db.scalar(select(Bid).where(Bid.lot_id == lot.id, Bid.active == True)
                             .order_by(desc(Bid.sequence_no), desc(Bid.id)).limit(1))
        lot.current_bid = sale.price
        lot.highest_franchise_id = sale.franchise_id
        lot.bid_sequence = last_bid.sequence_no if last_bid else 0
    else:
        lot.current_bid = 0
        lot.highest_franchise_id = None
        lot.bid_sequence = 0
    if was_paused:
        lot.status = "PAUSED"
        lot.ends_at = None
        lot.remaining_ms = room.timer_seconds * 1000
        room.status = "PAUSED"
    else:
        lot.status = "LIVE"
        lot.ends_at = now_utc() + timedelta(seconds=room.timer_seconds)
        lot.remaining_ms = None
        room.status = "LIVE"
    lot.settlement_redo_available = True
    room.current_lot_id = lot.id

    msg = f"Undo last player: {player.name} is back on the block"
    msg += f" at {sale.price} (last bid by {franchise.name})." if sale else " (previously unsold)."
    if displaced is not None:
        msg += f" {displaced.name} returns to the queue and is next."
    event(db, room.id, "PLAYER_UNDONE", msg,
          {"lot_id": lot.id, "player_id": player.id, "was_sold": bool(sale),
           "sale_id": sale.id if sale else None,
           "restored_price": sale.price if sale else 0,
           "displaced_player": displaced.name if displaced is not None else None})
    db.flush()
    return lot

def _reset_clock(room: Room, lot: Lot):
    """Full timer after an undo/redo of a bid. A paused room stays paused: the full timer is held until resume."""
    if lot.status == "PAUSED":
        lot.ends_at = None
        lot.remaining_ms = room.timer_seconds * 1000
    else:
        lot.ends_at = now_utc() + timedelta(seconds=room.timer_seconds)

def undo_last_bid(db: Session, room: Room):
    lot = current_lot(db, room)
    if not lot or lot.status not in ("LIVE", "PAUSED"):
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
    _reset_clock(room, lot)
    event(db, room.id, "BID_UNDONE", "Host removed the most recent bid.",
          {"removed_bid_id": last.id, "restored_bid_id": previous.id if previous else None})
    return last

def redo_last_bid(db: Session, room: Room):
    lot = current_lot(db, room)
    if not lot or lot.status not in ("LIVE", "PAUSED"):
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
    _reset_clock(room, lot)
    franchise = db.get(Franchise, bid.franchise_id)
    event(db, room.id, "BID_REDONE", f"Host restored {franchise.name}'s bid of {bid.amount}.",
          {"restored_bid_id": bid.id, "franchise_id": franchise.id, "amount": bid.amount})
    db.flush()
    return bid


def _redo_sale_still_valid(lot, sale) -> bool:
    """A restored (live) player may only have his old sale re-applied while the bidding is
    still exactly where it was when he was sold."""
    if sale is not None:
        return lot.current_bid == sale.price and lot.highest_franchise_id == sale.franchise_id
    return (lot.current_bid or 0) == 0

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
    if lot.status in ("LIVE", "PAUSED") and not _redo_sale_still_valid(lot, sale):
        raise ValueError("Bidding has moved on for this player, so the earlier sale can no longer be redone")
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
    # A restart during the sold/unsold hold must not leave the room stuck without a player.
    # The next player is opened PAUSED so nothing starts ticking unattended.
    for room in db.scalars(select(Room).where(Room.status == "BETWEEN_LOTS")).all():
        lot = open_next_lot(db, room)
        if lot is not None:
            lot.status = "PAUSED"
            lot.ends_at = None
            lot.remaining_ms = room.timer_seconds * 1000
            room.status = "PAUSED"
            event(db, room.id, "RECOVERED_PAUSED",
                  "Server restarted between players. The next player is ready and paused; resume starts a fresh timer.",
                  {"lot_id": lot.id})
        room.version += 1
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
    redoable_sale = False
    for l in lots:
        if not (l.settlement_redo_available and l.status in ("QUEUED", "LIVE", "PAUSED")):
            continue
        if l.status == "QUEUED":
            redoable_sale = True
            break
        old_sale = db.scalar(select(Sale).where(Sale.lot_id == l.id, Sale.reversed == True,
                                                Sale.redo_available == True).order_by(desc(Sale.id)).limit(1))
        if _redo_sale_still_valid(l, old_sale):
            redoable_sale = True
            break
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
            "ends_at": iso_utc(live.ends_at), "remaining_ms": live.remaining_ms,
            "status": live.status, "round_no": live.round_no,
            "next_bid": next_bid_amount(live)
        }
    latest_lot_by_player = {}
    for candidate in sorted(lots, key=lambda item: item.id, reverse=True):
        latest_lot_by_player.setdefault(candidate.player_id, candidate)
    current_unsold = [candidate for candidate in latest_lot_by_player.values() if candidate.status == "UNSOLD"]
    return {
        # Lets browsers correct their clock skew so every device shows the same countdown.
        "server_time": iso_utc(now_utc()),
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
            "can_undo_bid": bool(live and active_bids and room.status in ("LIVE", "PAUSED")),
            "can_redo_bid": bool(live and room.status in ("LIVE", "PAUSED") and redoable_bid),
            "can_undo_sale": bool(room.status != "COMPLETED" and undoable_sale),
            "can_redo_sale": bool(room.status != "COMPLETED" and redoable_sale)
        }
    }
