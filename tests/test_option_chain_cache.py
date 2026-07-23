"""
Unit tests for service/optionChain/OptionChainCache.py.
"""

import asyncio

import pytest

from service.optionChain.OptionChainCache import OptionChainCache

STRIKE_CHAIN = {
    "24300": {"ce_token": "111", "pe_token": "222", "lot_size": 65},
    "24350": {"ce_token": "333", "pe_token": "444", "lot_size": 65},
}


def _far_expiry() -> str:
    """A far-future expiry so time-to-expiry is always positive regardless of when tests run."""
    return "2099-12-31"


class TestSeedAndSnapshot:

    def test_seed_leg_populates_snapshot(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        cache.set_spot(24270.0)
        cache.seed_leg("24300", "ce", {"ltp": 62.16, "oi": 18366})

        snapshot = cache.get()
        assert snapshot["spot"] == 24270.0
        strike_entry = next(s for s in snapshot["strikes"] if s["strike"] == 24300.0)
        assert strike_entry["ce"]["ltp"] == 62.16
        assert strike_entry["pe"] is None

    def test_get_returns_none_when_never_seeded(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        assert cache.get() is None

    def test_strikes_sorted_ascending(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        cache.seed_leg("24350", "ce", {"ltp": 40.0})
        cache.seed_leg("24300", "ce", {"ltp": 62.0})
        strikes = [s["strike"] for s in cache.get()["strikes"]]
        assert strikes == sorted(strikes)


class TestApplyTick:

    @pytest.mark.asyncio
    async def test_apply_tick_merges_into_existing_leg(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        cache.set_spot(24270.0)
        cache.seed_leg("24300", "ce", {"ltp": 62.16, "oi": 18366})

        await cache.apply_tick("NFO|111", {"ltp": 65.0, "bid": 64.8, "ask": 65.2})

        strike_entry = next(s for s in cache.get()["strikes"] if s["strike"] == 24300.0)
        assert strike_entry["ce"]["ltp"] == 65.0
        assert strike_entry["ce"]["bid"] == 64.8
        assert strike_entry["ce"]["oi"] == 18366  # untouched field preserved

    @pytest.mark.asyncio
    async def test_apply_tick_unknown_token_is_ignored(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        gen_before = cache.generation
        await cache.apply_tick("NFO|999999", {"ltp": 10.0})
        assert cache.generation == gen_before

    @pytest.mark.asyncio
    async def test_apply_tick_bumps_generation(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        gen_before = cache.generation
        await cache.apply_tick("NFO|111", {"ltp": 65.0})
        assert cache.generation == gen_before + 1

    @pytest.mark.asyncio
    async def test_oi_change_computed_against_first_seen_baseline(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        await cache.apply_tick("NFO|111", {"oi": 18000})
        await cache.apply_tick("NFO|111", {"oi": 18500})

        strike_entry = next(s for s in cache.get()["strikes"] if s["strike"] == 24300.0)
        assert strike_entry["ce"]["oi_change"] == 500

    @pytest.mark.asyncio
    async def test_malformed_expiry_does_not_block_tick_from_applying(self):
        """Regression test: _recompute_iv() used to raise unguarded on a bad
        expiry string, aborting apply_tick() before the generation bump/
        notify_all() - silently dropping the tick's ltp/bid/ask/oi fields too,
        with no error surfaced anywhere (apply_tick runs from a detached task)."""
        cache = OptionChainCache("NIFTY", "not-a-valid-date", STRIKE_CHAIN)
        cache.set_spot(24270.0)
        gen_before = cache.generation

        await cache.apply_tick("NFO|111", {"ltp": 65.0, "oi": 18000})

        assert cache.generation == gen_before + 1
        strike_entry = next(s for s in cache.get()["strikes"] if s["strike"] == 24300.0)
        assert strike_entry["ce"]["ltp"] == 65.0  # tick data still applied
        assert strike_entry["ce"]["iv"] is None    # IV safely omitted instead

    @pytest.mark.asyncio
    async def test_wait_for_next_wakes_on_update(self):
        cache = OptionChainCache("NIFTY", _far_expiry(), STRIKE_CHAIN)
        gen = cache.generation

        async def _tick_later():
            await asyncio.sleep(0.05)
            await cache.apply_tick("NFO|111", {"ltp": 70.0})

        asyncio.create_task(_tick_later())
        result = await asyncio.wait_for(cache.wait_for_next(gen), timeout=2.0)
        assert result is not None
