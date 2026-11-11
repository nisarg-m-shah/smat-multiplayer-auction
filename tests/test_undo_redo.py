from datetime import timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import Room, Franchise, Player, Lot
from app.engine import (
    place_bid, undo_last_bid, redo_last_bid, settle_lot,
    undo_last_sale, redo_last_sale, now_utc,
)


def make_auction():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    db = Session()
    room = Room(code="REDO01", name="Test", status="LIVE", host_token_hash="x",
                purse=5000, timer_seconds=10, min_squad=1, max_squad=25)
    db.add(room); db.flush()
    mi = Franchise(room_id=room.id, name="MI", invite_hash="mi", purse_remaining=5000, created_order=0)
    csk = Franchise(room_id=room.id, name="CSK", invite_hash="csk", purse_remaining=5000, created_order=1)
    player = Player(room_id=room.id, name="Test Player", state_team="Test State", role="BAT", base_price=20)
    db.add_all([mi, csk, player]); db.flush()
    lot = Lot(room_id=room.id, player_id=player.id, round_no=1, queue_order=0,
              status="LIVE", base_price=20, current_bid=0, bid_sequence=0,
              ends_at=now_utc() + timedelta(seconds=10))
    db.add(lot); db.flush(); room.current_lot_id = lot.id; db.commit()
    return db, room, mi, csk, lot


def test_bid_undo_redo_restores_exact_bidder_and_amount():
    db, room, mi, csk, lot = make_auction()
    place_bid(db, room, mi, 20); place_bid(db, room, csk, 30); db.commit()
    undo_last_bid(db, room); db.commit()
    assert (lot.current_bid, lot.highest_franchise_id) == (20, mi.id)
    redo_last_bid(db, room); db.commit()
    assert (lot.current_bid, lot.highest_franchise_id) == (30, csk.id)
    db.close()


def test_sale_undo_redo_restores_purse_and_squad_sale():
    db, room, mi, csk, lot = make_auction()
    place_bid(db, room, mi, 20); db.commit()
    lot.ends_at = now_utc() - timedelta(seconds=1); db.commit()
    settle_lot(db, room); db.commit()
    assert mi.purse_remaining == 4980
    undo_last_sale(db, room); db.commit()
    assert mi.purse_remaining == 5000 and lot.status == "PAUSED"
    assert (lot.current_bid, lot.highest_franchise_id) == (20, mi.id) and room.current_lot_id == lot.id
    redo_last_sale(db, room); db.commit()
    assert mi.purse_remaining == 4980 and lot.status == "SOLD"
    db.close()


def test_unsold_player_can_be_undone_and_redone():
    db, room, mi, csk, lot = make_auction()
    lot.ends_at = now_utc() - timedelta(seconds=1)
    db.commit()
    settle_lot(db, room)
    db.commit()
    assert lot.status == "UNSOLD"
    from app.engine import undo_last_sale
    undo_last_sale(db, room)
    db.commit()
    assert lot.status == "PAUSED" and lot.current_bid == 0 and lot.settlement_redo_available is True
    redo_last_sale(db, room)
    db.commit()
    assert lot.status == "UNSOLD" and lot.settlement_redo_available is False
    db.close()


def test_unsold_only_round_skips_unseen_players_and_opens_selected_unsold():
    from app.engine import nominated_unsold, open_next_lot
    db, room, mi, csk, first_lot = make_auction()
    # Make the current player unsold and create one unseen queued player.
    first_lot.ends_at = now_utc() - timedelta(seconds=1)
    db.commit()
    settle_lot(db, room)
    db.commit()
    unseen = Player(room_id=room.id, name="Unseen Player", state_team="Other", role="BAT", base_price=20)
    db.add(unseen); db.flush()
    db.add(Lot(room_id=room.id, player_id=unseen.id, round_no=1, queue_order=1, status="QUEUED", base_price=20))
    db.commit()
    nominated_unsold(db, room, [first_lot.player_id])
    db.commit()
    assert room.status == "LIVE"
    assert room.current_lot_id is not None
    assert db.get(Lot, room.current_lot_id).player_id == first_lot.player_id
    assert db.query(Lot).filter(Lot.player_id == unseen.id).one().status == "SKIPPED"
    db.close()


def _two_lots():
    """Sanju (sold to MI at 30) then Jaiswal already live with a bid from CSK."""
    from app.engine import resume_room, open_next_lot
    db, room, mi, csk, sanju = make_auction()
    jaiswal_p = Player(room_id=room.id, name="Jaiswal", state_team="X", role="BAT", base_price=20)
    third_p = Player(room_id=room.id, name="Third", state_team="X", role="BAT", base_price=20)
    db.add_all([jaiswal_p, third_p]); db.flush()
    jai = Lot(room_id=room.id, player_id=jaiswal_p.id, round_no=1, queue_order=1, status="QUEUED", base_price=20)
    third = Lot(room_id=room.id, player_id=third_p.id, round_no=1, queue_order=2, status="QUEUED", base_price=20)
    db.add_all([jai, third]); db.commit()
    place_bid(db, room, mi, 20); place_bid(db, room, csk, 30); place_bid(db, room, mi, 40); db.commit()
    sanju.ends_at = now_utc() - timedelta(seconds=1); db.commit()
    settle_lot(db, room); open_next_lot(db, room); db.commit()          # Jaiswal is now live
    place_bid(db, room, csk, 20); db.commit()                            # ...and already has a bid
    return db, room, mi, csk, sanju, jai, third


def test_undo_player_after_next_started_restores_last_bid_then_resumes_with_next_player():
    from app.engine import resume_room, open_next_lot
    db, room, mi, csk, sanju, jai, third = _two_lots()
    assert mi.purse_remaining == 5000 - 40 and sanju.status == "SOLD" and room.current_lot_id == jai.id
    undo_last_sale(db, room); db.commit()
    # Sanju is back on the block at his LAST HIGHEST BID, paused, with his bid history intact.
    assert room.current_lot_id == sanju.id and room.status == "PAUSED" and sanju.status == "PAUSED"
    assert (sanju.current_bid, sanju.highest_franchise_id) == (40, mi.id)
    assert mi.purse_remaining == 5000
    # Jaiswal is parked at the FRONT of the queue (fresh), ahead of the third player.
    assert jai.status == "QUEUED" and jai.current_bid == 0 and jai.highest_franchise_id is None
    assert third.status == "QUEUED"
    # Host resumes; bidding continues from 40 (exact next amount), then Sanju is settled again.
    resume_room(db, room); db.commit()
    assert sanju.settlement_redo_available is False
    place_bid(db, room, csk, 50); db.commit()
    sanju.ends_at = now_utc() - timedelta(seconds=1); db.commit()
    settle_lot(db, room); db.commit()
    assert sanju.status == "SOLD" and csk.purse_remaining == 5000 - 50
    nxt = open_next_lot(db, room); db.commit()
    assert nxt.id == jai.id, "Jaiswal (already chosen as next) must follow Sanju, not the third player"
    db.close()


def test_undo_bid_still_works_on_a_restored_player():
    db, room, mi, csk, sanju, jai, third = _two_lots()
    from app.engine import resume_room
    undo_last_sale(db, room); resume_room(db, room); db.commit()
    undo_last_bid(db, room); db.commit()
    assert (sanju.current_bid, sanju.highest_franchise_id) == (30, csk.id)
    db.close()


def test_redo_player_after_undo_with_next_started_resells_and_moves_on():
    from app.engine import open_next_lot
    db, room, mi, csk, sanju, jai, third = _two_lots()
    undo_last_sale(db, room); db.commit()
    redo_last_sale(db, room); db.commit()
    assert sanju.status == "SOLD" and mi.purse_remaining == 5000 - 40
    nxt = open_next_lot(db, room); db.commit()
    assert nxt.id == jai.id
    db.close()


def test_next_player_waits_for_result_pause_and_rejects_early_bids(monkeypatch):
    import app.engine as eng
    from app.engine import open_next_lot, load_bid_context, validate_bid, BidRejected, result_pause_for
    import pytest
    db, room, mi, csk, sanju, jai, third = _two_lots()
    monkeypatch.setattr(eng, "RESULT_PAUSE_SOLD", 4.0)
    undo_last_sale(db, room); db.commit()
    redo_last_sale(db, room); db.commit()
    assert result_pause_for(sanju) == 4.0
    nxt = open_next_lot(db, room, lead_in=result_pause_for(sanju)); db.commit()
    remaining = (nxt.ends_at.replace(tzinfo=timezone.utc) if nxt.ends_at.tzinfo is None else nxt.ends_at) - now_utc()
    assert 13.5 < remaining.total_seconds() <= 14.0          # 4 s pause + 10 s timer
    from app.engine import BidContext
    ctx = BidContext(room=room, lot=nxt, franchise=mi, is_host=False)
    with pytest.raises(BidRejected) as e:
        validate_bid(ctx, 20)
    assert e.value.code == "PLAYER_NOT_STARTED"
    # once the pause has elapsed (<= timer seconds remain) the same bid is valid
    nxt.ends_at = now_utc() + timedelta(seconds=9.9)
    assert validate_bid(ctx, 20) == 20
    db.close()
