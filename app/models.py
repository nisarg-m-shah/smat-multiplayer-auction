from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

def utcnow():
    return datetime.now(timezone.utc)

class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), default="SETUP")
    host_token_hash: Mapped[str] = mapped_column(String(64))
    purse: Mapped[int] = mapped_column(Integer, default=5000)
    timer_seconds: Mapped[int] = mapped_column(Integer, default=10)
    min_squad: Mapped[int] = mapped_column(Integer, default=18)
    max_squad: Mapped[int] = mapped_column(Integer, default=25)
    bid_extension: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=0)
    current_lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)
    host_franchise_id: Mapped[int | None] = mapped_column(ForeignKey("franchises.id"), nullable=True)
    host_password_hash: Mapped[str | None] = mapped_column(String(160), nullable=True)
    host_nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)

class Franchise(Base):
    __tablename__ = "franchises"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    invite_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purse_remaining: Mapped[int] = mapped_column(Integer)
    created_order: Mapped[int] = mapped_column(Integer)
    password_hash: Mapped[str | None] = mapped_column(String(160), nullable=True)
    login_nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("room_id", "name"),)

class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    state_team: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    base_price: Mapped[int] = mapped_column(Integer, default=20)
    importance_weight: Mapped[float] = mapped_column(Float, default=1.0)
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    source_order: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("room_id", "name", "state_team"),)

class Lot(Base):
    __tablename__ = "lots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    queue_order: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    base_price: Mapped[int] = mapped_column(Integer)
    current_bid: Mapped[int] = mapped_column(Integer, default=0)
    highest_franchise_id: Mapped[int | None] = mapped_column(ForeignKey("franchises.id"), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remaining_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bid_sequence: Mapped[int] = mapped_column(Integer, default=0)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settlement_redo_available: Mapped[bool] = mapped_column(Boolean, default=False)

class Bid(Base):
    __tablename__ = "bids"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), index=True)
    franchise_id: Mapped[int] = mapped_column(ForeignKey("franchises.id"))
    amount: Mapped[int] = mapped_column(Integer)
    sequence_no: Mapped[int] = mapped_column(Integer)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    redo_available: Mapped[bool] = mapped_column(Boolean, default=False)

class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    franchise_id: Mapped[int] = mapped_column(ForeignKey("franchises.id"))
    price: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reversed: Mapped[bool] = mapped_column(Boolean, default=False)
    redo_available: Mapped[bool] = mapped_column(Boolean, default=False)

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
