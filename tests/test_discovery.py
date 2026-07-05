"""Discovery mapping tests — pure logic, no network."""

from kalshibot.config import TARGET_ASSETS, load_asset_configs
from kalshibot.kalshi.discovery import match_asset
from kalshibot.kalshi.models import Series


def series(ticker: str, title: str = "", frequency: str = "fifteen_min") -> Series:
    return Series(ticker=ticker, title=title, frequency=frequency)


LIVE_LIKE = [
    series("KXBTC15M", "Bitcoin price up down"),
    series("KXETH15M", "ETH 15M price up down"),
    series("KXSOL15M", "Solana 15 minutes"),
    series("KXZEC15M", "Zcash 15min"),
    series("KXHYPE15M", "Hype 15 min"),
    series("KXXRP15M", "XRP 15 Minute"),
    series("KXDOGE15M", "Dogecoin 15 Minute"),
    series("KXBNB15M", "BNB 15 Minute"),
    series("KXNEAR15M", "NEAR 15m"),
    series("KXADA15M", "Cardano 15 Minute"),
    series("KXBCH15M", "Bitcoin Cash 15 Minute"),
    series("KXTON15M", "TON 15m"),
]


def test_all_nine_targets_match_exact_pattern():
    for asset in TARGET_ASSETS:
        matched = match_asset(asset, LIVE_LIKE)
        assert matched is not None, asset
        assert matched.ticker == f"KX{asset}15M"


def test_missing_asset_returns_none():
    pool = [s for s in LIVE_LIKE if s.ticker != "KXNEAR15M"]
    assert match_asset("NEAR", pool) is None


def test_btc_does_not_match_bch_or_bcash():
    # BTC must not fuzzy-match "Bitcoin Cash" / KXBCH15M
    pool = [series("KXBCH15M", "Bitcoin Cash 15 Minute")]
    assert match_asset("BTC", pool) is None


def test_fallback_matches_renamed_ticker():
    pool = [series("KXSOLANA-15MIN", "SOL 15 minute up/down")]
    matched = match_asset("SOL", pool)
    assert matched is not None and matched.ticker == "KXSOLANA-15MIN"


def test_asset_config_loads_all_nine():
    configs = load_asset_configs()
    assert set(configs) == set(TARGET_ASSETS)
    # defaults applied, per-asset overrides respected
    assert configs["BTC"].edge_threshold_cents == 3
    assert configs["ZEC"].edge_threshold_cents == 5
    assert all(c.enabled for c in configs.values())
    # paused is a legitimate per-asset state (HYPE benched 2026-07-05);
    # just verify the flag round-trips from YAML
    assert configs["HYPE"].paused is True
    assert configs["BTC"].paused is False
