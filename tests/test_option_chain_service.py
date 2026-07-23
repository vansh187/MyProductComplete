"""
Unit tests for service/optionChain/OptionChainService.py.
All Shoonya/OptionMaster calls are mocked - no live network, no real master file.
"""

from unittest.mock import MagicMock, patch

import pytest

from service.optionChain.OptionChainService import OptionChainService

FAKE_STRIKE_CHAIN = {
    "24300": {"ce_token": "111", "ce_tsym": "NIFTY07JUL26C24300", "pe_token": "222", "pe_tsym": "NIFTY07JUL26P24300", "lot_size": 65},
    "24350": {"ce_token": "333", "ce_tsym": "NIFTY07JUL26C24350", "pe_token": "444", "pe_tsym": "NIFTY07JUL26P24350", "lot_size": 65},
}


def _fake_shoonya():
    shoonya = MagicMock()
    shoonya.is_connected = True
    shoonya.get_index_quote.return_value = {"ltp": 24270.85}
    shoonya.get_option_quote.return_value = {"ltp": 62.16, "bid": 61.8, "ask": 62.5, "oi": 18366, "volume": 45210}
    return shoonya


class TestGetChainErrorPaths:

    @pytest.mark.asyncio
    async def test_invalid_underlying(self):
        svc = OptionChainService(feed=None)
        data, errors = await svc.get_chain(_fake_shoonya(), "dowjones", None)
        assert data is None
        assert errors == [{"reason": "invalid_underlying"}]

    @pytest.mark.asyncio
    async def test_no_option_data_when_master_has_nothing_for_underlying(self):
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.nearest_expiry.return_value = "2026-07-07"
            mock_master.get_strike_chain.return_value = {}

            svc = OptionChainService(feed=None)
            data, errors = await svc.get_chain(_fake_shoonya(), "nifty", None)

        assert data is None
        assert errors == [{"reason": "no_option_data"}]

    @pytest.mark.asyncio
    async def test_no_expiry_available(self):
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.nearest_expiry.return_value = None

            svc = OptionChainService(feed=None)
            data, errors = await svc.get_chain(_fake_shoonya(), "nifty", None)

        assert data is None
        assert errors == [{"reason": "no_expiry_available"}]


class TestGetChainHappyPath:

    @pytest.mark.asyncio
    async def test_returns_populated_chain(self):
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.nearest_expiry.return_value = "2026-07-07"
            mock_master.get_strike_chain.return_value = FAKE_STRIKE_CHAIN

            svc = OptionChainService(feed=None)
            data, errors = await svc.get_chain(_fake_shoonya(), "nifty", None)

        assert errors == []
        assert data["symbol"] == "NIFTY"
        assert data["exchange"] == "NFO"
        assert data["expiry"] == "2026-07-07"
        assert data["spot"] == 24270.85
        assert len(data["strikes"]) == 2

    @pytest.mark.asyncio
    async def test_sensex_chain_uses_bfo_exchange(self):
        """Sensex options trade on BFO (BSE F&O), not NFO - the response and
        every underlying REST/WS token must reflect that, not the NSE default."""
        sensex_chain = {
            "80000": {"ce_token": "900001", "ce_tsym": "SENSEX07JUL26C80000", "pe_token": "900002", "pe_tsym": "SENSEX07JUL26P80000", "lot_size": 10},
        }
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.nearest_expiry.return_value = "2026-07-07"
            mock_master.get_strike_chain.return_value = sensex_chain

            shoonya = _fake_shoonya()
            svc = OptionChainService(feed=None)
            data, errors = await svc.get_chain(shoonya, "sensex", None)

        assert errors == []
        assert data["symbol"] == "SENSEX"
        assert data["exchange"] == "BFO"
        shoonya.get_option_quote.assert_any_call("BFO", "900001")
        shoonya.get_index_quote.assert_any_call("BSE", "1")

    @pytest.mark.asyncio
    async def test_explicit_expiry_bypasses_nearest_expiry_lookup(self):
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.get_strike_chain.return_value = FAKE_STRIKE_CHAIN

            svc = OptionChainService(feed=None)
            data, errors = await svc.get_chain(_fake_shoonya(), "nifty", "2026-07-14")

        mock_master.nearest_expiry.assert_not_called()
        assert data["expiry"] == "2026-07-14"

    @pytest.mark.asyncio
    async def test_cache_is_reused_across_calls(self):
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.get_strike_chain.return_value = FAKE_STRIKE_CHAIN

            svc = OptionChainService(feed=None)
            shoonya = _fake_shoonya()
            await svc.get_chain(shoonya, "nifty", "2026-07-14")
            await svc.get_chain(shoonya, "nifty", "2026-07-14")

        # get_strike_chain (master lookup) only happens on first creation
        assert mock_master.get_strike_chain.call_count == 1


class TestFeedSubscription:

    @pytest.mark.asyncio
    async def test_new_chain_triggers_subscription(self):
        feed = MagicMock()
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.get_strike_chain.return_value = FAKE_STRIKE_CHAIN

            svc = OptionChainService(feed=feed)
            await svc.get_chain(_fake_shoonya(), "nifty", "2026-07-14")

        feed.ensure_subscribed.assert_called_once()
        subscribed_tokens = feed.ensure_subscribed.call_args[0][0]
        assert subscribed_tokens == {"NFO|111", "NFO|222", "NFO|333", "NFO|444"}

    @pytest.mark.asyncio
    async def test_release_chain_unsubscribes_after_stream_disconnect(self):
        feed = MagicMock()
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.get_strike_chain.return_value = FAKE_STRIKE_CHAIN

            svc = OptionChainService(feed=feed)
            await svc.get_cache_for_stream(_fake_shoonya(), "nifty", "2026-07-14")
            await svc.release_chain("nifty", "2026-07-14")

        feed.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_two_concurrent_stream_viewers_first_disconnect_does_not_unsubscribe(self):
        """Regression test: two SSE clients viewing the same chain used to
        share a single feed.ensure_subscribed() call, but release_chain()
        would unsubscribe on ANY single client's disconnect - killing live
        updates for the client that's still connected."""
        feed = MagicMock()
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.get_strike_chain.return_value = FAKE_STRIKE_CHAIN

            svc = OptionChainService(feed=feed)
            shoonya = _fake_shoonya()

            # Two concurrent SSE viewers attach to the same chain.
            await svc.get_cache_for_stream(shoonya, "nifty", "2026-07-14")
            await svc.get_cache_for_stream(shoonya, "nifty", "2026-07-14")

            # First viewer disconnects.
            await svc.release_chain("nifty", "2026-07-14")
            feed.release.assert_not_called()  # second viewer still needs it subscribed

            # Second viewer disconnects.
            await svc.release_chain("nifty", "2026-07-14")
            feed.release.assert_called_once()  # only now actually unsubscribed

    @pytest.mark.asyncio
    async def test_evicted_chain_is_freshly_recreated_on_next_request(self):
        """Once fully released, the cache/token-index entries must actually be
        removed (not just unsubscribed) so a later request re-subscribes
        from scratch rather than reusing a stale, unsubscribed cache."""
        feed = MagicMock()
        with patch("service.optionChain.OptionChainService.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            mock_master.get_strike_chain.return_value = FAKE_STRIKE_CHAIN

            svc = OptionChainService(feed=feed)
            shoonya = _fake_shoonya()

            await svc.get_cache_for_stream(shoonya, "nifty", "2026-07-14")
            await svc.release_chain("nifty", "2026-07-14")

            assert svc._caches == {}
            assert svc._token_to_cache_keys == {}

            await svc.get_cache_for_stream(shoonya, "nifty", "2026-07-14")
            assert feed.ensure_subscribed.call_count == 2  # re-subscribed on the new request
