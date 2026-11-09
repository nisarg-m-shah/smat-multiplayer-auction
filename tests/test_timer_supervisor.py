import asyncio

from app import main
from app.engine import now_utc


def test_timer_supervisor_retries_transient_settlement_failure(monkeypatch):
    attempts = {"settle": 0, "broadcast": 0, "sleep": []}

    def read_deadline(_code):
        return now_utc()

    def settle(_code):
        attempts["settle"] += 1
        if attempts["settle"] == 1:
            raise RuntimeError("temporary database disconnect")
        return True, False

    async def no_wait(delay):
        attempts["sleep"].append(delay)

    async def broadcast(_code):
        attempts["broadcast"] += 1

    monkeypatch.setattr(main, "_read_timer_deadline", read_deadline)
    monkeypatch.setattr(main, "_settle_expired_lot", settle)
    monkeypatch.setattr(main, "broadcast_snapshot", broadcast)
    monkeypatch.setattr(main.asyncio, "sleep", no_wait)

    asyncio.run(main.timer_supervisor("TEST01"))

    assert attempts["settle"] == 2
    assert attempts["broadcast"] == 1
    assert attempts["sleep"] == [1.0]
