from datetime import timedelta
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
    assert mi.purse_remaining == 5000 and lot.status == "QUEUED"
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
    assert lot.status == "QUEUED" and lot.settlement_redo_available is True
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
