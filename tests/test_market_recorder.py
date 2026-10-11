"""
Unit tests for service/marketRecorder: forward-filled rows, day/window
naming, journal -> hourly Parquet (incl. crash recovery), subscription
planning (ATM window, hysteresis, subscribe-before-release), the recorder's
session flow end to end with a fake feed, and the end-of-day Drive upload
with a fake Drive client.
"""

import json
from datetime import date, datetime, time
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from marketengine.ShoonyaOptionFeed import DEPTH_FEED, ShoonyaOptionFeed
from service.marketRecorder.config import RecorderConfig
from service.marketRecorder.contract import validate_file
from service.marketRecorder.dayUploader import MANIFEST_NAME, DayUploader, md5_of
from service.marketRecorder.recorder import MarketRecorder
from service.marketRecorder.rowBuilder import RowBuilder
from service.marketRecorder.streamWriter import StreamWriter
from service.marketRecorder.subscriptionPlanner import SubscriptionPlanner
from service.marketRecorder.tradingCalendar import TradingCalendar, day_folder_name, to_ns
from utils.market_hours import IST

DAY = date(2026, 10, 12)   # a Monday


def ns_at(hh, mm, ss=0, day=DAY):
    return to_ns(datetime.combine(day, time(hh, mm, ss), IST))


def make_calendar(window_minutes=60):
    return TradingCalendar(time(9, 0), time(15, 30), window_minutes)


def make_config(tmp_path, **overrides):
    values = dict(enabled=True, data_dir=tmp_path / "market", holidays_file=tmp_path / "none.json",
                  holidays_cache_file=tmp_path / "nse_cache.json", refresh_holidays=False,
                  session_end=time(15, 30), candles_enabled=False,
                  strikes_each_side=1, recenter_after_strikes=2, option_expiries=1, hostname="test-vm")
    values.update(overrides)
    return RecorderConfig(**values)


# ── naming and windows ───────────────────────────────────────────────────

class TestCalendar:

    def test_day_folder_is_dd_mm_yyyy(self):
        assert day_folder_name(DAY) == "12-10-2026"

    def test_hourly_windows_seven_per_day_last_one_short(self):
        windows = make_calendar().windows(DAY)
        assert [w.file_name for w in windows] == [
            "part-0900.parquet", "part-1000.parquet", "part-1100.parquet", "part-1200.parquet",
            "part-1300.parquet", "part-1400.parquet", "part-1500.parquet"]
        assert windows[-1].end.time() == time(15, 30)

    def test_window_for_ns_and_outside_session(self):
        calendar = make_calendar()
        assert calendar.window_for_ns(ns_at(10, 59, 59)).label == "1000"
        assert calendar.window_for_ns(ns_at(11, 0, 0)).label == "1100"
        assert calendar.window_for_ns(ns_at(15, 30, 0)) is None
        assert calendar.window_for_ns(ns_at(8, 59, 59)) is None

    def test_holidays_and_special_sessions(self, tmp_path):
        holidays = tmp_path / "h.json"
        holidays.write_text(json.dumps({"holidays": ["2026-10-12"], "special_sessions": ["2026-10-17"]}))
        calendar = TradingCalendar(time(9, 0), time(15, 30), 60, holidays)
        assert not calendar.is_trading_day(DAY)
        assert calendar.is_trading_day(date(2026, 10, 17))       # a Saturday special session
        assert not calendar.is_trading_day(date(2026, 10, 18))   # a plain Sunday


# ── forward-filled rows ──────────────────────────────────────────────────

class TestRowBuilder:

    def test_partial_update_is_forward_filled(self):
        rows = RowBuilder(index_tokens=set())
        rows.tick_row(1, {"t": "tk", "e": "NFO", "tk": "111", "ts": "NIFTY13OCT26C25000",
                          "lp": "120.5", "bp1": "120", "sp1": "121", "v": "100", "oi": "5000", "ft": "1760239800"})
        row = rows.tick_row(2, {"t": "tf", "e": "NFO", "tk": "111", "lp": "121.0"})
        assert row["ltp"] == 121.0
        assert row["bid"] == 120.0 and row["ask"] == 121.0 and row["oi"] == 5000
        assert row["tsym"] == "NIFTY13OCT26C25000"
        assert row["ts_exch_ns"] is None      # feed time belongs to its own frame only
        assert row["day_open"] is None        # never received -> null, not 0

    def test_zero_price_is_null(self):
        row = RowBuilder(set()).tick_row(1, {"t": "tk", "e": "NFO", "tk": "1", "lp": "0", "bp1": "0.00"})
        assert row["ltp"] is None and row["bid"] is None

    def test_index_has_no_bid_ask(self):
        row = RowBuilder({"NSE|26000"}).tick_row(1, {"t": "tk", "e": "NSE", "tk": "26000", "lp": "25000", "bp1": "1"})
        assert row["bid"] is None and row["ltp"] == 25000.0

    def test_every_frame_with_content_is_a_row(self):
        """Nothing Shoonya sends is dropped: % change alone is still a row."""
        rows = RowBuilder(set())
        rows.tick_row(1, {"t": "tk", "e": "NFO", "tk": "1", "lp": "10"})
        row = rows.tick_row(2, {"t": "tf", "e": "NFO", "tk": "1", "pc": "-0.5"})
        assert row["pct_change"] == -0.5 and row["ltp"] == 10.0
        assert rows.tick_row(3, {"t": "tf", "e": "NFO", "tk": "1"}) is None   # envelope only

    def test_all_known_fields_and_unknown_ones_kept(self):
        row = RowBuilder(set()).tick_row(1, {
            "t": "tk", "e": "NFO", "tk": "1", "lp": "10", "poi": "100", "toi": "900", "ltt": "09:15:01",
            "uc": "20", "lc": "5", "52h": "30", "52l": "2", "tbq": "1000", "tsq": "2000", "bo1": "4", "so1": "6",
            "pp": "2", "ls": "75", "brand_new_field": "x"})
        assert (row["prev_oi"], row["total_oi"], row["last_trade_time"]) == (100, 900, "09:15:01")
        assert (row["upper_circuit"], row["lower_circuit"], row["high_52w"], row["low_52w"]) == (20.0, 5.0, 30.0, 2.0)
        assert (row["total_buy_qty"], row["total_sell_qty"], row["bid_orders"], row["ask_orders"]) == (1000, 2000, 4, 6)
        assert json.loads(row["extra"]) == {"pp": "2", "ls": "75", "brand_new_field": "x"}

    def test_depth_keeps_all_fields(self):
        row = RowBuilder(set()).depth_row(1, {"t": "dk", "e": "NFO", "tk": "7", "bp1": "100", "oi": "50",
                                              "tbq": "10", "ltt": "10:00:00", "odd": "1"})
        assert row["oi"] == 50 and row["total_buy_qty"] == 10 and row["last_trade_time"] == "10:00:00"
        assert json.loads(row["extra"]) == {"odd": "1"}

    def test_missed_first_frame_uses_master_name(self):
        rows = RowBuilder(set(), tsym_lookup=lambda key: "NIFTY28OCT26F" if key == "NFO|9" else None)
        assert rows.tick_row(1, {"t": "tf", "e": "NFO", "tk": "9", "lp": "1"})["tsym"] == "NIFTY28OCT26F"

    def test_depth_levels_merge(self):
        rows = RowBuilder(set())
        rows.depth_row(1, {"t": "dk", "e": "NFO", "tk": "7", "bp1": "100", "bq1": "50", "bo1": "3", "sp2": "101.5"})
        row = rows.depth_row(2, {"t": "df", "e": "NFO", "tk": "7", "bq1": "75"})
        assert row["bid_px_1"] == 100.0 and row["bid_qty_1"] == 75 and row["bid_orders_1"] == 3
        assert row["ask_px_2"] == 101.5 and row["ask_px_5"] is None


# ── journal -> hourly Parquet ────────────────────────────────────────────

def tick(ns, token="1", ltp=1.0):
    return {"ts_recv_ns": ns, "ts_exch_ns": None, "exchange": "NFO", "token": token, "tsym": f"T{token}",
            "ltp": ltp, "ltq": None, "volume": None, "oi": None, "atp": None, "bid": None, "bid_qty": None,
            "ask": None, "ask_qty": None, "day_open": None, "day_high": None, "day_low": None, "prev_close": None}


META = {"window_minutes": "60", "recorder_version": "test", "vm_hostname": "test-vm"}


class TestStreamWriter:

    def test_window_file_is_sorted_valid_and_named(self, tmp_path):
        calendar = make_calendar()
        writer = StreamWriter("ticks", tmp_path, calendar, META)
        writer.add(tick(ns_at(10, 30)))
        writer.add(tick(ns_at(10, 5)))
        writer.flush()
        writer.add(tick(ns_at(10, 59, 59)))
        path = writer.close_window(calendar.window_for_ns(ns_at(10, 0)))
        assert path == tmp_path / "12-10-2026" / "ticks" / "part-1000.parquet"
        assert validate_file(path) == []
        table = pq.read_table(path)
        assert table.column("ts_recv_ns").to_pylist() == [ns_at(10, 5), ns_at(10, 30), ns_at(10, 59, 59)]
        assert table.schema.metadata[b"trading_date"] == b"2026-10-12"
        assert not list((tmp_path / ".journal").rglob("*.arrows"))   # segments removed

    def test_restart_mid_window_gives_one_file(self, tmp_path):
        calendar = make_calendar()
        before = StreamWriter("ticks", tmp_path, calendar, META)
        before.add(tick(ns_at(11, 10)))
        before.flush()                         # journalled, then the process "crashes"
        after = StreamWriter("ticks", tmp_path, calendar, META)
        assert [w.label for w in after.pending_windows()] == ["1100"]
        assert after.last_journal_ns(DAY) == ns_at(11, 10)
        after.add(tick(ns_at(11, 40)))
        path = after.close_window(calendar.window_for_ns(ns_at(11, 0)))
        assert pq.read_table(path).num_rows == 2
        assert sorted(p.name for p in path.parent.iterdir()) == ["part-1100.parquet"]

    def test_empty_window_still_writes_a_valid_file(self, tmp_path):
        calendar = make_calendar()
        path = StreamWriter("depth", tmp_path, calendar, META).close_window(calendar.windows(DAY)[0])
        assert validate_file(path) == [] and pq.read_table(path).num_rows == 0

    def test_rows_after_close_are_counted_not_written(self, tmp_path):
        calendar = make_calendar()
        writer = StreamWriter("ticks", tmp_path, calendar, META)
        window = calendar.window_for_ns(ns_at(9, 30))
        writer.close_window(window)
        writer.add(tick(ns_at(9, 59)))
        assert writer.late_rows == 1


# ── subscription planning ────────────────────────────────────────────────

class FakeOptionMaster:
    STRIKES = [24800, 24900, 25000, 25100, 25200, 25300]

    def upcoming_expiries(self, underlying, now=None):
        return ["2026-10-13", "2026-10-20"]

    def get_strike_chain(self, underlying, expiry):
        base = 1000 if underlying == "NIFTY" else 5000
        return {str(s): {"ce_token": str(base + i * 2), "pe_token": str(base + i * 2 + 1),
                         "ce_tsym": f"{underlying}C{s}", "pe_tsym": f"{underlying}P{s}", "lot_size": 65}
                for i, s in enumerate(self.STRIKES)}


class FakeFutureMaster:
    def get_expiries(self, underlying):
        return ["2026-10-27", "2026-11-24", "2026-12-29"]

    def get_contract(self, underlying, expiry):
        token = {"NIFTY": 9000, "BANKNIFTY": 9100}[underlying] + int(expiry[5:7])
        return {"token": str(token), "tsym": f"{underlying}{expiry}F", "lot_size": 65}


def make_planner(config):
    planner = SubscriptionPlanner(config, option_master=FakeOptionMaster(), future_master=FakeFutureMaster())
    planner.start_day(datetime.combine(DAY, time(9, 0), IST))
    return planner


class TestSubscriptionPlanner:

    def test_initial_plan_without_spot_has_indices_and_futures_only(self, tmp_path):
        touchline, depth = make_planner(make_config(tmp_path)).plan(lambda u: None)
        assert {"NSE|26000", "NSE|26009", "NSE|26017"} <= touchline
        assert {"NFO|9010", "NFO|9011", "NFO|9110", "NFO|9111"} <= touchline   # 2 months each
        assert depth == {"NFO|9010", "NFO|9110"}                                 # front month only
        assert not any(key.startswith("NFO|1") for key in touchline)

    def test_atm_window_and_hysteresis(self, tmp_path):
        planner = make_planner(make_config(tmp_path))     # +/- 1 strike, recentre after 2
        spot = {"NIFTY": 25010.0, "BANKNIFTY": None}
        touchline, _ = planner.plan(spot.get)
        nifty_options = {k for k in touchline if planner.instruments.get(k, {}).get("instrument_type") == "OPT"}
        strikes = sorted({planner.instruments[k]["strike"] for k in nifty_options})
        assert strikes == [24900, 25000, 25100] and len(nifty_options) == 6
        spot["NIFTY"] = 25110.0                            # moved 1 strike: no change
        touchline_1, _ = planner.plan(spot.get)
        assert touchline_1 == touchline
        spot["NIFTY"] = 25210.0                            # moved 2 strikes: re-centre
        touchline_2, _ = planner.plan(spot.get)
        assert sorted({planner.instruments[k]["strike"] for k in touchline_2 if k in planner.option_keys()}) == [25100, 25200, 25300]


# ── recorder end to end (fake feed) ──────────────────────────────────────

class FakeFeed:
    """Ref-counted like ShoonyaOptionFeed; product_held simulates tokens the
    product already subscribes to."""

    def __init__(self, product_held=()):
        self.is_connected = True
        self.handlers = []
        self.calls = []
        self.touchline, self.depth = set(), set()
        self.product_held = set(product_held)
        self.resubscribed = []

    def on_raw_tick(self, handler):
        self.handlers.append(handler)

    def subscribed_count(self, key):
        return int(key in self.touchline) + int(key in self.product_held)

    def depth_subscribed_count(self, key):
        return int(key in self.depth)

    def ensure_subscribed(self, keys):
        self.calls.append(("subscribe", frozenset(keys)))
        self.touchline |= set(keys)

    def resubscribe(self, keys, feed_type=1):
        self.resubscribed.append((frozenset(keys), feed_type))

    def release(self, keys):
        self.calls.append(("release", frozenset(keys)))
        self.touchline -= set(keys)

    def ensure_depth_subscribed(self, keys):
        self.depth |= set(keys)

    def release_depth(self, keys):
        self.depth -= set(keys)

    def send(self, raw):
        for handler in self.handlers:
            handler(raw)


class Clock:
    def __init__(self, ns):
        self.ns = ns

    def __call__(self):
        return self.ns


def make_recorder(tmp_path, clock, feed=None, **config):
    config = make_config(tmp_path, **config)
    feed = feed or FakeFeed()
    recorder = MarketRecorder(feed, config, planner=make_planner(config), clock_ns=clock)
    return recorder, feed


def pump(recorder, clock, at_ns, frames=()):
    """Delivers frames stamped at at_ns, then runs one housekeeping pass."""
    clock.ns = at_ns
    for raw in frames:
        recorder._queue.append((at_ns, raw))
    recorder._drain()
    recorder._next = dict.fromkeys(recorder._next, 0.0)
    recorder._housekeeping(0.0)


class TestRecorderSession:

    def test_full_day_produces_valid_hourly_files(self, tmp_path):
        clock = Clock(ns_at(9, 0, 1))
        recorder, feed = make_recorder(tmp_path, clock)
        pump(recorder, clock, ns_at(9, 0, 1))
        assert recorder._accepting and "NSE|26000" in feed.touchline
        # Spot arrives -> options get subscribed on the next plan.
        pump(recorder, clock, ns_at(9, 0, 2), [{"t": "tk", "e": "NSE", "tk": "26000", "lp": "25010", "c": "24950"}])
        option_key = next(k for k in feed.touchline if recorder.planner.instruments.get(k, {}).get("instrument_type") == "OPT")
        token = option_key.split("|")[1]
        pump(recorder, clock, ns_at(9, 30), [{"t": "tk", "e": "NFO", "tk": token, "lp": "120", "bp1": "119", "sp1": "121"},
                                             {"t": "dk", "e": "NFO", "tk": "9010", "bp1": "25050", "bq1": "65"},
                                             {"t": "tk", "e": "NFO", "tk": "424242", "lp": "1"}])   # not ours: ignored
        pump(recorder, clock, ns_at(10, 0, 3))             # 09:00 window closes after the grace period
        day_dir = tmp_path / "market" / "12-10-2026"
        for stream in ("ticks", "depth", "chain", "events"):
            path = day_dir / stream / "part-0900.parquet"
            assert path.exists() and validate_file(path) == [], stream
        ticks = pq.read_table(day_dir / "ticks" / "part-0900.parquet")
        assert "424242" not in ticks.column("token").to_pylist()
        assert pq.read_table(day_dir / "chain" / "part-0900.parquet").num_rows >= 1
        assert validate_file(day_dir / "instruments" / "snapshot.parquet") == []
        events = pq.read_table(day_dir / "events" / "part-0900.parquet").column("event_type").to_pylist()
        assert events[0] == "RECORDER_START" and "SUBSCRIBE" in events

        pump(recorder, clock, ns_at(15, 30, 1))            # session end: last window closed, all released
        assert not recorder._accepting and not feed.touchline and not feed.depth
        assert (day_dir / "ticks" / "part-1500.parquet").exists()
        last_events = pq.read_table(day_dir / "events" / "part-1500.parquet").column("event_type").to_pylist()
        assert "RECORDER_STOP" in last_events

    def test_subscribe_happens_before_release(self, tmp_path):
        clock = Clock(ns_at(9, 0, 1))
        recorder, feed = make_recorder(tmp_path, clock)
        pump(recorder, clock, ns_at(9, 0, 1))                # session starts, indices subscribed
        pump(recorder, clock, ns_at(9, 0, 2), [{"t": "tk", "e": "NSE", "tk": "26000", "lp": "25010"}])
        pump(recorder, clock, ns_at(9, 0, 3))                # options around 25000 subscribed
        feed.calls.clear()
        pump(recorder, clock, ns_at(9, 1), [{"t": "tf", "e": "NSE", "tk": "26000", "lp": "25210"}])
        pump(recorder, clock, ns_at(9, 1, 1))
        kinds = [kind for kind, _ in feed.calls]
        assert kinds == ["subscribe", "release"]

    def test_ws_disconnect_and_queue_overflow_become_events(self, tmp_path):
        clock = Clock(ns_at(9, 0, 1))
        recorder, feed = make_recorder(tmp_path, clock, queue_max=1000)
        pump(recorder, clock, ns_at(9, 0, 1))
        feed.is_connected = False
        pump(recorder, clock, ns_at(9, 5))
        feed.is_connected = True
        recorder._queue_max = 0
        feed.send({"t": "tf", "e": "NSE", "tk": "26000", "lp": "1"})   # dropped, never blocks
        pump(recorder, clock, ns_at(9, 6))
        pump(recorder, clock, ns_at(10, 0, 3))
        events = pq.read_table(tmp_path / "market" / "12-10-2026" / "events" / "part-0900.parquet").to_pylist()
        types = [e["event_type"] for e in events]
        assert {"WS_DISCONNECT", "WS_RECONNECT", "GAP", "QUEUE_OVERFLOW"} <= set(types)
        gap = next(e for e in events if e["event_type"] == "GAP")
        assert gap["from_ns"] == ns_at(9, 5) and gap["to_ns"] == ns_at(9, 6)

    def test_restart_records_gap_and_merges_window(self, tmp_path):
        clock = Clock(ns_at(9, 10))
        first, feed = make_recorder(tmp_path, clock)
        pump(first, clock, ns_at(9, 10))
        pump(first, clock, ns_at(9, 10, 1), [{"t": "tk", "e": "NSE", "tk": "26000", "lp": "25000"}])
        first._shutdown()                                   # journal flushed, window left open

        clock.ns = ns_at(9, 40)
        second, _ = make_recorder(tmp_path, clock)
        second._recover()
        pump(second, clock, ns_at(9, 40))
        pump(second, clock, ns_at(9, 40, 1), [{"t": "tk", "e": "NSE", "tk": "26000", "lp": "25100"}])
        pump(second, clock, ns_at(10, 0, 3))
        day_dir = tmp_path / "market" / "12-10-2026"
        assert sorted(p.name for p in (day_dir / "ticks").iterdir()) == ["part-0900.parquet"]
        assert pq.read_table(day_dir / "ticks" / "part-0900.parquet").column("ltp").to_pylist() == [25000.0, 25100.0]
        events = pq.read_table(day_dir / "events" / "part-0900.parquet").to_pylist()
        gap = next(e for e in events if e["event_type"] == "GAP")
        assert gap["detail"] == "recorder restart"
        assert gap["from_ns"] == ns_at(9, 10, 1) and gap["to_ns"] == ns_at(9, 40)

    def test_outside_session_tap_ignores_frames(self, tmp_path):
        clock = Clock(ns_at(8, 0))
        recorder, feed = make_recorder(tmp_path, clock)
        pump(recorder, clock, ns_at(8, 0))
        feed.send({"t": "tk", "e": "NSE", "tk": "26000", "lp": "1"})
        assert not recorder._queue and not feed.touchline

    def test_tokens_already_held_by_the_product_are_resubscribed(self, tmp_path):
        """Review #1: ref-counting sends no subscribe frame for a token the
        product holds, so the recorder must ask for a fresh full frame."""
        clock = Clock(ns_at(9, 0, 1))
        recorder, feed = make_recorder(tmp_path, clock, feed=FakeFeed(product_held={"NSE|26000"}))
        pump(recorder, clock, ns_at(9, 0, 1))
        resent = set().union(*(keys for keys, _ in feed.resubscribed))
        assert resent == {"NSE|26000"}

    def test_failed_session_start_backs_off(self, tmp_path):
        """Review #2: a failing start is retried once a minute, not every second."""
        clock = Clock(ns_at(9, 0, 1))
        recorder, _ = make_recorder(tmp_path, clock)
        attempts = []

        def broken_start(now_ns):
            attempts.append(now_ns)
            raise RuntimeError("master not loaded")

        recorder._begin_session = broken_start
        for second in range(30):
            clock.ns = ns_at(9, 0, 1 + second)
            recorder._next = dict.fromkeys(recorder._next, 0.0)
            recorder._housekeeping(float(second))
        assert len(attempts) == 1
        clock.ns = ns_at(9, 1, 5)
        recorder._housekeeping(61.0)
        assert len(attempts) == 2

    def test_plan_load_errors_do_not_stop_the_session(self, tmp_path):
        class BrokenOptions(FakeOptionMaster):
            def upcoming_expiries(self, underlying, now=None):
                return ["not-a-date"]

        config = make_config(tmp_path)
        planner = SubscriptionPlanner(config, option_master=BrokenOptions(), future_master=FakeFutureMaster())
        planner.start_day(datetime.combine(DAY, time(9, 0), IST))
        assert planner.load_errors and "NFO|9010" in planner.instruments   # futures still planned

    def test_session_end_unsubscribe_events_are_recorded(self, tmp_path):
        """Review #4: UNSUBSCRIBE events at the close land in the last window."""
        clock = Clock(ns_at(15, 10))
        recorder, _ = make_recorder(tmp_path, clock)
        pump(recorder, clock, ns_at(15, 10))
        pump(recorder, clock, ns_at(15, 30, 1))
        events = pq.read_table(tmp_path / "market" / "12-10-2026" / "events" / "part-1500.parquet").to_pylist()
        subscribed = {e["token"] for e in events if e["event_type"] == "SUBSCRIBE"}
        unsubscribed = {e["token"] for e in events if e["event_type"] == "UNSUBSCRIBE"}
        assert subscribed and subscribed == unsubscribed

    def test_window_waits_until_its_frames_are_written(self, tmp_path):
        """No late rows: a window is not closed while frames stamped inside it are queued."""
        clock = Clock(ns_at(9, 0, 1))
        recorder, _ = make_recorder(tmp_path, clock)
        pump(recorder, clock, ns_at(9, 0, 1))
        original_drain_backlog = recorder._drain_backlog
        recorder._drain_backlog = lambda: None          # simulate a slow worker: queue not drained yet
        recorder._queue.append((ns_at(9, 59, 59), {"t": "tf", "e": "NSE", "tk": "26000", "lp": "25000"}))
        clock.ns = ns_at(10, 0, 5)
        recorder._close_finished_windows(clock.ns)
        ticks_file = tmp_path / "market" / "12-10-2026" / "ticks" / "part-0900.parquet"
        assert not ticks_file.exists()
        recorder._drain_backlog = original_drain_backlog
        recorder._close_finished_windows(clock.ns)
        assert ticks_file.exists() and pq.read_table(ticks_file).num_rows == 1
        assert recorder.writers["ticks"].late_rows == 0

    def test_backlog_drain_is_bounded(self, tmp_path):
        """Review #5: frames arriving during a drain don't keep the worker in it."""
        clock = Clock(ns_at(9, 0, 1))
        recorder, feed = make_recorder(tmp_path, clock)
        pump(recorder, clock, ns_at(9, 0, 1))
        refill = {"left": 50}
        original_ingest = recorder._ingest

        def ingest_and_refill(recv_ns, raw):
            original_ingest(recv_ns, raw)
            if refill["left"] > 0:
                refill["left"] -= 1
                recorder._queue.append((recv_ns, raw))   # a new frame for every one processed

        recorder._ingest = ingest_and_refill
        for _ in range(10):
            recorder._queue.append((ns_at(9, 0, 2), {"t": "tf", "e": "NSE", "tk": "26000", "lp": "1"}))
        recorder._drain_backlog()
        assert len(recorder._queue) > 0   # returned instead of chasing the refills


# ── end-of-day upload ────────────────────────────────────────────────────

class FakeDrive:
    def __init__(self):
        self.items = {}     # id -> {name, parent, md5, folder}
        self.uploads = 0
        self.order = []

    def _new(self, **item):
        self.next_id = getattr(self, "next_id", 0) + 1   # never reused, even after deletes
        item_id = f"id{self.next_id}"
        self.items[item_id] = item
        return item_id

    def find_child(self, parent_id, name, folder=False):
        for item_id, item in self.items.items():
            if item["parent"] == parent_id and item["name"] == name and item["folder"] == folder:
                return {"id": item_id, "md5Checksum": item.get("md5")}
        return None

    def folder_exists(self, folder_id):
        return folder_id in self.items

    def create_folder(self, parent_id, name):
        return self._new(name=name, parent=parent_id, folder=True)

    def upload(self, parent_id, path, mime_type, expected_md5=None):
        if parent_id not in self.items:
            raise NotFound()
        self.uploads += 1
        self.order.append(path.name)
        item_id = self._new(name=path.name, parent=parent_id, folder=False, md5=md5_of(path), size=path.stat().st_size,
                            content=path.read_bytes() if path.suffix == ".json" else None)
        return {"id": item_id, "md5Checksum": md5_of(path)}

    def download_text(self, file_id):
        return self.items[file_id]["content"].decode("utf-8")

    def update_content(self, file_id, path, mime_type):
        self.items[file_id]["md5"] = md5_of(path)
        return {"id": file_id, "md5Checksum": md5_of(path)}

    def list_children(self, parent_id, folders_only=False):
        return [{"id": item_id, "name": item["name"], "size": item.get("size"), "md5Checksum": item.get("md5"),
                 "mimeType": "application/vnd.google-apps.folder" if item["folder"] else "application/octet-stream"}
                for item_id, item in self.items.items()
                if item["parent"] == parent_id and (item["folder"] or not folders_only)]

    def delete(self, item_id):
        doomed = {item_id}
        while True:
            more = {i for i, item in self.items.items() if item["parent"] in doomed} - doomed
            if not more:
                break
            doomed |= more
        for i in doomed:
            self.items.pop(i, None)

    def storage_usage_bytes(self):
        return self.account_usage, None

    account_usage = 1


class NotFound(Exception):
    class resp:
        status = 404


def write_day(data_dir):
    calendar = make_calendar()
    writer = StreamWriter("ticks", data_dir, calendar, META)
    writer.add(tick(ns_at(9, 30)))
    writer.close_window(calendar.windows(DAY)[0])
    StreamWriter("events", data_dir, calendar, META).close_window(calendar.windows(DAY)[0])


class TestDayUploader:

    def test_uploads_day_with_manifest_last_and_never_twice(self, tmp_path):
        write_day(tmp_path)
        drive = FakeDrive()
        uploader = DayUploader(tmp_path, "PrimePipData", lambda: drive)
        assert uploader.pending_days(finished_before=date(2026, 10, 13)) == [DAY]
        result = uploader.upload_day(DAY)
        assert result["status"] == "uploaded" and result["files"] == 2
        assert drive.order[-1] == MANIFEST_NAME
        folders = {item["name"] for item in drive.items.values() if item["folder"]}
        assert {"PrimePipData", "12-10-2026", "ticks", "events"} <= folders
        manifest = json.loads((tmp_path / "12-10-2026" / MANIFEST_NAME).read_text())
        assert manifest["rows_by_stream"] == {"events": 0, "ticks": 1}

        # Same day again (fresh process, ledger on disk): nothing re-sent.
        again = DayUploader(tmp_path, "PrimePipData", lambda: drive)
        assert again.pending_days(finished_before=date(2026, 10, 13)) == []
        uploads = drive.uploads
        again.upload_day(DAY)
        assert drive.uploads == uploads

    def test_existing_remote_file_is_not_duplicated(self, tmp_path):
        write_day(tmp_path)
        drive = FakeDrive()
        DayUploader(tmp_path, "PrimePipData", lambda: drive).upload_day(DAY)
        (tmp_path / ".upload_ledger.jsonl").unlink()          # ledger lost
        before = len(drive.items)
        DayUploader(tmp_path, "PrimePipData", lambda: drive).upload_day(DAY)
        assert len(drive.items) == before

    def test_day_without_ticks_is_not_uploaded(self, tmp_path):
        calendar = make_calendar()
        StreamWriter("ticks", tmp_path, calendar, META).close_window(calendar.windows(DAY)[0])
        drive = FakeDrive()
        result = DayUploader(tmp_path, "PrimePipData", lambda: drive).upload_day(DAY)
        assert result["status"] == "skipped_empty" and drive.uploads == 0

    def test_open_journal_blocks_upload(self, tmp_path):
        write_day(tmp_path)
        writer = StreamWriter("ticks", tmp_path, make_calendar(), META)
        writer.add(tick(ns_at(10, 15)))
        writer.flush()
        assert DayUploader(tmp_path, "PrimePipData", FakeDrive).pending_days(date(2026, 10, 13)) == []

    def test_local_retention_only_removes_uploaded_days(self, tmp_path):
        write_day(tmp_path)
        uploader = DayUploader(tmp_path, "PrimePipData", FakeDrive)
        assert uploader.prune_local(7, date(2026, 10, 30)) == []      # not uploaded yet
        uploader.upload_day(DAY)
        assert uploader.prune_local(7, date(2026, 10, 15)) == []      # too recent
        assert uploader.prune_local(7, date(2026, 10, 30)) == ["12-10-2026"]

    def test_budget_uses_the_recorders_own_files_not_the_account(self, tmp_path):
        """Review #3: a full Gmail/Photos account must not trigger deletions."""
        write_day(tmp_path)
        drive = FakeDrive()
        drive.account_usage = 14 * 10**9          # the account is nearly full of other data
        uploader = DayUploader(tmp_path, "PrimePipData", lambda: drive)
        uploader.upload_day(DAY)
        status = uploader.enforce_drive_budget(10.0, 60, date(2027, 1, 30))
        assert status["pruned"] == [] and status["recorder_usage_gb"] < 0.001
        # Over the recorder's own budget: the oldest day's ticks go first, chain/events stay.
        status = uploader.enforce_drive_budget(1e-9, 60, date(2027, 1, 30))
        assert status["pruned"] == ["12-10-2026/ticks"]
        names = {item["name"] for item in drive.items.values()}
        assert "events" in names and "ticks" not in names

    def test_pruned_folder_is_recreated_on_reupload(self, tmp_path):
        """Review #10: a cached folder id deleted in Drive is not reused."""
        write_day(tmp_path)
        drive = FakeDrive()
        uploader = DayUploader(tmp_path, "PrimePipData", lambda: drive)
        uploader.upload_day(DAY)
        uploader.enforce_drive_budget(1e-9, 60, date(2027, 1, 30))       # deletes .../ticks
        (tmp_path / ".upload_ledger.jsonl").unlink()                       # ledger lost
        fresh = DayUploader(tmp_path, "PrimePipData", lambda: drive)       # stale folder cache on disk
        assert fresh.upload_day(DAY)["status"] == "uploaded"
        assert any(item["name"] == "ticks" and item["folder"] for item in drive.items.values())

    def test_cached_folders_are_checked_once_per_process(self, tmp_path):
        write_day(tmp_path)
        drive = FakeDrive()
        DayUploader(tmp_path, "PrimePipData", lambda: drive).upload_day(DAY)
        checks = []
        original = drive.folder_exists
        drive.folder_exists = lambda folder_id: checks.append(folder_id) or original(folder_id)
        (tmp_path / ".upload_ledger.jsonl").unlink()
        DayUploader(tmp_path, "PrimePipData", lambda: drive).upload_day(DAY)
        assert len(checks) == len(set(checks))   # each cached folder verified at most once


class TestDriveAudit:

    def _uploaded(self, tmp_path):
        import pyarrow as pa
        from service.marketRecorder import contract
        from service.marketRecorder.driveAudit import DriveAudit
        from service.marketRecorder.streamWriter import write_parquet_atomic
        write_day(tmp_path)
        snapshot = pa.Table.from_pylist([{"exchange": "NSE", "token": "26000", "tsym": "NIFTY", "underlying": "NIFTY",
                                          "instrument_type": "INDEX", "expiry": None, "strike": None, "opt_type": None,
                                          "lot_size": None, "tick_size": None, "freeze_qty": None}], schema=contract.INSTRUMENTS)
        write_parquet_atomic(snapshot.replace_schema_metadata({
            "schema_version": contract.SCHEMA_VERSION, "stream": "instruments", "trading_date": DAY.isoformat(),
            "row_count": "1", "recorder_version": "test", "vm_hostname": "test-vm"}),
            tmp_path / "12-10-2026" / "instruments" / "snapshot.parquet")
        drive = FakeDrive()
        DayUploader(tmp_path, "PrimePipData", lambda: drive).upload_day(DAY)
        return drive, DriveAudit(drive, "PrimePipData", make_calendar())

    def _file_id(self, drive, name):
        return next(i for i, item in drive.items.items() if item["name"] == name)

    def test_consistent_day_passes_and_missing_windows_are_noted(self, tmp_path):
        _, audit = self._uploaded(tmp_path)
        report = audit.audit()
        day = report["days"]["12-10-2026"]
        assert report["ok"] and day["problems"] == []
        assert any("ticks: no file for" in note for note in day["notes"])   # sample has only the 09:00 window

    def test_missing_file_is_a_problem(self, tmp_path):
        drive, audit = self._uploaded(tmp_path)
        drive.items.pop(self._file_id(drive, "part-0900.parquet"))
        report = audit.audit()
        assert not report["ok"] and "not in Drive" in report["days"]["12-10-2026"]["problems"][0]

    def test_duplicate_and_changed_files_are_problems(self, tmp_path):
        drive, audit = self._uploaded(tmp_path)
        original = drive.items[self._file_id(drive, "part-0900.parquet")]
        drive._new(**{**original, "md5": "different"})
        problems = " ".join(audit.audit()["days"]["12-10-2026"]["problems"])
        assert "duplicate files" in problems

    def test_day_without_manifest_is_incomplete(self, tmp_path):
        drive, audit = self._uploaded(tmp_path)
        drive.items.pop(self._file_id(drive, MANIFEST_NAME))
        assert "incomplete" in audit.audit()["days"]["12-10-2026"]["problems"][0]

    def test_budget_pruned_streams_are_notes_not_problems(self, tmp_path):
        """Review #6: deliberate pruning (recorded in _pruned.json) keeps the audit green."""
        drive, audit = self._uploaded(tmp_path)
        DayUploader(tmp_path, "PrimePipData", lambda: drive).enforce_drive_budget(1e-9, 60, date(2027, 1, 30))
        assert any(item["name"] == "_pruned.json" for item in drive.items.values())
        report = audit.audit()
        day = report["days"]["12-10-2026"]
        assert report["ok"] and day["problems"] == []
        assert any("pruned by the Drive budget" in note for note in day["notes"])

    def test_stray_folder_is_reported(self, tmp_path):
        drive, audit = self._uploaded(tmp_path)
        root_id = next(i for i, item in drive.items.items() if item["name"] == "PrimePipData")
        drive.create_folder(root_id, "random stuff")
        report = audit.audit()
        assert not report["ok"] and "not a DD-MM-YYYY" in report["days"]["random stuff"]["problems"][0]


class TestUploadScheduler:

    def test_close_stops_an_upload_without_blocking(self, tmp_path):
        """Review #6: shutdown never waits for Drive retries."""
        import threading
        import time as _time
        from service.marketRecorder.driveClient import UploadCancelled
        from service.marketRecorder.uploadScheduler import UploadScheduler

        write_day(tmp_path)
        scheduler = UploadScheduler(make_config(tmp_path, data_dir=tmp_path))
        started = threading.Event()

        class SlowDrive(FakeDrive):
            def find_child(self, parent_id, name, folder=False):
                started.set()
                if scheduler._stop.wait(30):
                    raise UploadCancelled(name)
                return super().find_child(parent_id, name, folder)

        scheduler.uploader._client_factory = SlowDrive
        worker = threading.Thread(target=lambda: scheduler.uploader.pending_days(date(2026, 10, 13)) and
                                  _safe_upload(scheduler), daemon=True)
        worker.start()
        assert started.wait(5)
        begin = _time.perf_counter()
        scheduler.close()
        worker.join(5)
        assert not worker.is_alive() and _time.perf_counter() - begin < 2


def _safe_upload(scheduler):
    try:
        scheduler.uploader.upload_day(DAY)
    except Exception:
        pass


class TestDriveCreateRetries:
    """Review #5: a create whose response was lost is not repeated."""

    def _client(self, find_result):
        import threading
        from service.marketRecorder.driveClient import DriveClient
        client = DriveClient.__new__(DriveClient)
        client._stop_event = threading.Event()
        client.find_child = lambda parent, name, folder=False: find_result
        return client

    def test_lost_response_returns_the_existing_item(self, monkeypatch):
        import service.marketRecorder.driveClient as dc
        monkeypatch.setattr(dc.random, "random", lambda: 0.0)
        client = self._client({"id": "already", "md5Checksum": "abc"})
        client._stop_event.wait = lambda delay: False
        calls = []

        class Request:
            def execute(self):
                calls.append(1)
                raise TimeoutError("response lost")

        result = client._create("upload x", Request, lambda: client.find_child(None, "x"))
        assert result["id"] == "already" and len(calls) == 1      # checked, not created again

    def test_nothing_there_retries_the_create(self):
        client = self._client(None)
        client._stop_event.wait = lambda delay: False
        outcomes = iter([TimeoutError("lost"), {"id": "new"}])

        class Request:
            def execute(self):
                outcome = next(outcomes)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        assert client._create("upload x", Request, lambda: None)["id"] == "new"


class TestBrokerCandleCall:
    """Review #10: one timed TPSeries round trip behind both candle methods."""

    def test_raw_call_is_timed_and_shared(self):
        from unittest.mock import MagicMock
        from marketengine.ShoonyaConnection import ShoonyaConnection
        assert hasattr(ShoonyaConnection.get_raw_time_price_series, "__wrapped__")
        conn = ShoonyaConnection.__new__(ShoonyaConnection)
        conn._connected, conn._api = True, MagicMock()
        conn._api.get_time_price_series.return_value = {"stat": "Not_Ok", "emsg": "no data"}
        assert conn.get_raw_time_price_series("NFO", "1", 0, 1) == []
        assert conn.get_time_price_series("NFO", "1", "1") is None
        conn._api.get_time_price_series.return_value = [{"time": "12-10-2026 09:15:00", "into": "1", "inth": "1",
                                                         "intl": "1", "intc": "1", "intv": "5", "oi": "7"}]
        assert conn.get_raw_time_price_series("NFO", "1", 0, 1)[0]["oi"] == "7"
        assert conn.get_time_price_series("NFO", "1", "1")[0]["volume"] == 5


class TestInternalStatus:

    def test_status_runs_on_its_own_thread_and_never_piles_up(self):
        """Review #8: not on the broker executor; one call at a time per component."""
        import asyncio
        import threading
        from api.internal import _StatusRunner
        release = threading.Event()
        threads = []

        class Slow:
            def status(self):
                threads.append(threading.current_thread().name)
                release.wait(2)
                return {"ok": True}

        async def run():
            runner = _StatusRunner()
            component = Slow()
            first = asyncio.ensure_future(runner.status(component))
            await asyncio.sleep(0.05)
            second = await runner.status(component)     # first still running
            release.set()
            return await first, second

        first, second = asyncio.run(run())
        assert first == {"ok": True} and "status_stale" in second
        assert threads == ["diagnostics-status"]

    def test_failing_recorder_status_does_not_fail_the_endpoint(self):
        """Review #7: status runs off the loop and errors are contained."""
        import asyncio
        from api.internal import _recorder_status

        class Broken:
            def status(self):
                raise PermissionError("data dir unreadable")

        class Fine:
            def status(self):
                return {"ok": True}

        async def run():
            return await asyncio.gather(_recorder_status(Broken()), _recorder_status(Fine()), _recorder_status(None))

        broken, fine, missing = asyncio.run(run())
        assert "status_error" in broken and fine == {"ok": True} and missing == {"enabled": False}


# ── broker 1-minute candles ──────────────────────────────────────────────

class FakeShoonya:
    is_connected = True

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def get_raw_time_price_series(self, exchange, token, starttime, endtime, interval):
        self.calls.append((exchange, token, interval))
        if f"{exchange}|{token}" in self.fail:
            return None
        return [   # newest first, as Shoonya sends them
            {"time": "12-10-2026 09:16:00", "into": "101", "inth": "103", "intl": "100", "intc": "102",
             "intvwap": "101.5", "intv": "0", "v": "500", "oi": "1000", "intoi": "0"},
            {"time": "12-10-2026 09:15:00", "into": "100", "inth": "102", "intl": "99", "intc": "101",
             "intvwap": "100.5", "intv": "500", "v": "500", "oi": "1000", "intoi": "25"},
        ]


def record_morning(tmp_path, tick_at=(9, 15, 10)):
    """09:00 session start, one option tick, then the session is ended so
    every window is closed (as at the real close, before candles are fetched)."""
    clock = Clock(ns_at(9, 0, 1))
    recorder, feed = make_recorder(tmp_path, clock)
    pump(recorder, clock, ns_at(9, 0, 1))
    pump(recorder, clock, ns_at(9, 0, 2), [{"t": "tk", "e": "NSE", "tk": "26000", "lp": "25010"}])
    pump(recorder, clock, ns_at(9, 0, 3))
    option_key = sorted(k for k in feed.touchline if recorder.planner.instruments.get(k, {}).get("instrument_type") == "OPT")[0]
    if tick_at:
        pump(recorder, clock, ns_at(*tick_at), [{"t": "tf", "e": "NFO", "tk": option_key.split("|")[1], "lp": "101"}])
    pump(recorder, clock, ns_at(10, 0, 3))
    pump(recorder, clock, ns_at(15, 30, 1))   # session end: every window closed, journal empty
    return recorder, option_key


class TestInfoLogs:
    """The INFO trail that shows, from the log alone, that a day was recorded."""

    def test_a_day_leaves_a_readable_info_trail(self, tmp_path, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="service.marketRecorder")
        clock = Clock(ns_at(8, 50))
        recorder, feed = make_recorder(tmp_path, clock)
        pump(recorder, clock, ns_at(8, 50))                                    # before the open
        pump(recorder, clock, ns_at(9, 0, 1))
        pump(recorder, clock, ns_at(9, 0, 2), [{"t": "tk", "e": "NSE", "tk": "26000", "lp": "25010"}])
        recorder._next_heartbeat = 0.0
        pump(recorder, clock, ns_at(9, 5))                                     # heartbeat
        feed.is_connected = False
        pump(recorder, clock, ns_at(9, 6))
        feed.is_connected = True
        pump(recorder, clock, ns_at(9, 7))
        recorder._next_heartbeat = 0.0
        pump(recorder, clock, ns_at(9, 10))                                    # heartbeat, no new ticks
        pump(recorder, clock, ns_at(15, 30, 1))                                # session end
        pump(recorder, clock, ns_at(15, 30, 2))                                # next pass: idle
        text = "\n".join(r.getMessage() for r in caplog.records)
        for expected in ("Idle (outside session hours) - next recording starts Mon 12-10-2026 09:00 IST",
                         "Session started for 12-10-2026", "Websocket connected - recording",
                         "First ticks recorded at 09:00:02", "Heartbeat 09:05", "Websocket disconnected at 09:06:00",
                         "Websocket reconnected at 09:07:00", "No ticks recorded in the last 5 min",
                         "Closed ticks 12-10-2026 part-0900.parquet", "Session ended for 12-10-2026",
                         "next recording starts Tue 13-10-2026 09:00 IST"):
            assert expected in text, expected

    def test_heartbeat_never_raises(self, tmp_path):
        recorder, _ = make_recorder(tmp_path, Clock(ns_at(9, 0, 1)))
        recorder._heartbeat_base = None              # corrupt state
        recorder._log_heartbeat(datetime.combine(DAY, time(9, 5), IST))   # logged as a warning, no exception

    def test_upload_logs_missing_token(self, tmp_path, caplog):
        import logging
        from service.marketRecorder.uploadScheduler import UploadScheduler
        caplog.set_level(logging.INFO, logger="service.marketRecorder")
        UploadScheduler(make_config(tmp_path, drive_token_file=tmp_path / "missing.json"))._log_startup()
        assert any("Login token NOT found" in r.getMessage() for r in caplog.records)


class TestCandles:

    def _fetcher(self, recorder, shoonya):
        from service.marketRecorder.candleFetcher import DayCandleFetcher
        config = make_config(recorder._config.data_dir.parent, candles_calls_per_sec=1000.0, candles_enabled=True)
        fetcher = DayCandleFetcher(config, lambda: shoonya, recorder.calendar)
        fetcher.retry_wait_secs = 0.0
        return fetcher

    def test_candles_for_every_recorded_instrument_with_all_fields(self, tmp_path):
        recorder, option_key = record_morning(tmp_path)
        shoonya = FakeShoonya()
        result = self._fetcher(recorder, shoonya).fetch_day(DAY)
        assert result["status"] == "complete"
        subscribed = {key for key, _ in recorder.candles.subscriptions(DAY)[0].items()}
        assert {f"{e}|{t}" for e, t, _ in shoonya.calls} == subscribed and option_key in subscribed
        path = tmp_path / "market" / "12-10-2026" / "candles" / "candles_1m.parquet"
        assert validate_file(path) == []
        row = next(r for r in pq.read_table(path).to_pylist() if r["token"] == option_key.split("|")[1])
        assert (row["open"], row["vwap"], row["volume"], row["oi"], row["oi_change"]) == (100.0, 100.5, 500, 1000, 25)
        assert (tmp_path / "market" / "12-10-2026" / ".candles_done.json").exists()

    def test_check_flags_traded_minutes_without_ticks(self, tmp_path):
        recorder, option_key = record_morning(tmp_path, tick_at=None)   # the option never ticked
        result = self._fetcher(recorder, FakeShoonya()).fetch_day(DAY)
        check = result["check"]
        assert check["missing_unexplained"] >= 1 and option_key in check["unexplained_by_instrument"]

    def test_check_matches_when_ticks_present(self, tmp_path):
        recorder, option_key = record_morning(tmp_path)
        check = self._fetcher(recorder, FakeShoonya()).fetch_day(DAY)["check"]
        assert option_key not in check["unexplained_by_instrument"]

    def test_broker_down_leaves_no_marker_for_a_retry(self, tmp_path):
        recorder, _ = record_morning(tmp_path)
        shoonya = FakeShoonya()
        shoonya.is_connected = False
        fetcher = self._fetcher(recorder, shoonya)
        assert fetcher.fetch_day(DAY)["status"] == "broker_unavailable"
        assert not fetcher.has_marker(DAY)
        assert fetcher.days_missing_candles([DAY], date(2026, 10, 13), False) == [DAY]

    def test_failed_instrument_is_retried_then_reported(self, tmp_path):
        """Review #1: no marker until every instrument is in or the cap is hit;
        a retry fetches only what failed and keeps what was saved."""
        recorder, option_key = record_morning(tmp_path)
        shoonya = FakeShoonya(fail={option_key})
        fetcher = self._fetcher(recorder, shoonya)
        first = fetcher.fetch_day(DAY)
        assert first["status"] == "retrying" and first["failed"] == [option_key] and not fetcher.has_marker(DAY)
        shoonya.calls.clear()
        shoonya.fail.clear()                                    # the broker recovers
        second = fetcher.fetch_day(DAY)
        assert second["status"] == "complete" and fetcher.has_marker(DAY)
        assert [f"{e}|{t}" for e, t, _ in shoonya.calls] == [option_key]   # only the failed one again
        saved = pq.read_table(tmp_path / "market" / "12-10-2026" / "candles" / "candles_1m.parquet").to_pylist()
        assert len({(r["exchange"], r["token"]) for r in saved}) == len(recorder.candles.subscriptions(DAY)[0])

    def test_total_failure_writes_no_file_and_gives_up_at_the_cap(self, tmp_path):
        recorder, _ = record_morning(tmp_path)
        everything = set(recorder.candles.subscriptions(DAY)[0])
        fetcher = self._fetcher(recorder, FakeShoonya(fail=everything))
        fetcher._config.candles_max_attempts = 2
        assert fetcher.fetch_day(DAY)["status"] == "retrying"
        assert not (tmp_path / "market" / "12-10-2026" / "candles" / "candles_1m.parquet").exists()
        final = fetcher.fetch_day(DAY)
        assert final["status"] == "failed" and fetcher.has_marker(DAY)

    def test_open_journal_blocks_the_candle_download(self, tmp_path):
        """Review #3: events not merged yet -> no fetch, no marker."""
        recorder, _ = record_morning(tmp_path)
        writer = recorder.writers["events"]
        writer._closed.clear()          # simulate a crash: this window's segment was never merged
        writer.add({"ts_recv_ns": ns_at(10, 30), "event_type": "SUBSCRIBE", "stream": "ticks", "token": "NFO|1",
                    "from_ns": None, "to_ns": None, "detail": None})
        writer.flush()
        fetcher = self._fetcher(recorder, FakeShoonya())
        assert fetcher.fetch_day(DAY)["status"] == "journal_open" and not fetcher.has_marker(DAY)
        assert fetcher.days_missing_candles([DAY], date(2026, 10, 13), False) == []

    def test_candle_loop_waits_for_recovery_and_retries(self, tmp_path):
        """Review #2: one loop, started only after recovery, retried until settled."""
        import threading
        recorder, _ = record_morning(tmp_path)
        shoonya = FakeShoonya()
        shoonya.is_connected = False                       # broker down at the close
        recorder._config.candles_enabled = True
        recorder._config.candles_retry_secs = 0.05
        recorder.candles = self._fetcher(recorder, shoonya)
        recorder.candles._stop = recorder._stop
        recorder._clock_ns = lambda: ns_at(16, 30)
        thread = threading.Thread(target=recorder._candle_loop, daemon=True)
        thread.start()
        import time as _time
        _time.sleep(0.2)
        assert not recorder.candles.has_marker(DAY)        # recovery not signalled yet
        recorder._recovered.set()
        _time.sleep(0.3)
        assert not recorder.candles.has_marker(DAY)        # broker still down: retried, no marker
        shoonya.is_connected = True
        for _ in range(50):
            if recorder.candles.has_marker(DAY):
                break
            _time.sleep(0.05)
        recorder._stop.set()
        recorder._candle_wake.set()
        thread.join(2)
        assert recorder.candles.read_marker(DAY)["status"] == "complete"

    def test_late_candles_are_uploaded_after_the_day(self, tmp_path):
        """Review #2: candles finishing after the upload still reach Drive."""
        write_day(tmp_path)
        drive = FakeDrive()
        uploader = DayUploader(tmp_path, "PrimePipData", lambda: drive)
        uploader.upload_day(DAY)                                   # uploaded without candles
        assert uploader.pending_days(date(2026, 10, 13)) == []
        (tmp_path / "12-10-2026" / ".candles_done.json").write_text(json.dumps({"status": "complete"}))
        assert uploader.pending_days(date(2026, 10, 13)) == [DAY]  # late candles -> pending again
        uploader.upload_day(DAY)
        manifest_items = [i for i in drive.items.values() if i["name"] == MANIFEST_NAME]
        assert len(manifest_items) == 1                            # manifest updated, not duplicated
        assert uploader.pending_days(date(2026, 10, 13)) == []

    def test_upload_waits_for_todays_candles(self, tmp_path):
        from service.marketRecorder.uploadScheduler import UploadScheduler
        write_day(tmp_path)
        calendar = make_calendar()
        StreamWriter("events", tmp_path, calendar, META).close_window(calendar.windows(DAY)[-1])   # session closed
        config = make_config(tmp_path, data_dir=tmp_path, candles_enabled=True)
        scheduler = UploadScheduler(config)
        at_1620 = datetime.combine(DAY, time(16, 20), IST)
        ready = scheduler._ready(at_1620)
        assert not ready(DAY)                                     # today, no candles yet
        assert ready(date(2026, 10, 9))                            # earlier days never wait
        (tmp_path / "12-10-2026" / ".candles_done.json").write_text("{}")
        assert ready(DAY)
        (tmp_path / "12-10-2026" / ".candles_done.json").unlink()
        assert scheduler._ready(datetime.combine(DAY, time(17, 1), IST))(DAY)   # gives up waiting after 45 min

    def test_today_is_not_uploaded_before_its_session_is_closed(self, tmp_path):
        """Review #4: an early RECORDER_UPLOAD_AT can't upload a half-written day."""
        from service.marketRecorder.uploadScheduler import UploadScheduler
        write_day(tmp_path)
        config = make_config(tmp_path, data_dir=tmp_path, candles_enabled=False, upload_at=time(15, 0))
        scheduler = UploadScheduler(config)
        assert not scheduler._ready(datetime.combine(DAY, time(15, 0, 10), IST))(DAY)   # session still running
        assert not scheduler._ready(datetime.combine(DAY, time(15, 40), IST))(DAY)      # last window not written
        calendar = make_calendar()
        StreamWriter("events", tmp_path, calendar, META).close_window(calendar.windows(DAY)[-1])
        assert scheduler._ready(datetime.combine(DAY, time(15, 40), IST))(DAY)
        assert UploadScheduler(make_config(tmp_path / "x", data_dir=tmp_path / "x"))._ready(
            datetime.combine(DAY, time(17, 31), IST))(DAY)                                # fallback after 2 h

    def test_manifest_carries_the_candle_check(self, tmp_path):
        write_day(tmp_path)
        (tmp_path / "12-10-2026" / ".candles_done.json").write_text(json.dumps({"status": "complete", "secs": 3.2}))
        DayUploader(tmp_path, "PrimePipData", FakeDrive).upload_day(DAY)
        manifest = json.loads((tmp_path / "12-10-2026" / MANIFEST_NAME).read_text())
        assert manifest["candles"] == {"status": "complete"}


class TestDefaults:

    def test_records_past_the_close_and_uploads_after_candles(self):
        defaults = RecorderConfig()
        assert defaults.session_end == time(16, 0) and defaults.upload_at == time(16, 15)
        windows = TradingCalendar(defaults.session_start, defaults.session_end, 60).windows(DAY)
        assert len(windows) == 7 and windows[-1].file_name == "part-1500.parquet"


# ── NSE holiday list ─────────────────────────────────────────────────────

NSE_PAYLOAD = {"CM": [], "FO": [
    {"tradingDate": "20-Oct-2026", "weekDay": "Tuesday", "description": "Dussehra"},
    {"tradingDate": "08-Nov-2026", "weekDay": "Sunday", "description": "Diwali Laxmi Pujan*"},
    {"tradingDate": "garbage", "weekDay": "?", "description": "skipped"},
]}


class FakeNseSession:
    def __init__(self, payload=None, fail=False):
        self.headers = {}
        self._payload, self._fail = payload, fail

    def get(self, url, timeout=None, headers=None):
        from unittest.mock import MagicMock
        if self._fail and "api" in url:
            raise OSError("blocked")
        response = MagicMock()
        response.json.return_value = self._payload
        return response


class TestNseHolidays:

    def test_parse_marks_special_sessions_and_skips_bad_rows(self):
        from service.marketRecorder.nseHolidays import parse_holidays
        assert parse_holidays(NSE_PAYLOAD) == [
            {"date": "2026-10-20", "day": "Tuesday", "description": "Dussehra", "special": False},
            {"date": "2026-11-08", "day": "Sunday", "description": "Diwali Laxmi Pujan", "special": True},
        ]

    def test_refresh_writes_the_cache_and_calendar_uses_base_plus_cache(self, tmp_path):
        """Review #8: the tracked base file is never rewritten; the calendar
        combines it with the fetched cache."""
        from service.marketRecorder.nseHolidays import refresh_holiday_file
        base = tmp_path / "base.json"
        base.write_text(json.dumps({"holidays": ["2027-01-26"], "special_sessions": ["2026-11-08"]}))
        before = base.read_text()
        cache = tmp_path / "cache.json"
        assert refresh_holiday_file(cache, FakeNseSession(NSE_PAYLOAD))
        assert base.read_text() == before
        assert json.loads(cache.read_text())["holidays"] == ["2026-10-20", "2026-11-08"]
        calendar = TradingCalendar(time(9, 0), time(15, 30), 60, base, cache)
        assert not calendar.is_trading_day(date(2026, 10, 20))     # from NSE
        assert not calendar.is_trading_day(date(2027, 1, 26))      # hand-added for next year
        assert calendar.is_trading_day(date(2026, 11, 8))          # special session wins

    def test_refresh_keeps_other_years(self, tmp_path):
        from service.marketRecorder.nseHolidays import refresh_holiday_file
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({"holidays": ["2026-01-26", "2027-01-26"]}))
        assert refresh_holiday_file(cache, FakeNseSession(NSE_PAYLOAD))
        assert json.loads(cache.read_text())["holidays"] == ["2026-10-20", "2026-11-08", "2027-01-26"]

    def test_failure_keeps_the_saved_list(self, tmp_path):
        from service.marketRecorder.nseHolidays import refresh_holiday_file
        path = tmp_path / "nse_holidays.json"
        path.write_text(json.dumps({"holidays": ["2026-10-20"]}))
        assert not refresh_holiday_file(path, FakeNseSession(fail=True))
        assert not refresh_holiday_file(path, FakeNseSession({"FO": []}))
        assert json.loads(path.read_text())["holidays"] == ["2026-10-20"]


# ── shared feed: depth frames and depth subscriptions ────────────────────

class _InlineSender:
    def submit(self, label, send):
        send()
        return True

    def close(self):
        pass


class TestFeedDepthSupport:

    def _feed(self):
        from unittest.mock import MagicMock
        shoonya = MagicMock()
        feed = ShoonyaOptionFeed(shoonya, subscription_sender=_InlineSender())
        feed._api_instance = shoonya._api
        feed._socket_open = True
        return feed, shoonya

    def test_depth_frames_reach_raw_handlers_only(self):
        feed, _ = self._feed()
        raw_seen, ticks_seen = [], []
        feed.on_raw_tick(raw_seen.append)
        feed.on_tick(lambda key, tick: ticks_seen.append(key))
        feed._async_loop = object()
        feed._on_tick({"t": "df", "e": "NFO", "tk": "1", "bp1": "10"})
        assert raw_seen and not ticks_seen

    def test_depth_subscriptions_are_ref_counted_on_the_depth_feed(self):
        feed, shoonya = self._feed()
        feed.ensure_depth_subscribed({"NFO|1"})
        feed.ensure_depth_subscribed({"NFO|1"})
        shoonya._api.subscribe.assert_called_once_with(["NFO|1"], feed_type=DEPTH_FEED)
        feed.release_depth({"NFO|1"})
        shoonya._api.unsubscribe.assert_not_called()
        feed.release_depth({"NFO|1"})
        shoonya._api.unsubscribe.assert_called_once_with(["NFO|1"], feed_type=DEPTH_FEED)
        assert feed.subscribed_count("NFO|1") == 0      # touchline untouched

    def test_depth_frames_also_reach_touchline_consumers_of_that_token(self):
        """Review #7: if the broker sends a watched token's updates as depth
        frames, the product's tick handlers still get them."""
        feed, _ = self._feed()
        seen = []
        feed.on_tick(lambda key, tick: seen.append((key, tick)))
        feed._async_loop = object()
        feed.ensure_subscribed({"NFO|9"})          # the product watches it on touchline
        feed._on_tick({"t": "df", "e": "NFO", "tk": "9", "lp": "25100", "bp1": "25099"})
        assert seen == [("NFO|9", {"ltp": 25100.0, "bid": 25099.0})]

    def test_depth_release_refreshes_touchline_still_in_use(self):
        """Review #7: a depth unsubscribe can't leave the product's touchline dead."""
        feed, shoonya = self._feed()
        feed.ensure_subscribed({"NFO|9"})
        feed.ensure_depth_subscribed({"NFO|9"})
        shoonya._api.subscribe.reset_mock()
        feed.release_depth({"NFO|9"})
        shoonya._api.unsubscribe.assert_called_once_with(["NFO|9"], feed_type=DEPTH_FEED)
        shoonya._api.subscribe.assert_called_once_with(["NFO|9"])   # touchline re-sent after the 'ud'

    def test_reconnect_resubscribes_depth(self):
        feed, shoonya = self._feed()
        feed.ensure_depth_subscribed({"NFO|1"})
        shoonya._api.subscribe.reset_mock()
        feed._on_open()
        shoonya._api.subscribe.assert_called_with(["NFO|1"], feed_type=DEPTH_FEED)
