from kalshibot.feeds.spot import SpotTick
from kalshibot.observer import Observer


class RecordingDB:
    def __init__(self):
        self.rows = []

    def add(self, table, row):
        self.rows.append((table, row))


def observer(*, persist):
    value = Observer.__new__(Observer)
    value.persist_observations = persist
    value._last_spot_write = {}
    value.db = RecordingDB()
    return value


def test_non_persisting_observer_keeps_feed_tick_out_of_shared_tape():
    value = observer(persist=False)
    value._record_spot_tick(
        SpotTick(ts=100.0, asset="SOL", source="coinbase", price=125.0),
    )
    assert value.db.rows == []


def test_primary_observer_persists_feed_tick():
    value = observer(persist=True)
    value._record_spot_tick(
        SpotTick(ts=100.0, asset="SOL", source="coinbase", price=125.0),
    )
    assert value.db.rows == [
        (
            "spot_ticks",
            {"ts": 100.0, "asset": "SOL", "source": "coinbase", "price": 125.0},
        )
    ]
