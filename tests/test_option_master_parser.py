"""
Unit tests for scripts/build_option_master.py's CSV parser, against a small
fixture mirroring the real Shoonya NFO_symbols.txt column layout:
Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize
"""

from scripts.build_option_master import BFO_SYMBOL_TO_UNDERLYING, BFO_UNDERLYINGS, parse_master_csv

FIXTURE_CSV = """Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,
NFO,44594,65,NIFTY,NIFTY07JUL26C24300,07-JUL-2026,OPTIDX,CE,24300,0.05,
NFO,44595,65,NIFTY,NIFTY07JUL26P24300,07-JUL-2026,OPTIDX,PE,24300,0.05,
NFO,44596,65,NIFTY,NIFTY07JUL26C24350,07-JUL-2026,OPTIDX,CE,24350,0.05,
NFO,58474,65,NIFTY,NIFTY14JUL26C24300,14-JUL-2026,OPTIDX,CE,24300,0.05,
NFO,99999,900,ZYDUSLIFE,ZYDUSLIFE29SEP26C1600,29-SEP-2026,OPTSTK,CE,1600,0.05,
NFO,12345,15,BANKNIFTY,BANKNIFTY30JUL26C50000,30-JUL-2026,OPTIDX,CE,50000,0.05,
"""


def test_filters_to_tracked_index_underlyings_only():
    result = parse_master_csv(FIXTURE_CSV)
    assert set(result.keys()) == {"NIFTY", "BANKNIFTY", "FINNIFTY"}
    # ZYDUSLIFE (OPTSTK, untracked symbol) must not leak in anywhere
    assert "ZYDUSLIFE" not in result


def test_groups_ce_pe_by_strike_and_expiry():
    result = parse_master_csv(FIXTURE_CSV)
    nifty = result["NIFTY"]
    assert nifty["expiries"] == ["2026-07-07", "2026-07-14"]

    strike_24300 = nifty["2026-07-07"]["24300"]
    assert strike_24300["ce_token"] == "44594"
    assert strike_24300["ce_tsym"] == "NIFTY07JUL26C24300"
    assert strike_24300["pe_token"] == "44595"
    assert strike_24300["pe_tsym"] == "NIFTY07JUL26P24300"
    assert strike_24300["lot_size"] == 65


def test_ce_only_strike_has_no_pe_keys():
    result = parse_master_csv(FIXTURE_CSV)
    strike_24350 = result["NIFTY"]["2026-07-07"]["24350"]
    assert "ce_token" in strike_24350
    assert "pe_token" not in strike_24350


def test_expiry_date_converted_to_iso():
    result = parse_master_csv(FIXTURE_CSV)
    assert "2026-07-14" in result["NIFTY"]["expiries"]
    assert "14-JUL-2026" not in result["NIFTY"]["expiries"]


def test_banknifty_isolated_from_nifty():
    result = parse_master_csv(FIXTURE_CSV)
    assert result["BANKNIFTY"]["expiries"] == ["2026-07-30"]
    assert "50000" in result["BANKNIFTY"]["2026-07-30"]
    assert "50000" not in result["NIFTY"].get("2026-07-30", {})


def test_underlying_with_no_rows_has_empty_expiries():
    result = parse_master_csv(FIXTURE_CSV)
    assert result["FINNIFTY"]["expiries"] == []


def test_malformed_row_is_skipped_not_raised():
    bad_csv = FIXTURE_CSV + "NFO,1,65,NIFTY,BADROW,not-a-date,OPTIDX,CE,abc,0.05,\n"
    result = parse_master_csv(bad_csv)  # must not raise
    assert result["NIFTY"]["expiries"] == ["2026-07-07", "2026-07-14"]


# Real Shoonya BFO_symbols.txt uses 'BSXOPT' as the Symbol for Sensex index
# options (confirmed against a live download) - NOT the literal "SENSEX"
# used elsewhere in this codebase - hence the symbol_map remapping.
BFO_FIXTURE_CSV = """Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,
BFO,900001,10,BSXOPT,SENSEX07JUL26C80000,07-JUL-2026,OPTIDX,CE,80000,100,
BFO,900002,10,BSXOPT,SENSEX07JUL26P80000,07-JUL-2026,OPTIDX,PE,80000,100,
BFO,900003,375,BKXOPT,BANKEX07JUL26C58000,07-JUL-2026,OPTIDX,CE,58000,100,
"""


def test_sensex_parsed_from_bfo_master_via_symbol_map():
    result = parse_master_csv(BFO_FIXTURE_CSV, tracked=BFO_UNDERLYINGS, symbol_map=BFO_SYMBOL_TO_UNDERLYING)
    assert result["SENSEX"]["expiries"] == ["2026-07-07"]
    strike = result["SENSEX"]["2026-07-07"]["80000"]
    assert strike["ce_token"] == "900001"
    assert strike["pe_token"] == "900002"
    assert strike["lot_size"] == 10


def test_untracked_bfo_product_is_ignored():
    """BANKEX (BKXOPT) isn't a tracked underlying - it must not leak in even
    though it's a valid OPTIDX row in the same file."""
    result = parse_master_csv(BFO_FIXTURE_CSV, tracked=BFO_UNDERLYINGS, symbol_map=BFO_SYMBOL_TO_UNDERLYING)
    assert "BANKEX" not in result
    assert "BKXOPT" not in result


def test_sensex_absent_without_symbol_map():
    """Without the BFO symbol_map, the raw 'BSXOPT' label doesn't match the
    'SENSEX' tracked name, so nothing should be parsed in."""
    result = parse_master_csv(BFO_FIXTURE_CSV, tracked=BFO_UNDERLYINGS)
    assert result["SENSEX"]["expiries"] == []
