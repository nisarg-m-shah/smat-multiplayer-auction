from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.engine import now_utc, recover_interrupted_rooms, resume_room
from app.models import Room, Franchise, Player, Lot, Bid, Sale, Event


def test_restart_pauses_room_and_preserves_accepted_bid_and_state():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        room = Room(code="ABC123", name="Test", status="LIVE", host_token_hash="x",
                    purse=5000, timer_seconds=10, min_squad=1, max_squad=25)
        db.add(room)
        db.flush()
        franchise = Franchise(room_id=room.id, name="Mumbai", invite_hash="f",
                              purse_remaining=4900, created_order=0)
        player = Player(room_id=room.id, name="Player A", state_team="Mumbai",
                        role="BAT", base_price=20, importance_weight=1.0,
                        included=True, source_order=0)
        db.add_all([franchise, player])
        db.flush()
        lot = Lot(room_id=room.id, player_id=player.id, round_no=1, queue_order=0,
                  status="LIVE", base_price=20, current_bid=120,
                  highest_franchise_id=franchise.id,
                  ends_at=now_utc() + timedelta(seconds=3), bid_sequence=1)
        db.add(lot)
        db.flush()
        room.current_lot_id = lot.id
        bid = Bid(room_id=room.id, lot_id=lot.id, franchise_id=franchise.id,
                  amount=120, sequence_no=1)
        db.add(bid)
        db.commit()

        recovered = recover_interrupted_rooms(db)
        db.refresh(room); db.refresh(lot); db.refresh(franchise)

        assert recovered == ["ABC123"]
        assert room.status == "PAUSED"
        assert room.current_lot_id == lot.id
        assert lot.status == "PAUSED"
        assert lot.ends_at is None
        assert lot.remaining_ms == 10_000
        assert lot.current_bid == 120
        assert lot.highest_franchise_id == franchise.id
        assert db.scalar(select(Bid).where(Bid.lot_id == lot.id)).amount == 120
        assert franchise.purse_remaining == 4900

        resume_room(db, room)
        db.commit()
        db.refresh(room); db.refresh(lot)
        assert room.status == "LIVE"
        assert lot.status == "LIVE"
        assert lot.ends_at is not None
        deadline = lot.ends_at.replace(tzinfo=now_utc().tzinfo) if lot.ends_at.tzinfo is None else lot.ends_at
        assert 9.0 <= (deadline - now_utc()).total_seconds() <= 10.1
        assert lot.current_bid == 120
        assert lot.highest_franchise_id == franchise.id

        assert db.scalar(select(Event).where(Event.kind == "RECOVERED_PAUSED")) is not None

    engine.dispose()
