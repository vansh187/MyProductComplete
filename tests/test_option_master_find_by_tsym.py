"""
Regression tests for OptionMaster.find_by_tsym() accepting both
tradingsymbol conventions.

Root cause this guards against: master_options.json (and Shoonya's real
NFO/BFO scrip master) stores tradingsymbols in the native
'{prefix}{C|P}{strike}' form (e.g. 'NIFTY14JUL26C23950'), but
api/models.py's own OrderCreate.symbol field description - and,
apparently, whatever produced the orders/positions a user reported with
null token/underlying/expiry/option_type - used the
'{prefix}{strike}{CE|PE}' suffix convention (e.g.
'NIFTY14JUL2623950CE'). find_by_tsym() previously only matched the native
form, so any caller using the suffix convention got None back silently:

- service/orderService.py never resolved token/exchange at order creation
- service/positionService.py never resolved underlying/expiry/strike/
  option_type when building the position row
- service/marginengine/margin_engine.py's resolve_contract_type() couldn't
  even recognize the order as an OPTION, meaning margin could go
  unblocked entirely for an option order using this convention

These tests run against the REAL master_options.json (not mocked) so they
actually exercise the string-matching logic that was broken - the
existing tests in test_orders_and_execution.py/test_margin_engine.py all
mock find_by_tsym() directly and would not have caught this.
"""

from appconfig import OptionMaster


def _find_any_real_contract():
    """Picks one real (underlying, expiry, strike) triple with both a CE
    and PE leg from the actual loaded master_options.json, skipping the
    test entirely (rather than failing) if the master file is empty in
    whatever environment runs this - see OptionMaster._load()'s
    missing-file fallback."""
    for underlying, chains in OptionMaster._raw.items():
        for expiry, strikes in chains.items():
            if expiry == "expiries":
                continue
            for strike, info in strikes.items():
                if info.get("ce_tsym") and info.get("pe_tsym"):
                    return underlying, expiry, strike, info
    return None


class TestFindByTsymAcceptsBothConventions:

    def test_native_shoonya_convention_resolves(self):
        contract = _find_any_real_contract()
        if contract is None:
            import pytest
            pytest.skip("master_options.json has no CE/PE contract available in this environment")
        underlying, expiry, strike, info = contract

        result = OptionMaster.find_by_tsym(info["ce_tsym"])

        assert result is not None
        assert result["underlying"] == underlying
        assert result["expiry"] == expiry
        assert result["strike"] == float(strike)
        assert result["option_type"] == "CE"
        assert result["token"] == info["ce_token"]

    def test_ce_pe_suffix_convention_resolves_identically(self):
        """The convention documented in api/models.py's OrderCreate.symbol
        field ('NIFTY07JUL2623800CE' - strike then CE/PE) must resolve to
        the exact same contract as the native form."""
        contract = _find_any_real_contract()
        if contract is None:
            import pytest
            pytest.skip("master_options.json has no CE/PE contract available in this environment")
        underlying, expiry, strike, info = contract

        native_ce = info["ce_tsym"]
        # native_ce == "{prefix}C{strike}" -> build the suffix-convention
        # equivalent "{prefix}{strike}CE"
        marker = "C" + strike
        assert native_ce.upper().endswith(marker), f"unexpected tsym shape: {native_ce}"
        prefix = native_ce[: -len(marker)]
        suffix_convention_ce = f"{prefix}{strike}CE"

        result_native = OptionMaster.find_by_tsym(native_ce)
        result_suffix = OptionMaster.find_by_tsym(suffix_convention_ce)

        assert result_suffix is not None, (
            f"find_by_tsym() failed to resolve the CE/PE-suffix convention "
            f"'{suffix_convention_ce}' - this is the exact bug that left "
            f"token/underlying/expiry/option_type as null in production positions"
        )
        assert result_suffix == result_native

    def test_pe_suffix_convention_resolves_identically(self):
        contract = _find_any_real_contract()
        if contract is None:
            import pytest
            pytest.skip("master_options.json has no CE/PE contract available in this environment")
        underlying, expiry, strike, info = contract

        native_pe = info["pe_tsym"]
        marker = "P" + strike
        assert native_pe.upper().endswith(marker), f"unexpected tsym shape: {native_pe}"
        prefix = native_pe[: -len(marker)]
        suffix_convention_pe = f"{prefix}{strike}PE"

        result_native = OptionMaster.find_by_tsym(native_pe)
        result_suffix = OptionMaster.find_by_tsym(suffix_convention_pe)

        assert result_suffix is not None
        assert result_suffix == result_native
        assert result_suffix["option_type"] == "PE"

    def test_exact_reported_symbol_resolves(self):
        """The literal symbol from the bug report: NIFTY 2026-07-14
        23950 CE, sent in suffix-convention form."""
        result = OptionMaster.find_by_tsym("NIFTY14JUL2623950CE")
        if result is None and OptionMaster.get_strike_chain("NIFTY", "2026-07-14") == {}:
            import pytest
            pytest.skip("2026-07-14 NIFTY chain not present in this environment's master_options.json")

        assert result is not None
        assert result["underlying"] == "NIFTY"
        assert result["expiry"] == "2026-07-14"
        assert result["strike"] == 23950.0
        assert result["option_type"] == "CE"

    def test_unrecognized_symbol_still_returns_none(self):
        """The fix must not turn find_by_tsym into a fuzzy matcher that
        accepts arbitrary garbage - only the two known conventions."""
        assert OptionMaster.find_by_tsym("NOT_A_REAL_SYMBOL_AT_ALL") is None
        assert OptionMaster.find_by_tsym("") is None
        assert OptionMaster.find_by_tsym(None) is None

    def test_case_and_whitespace_insensitive_for_suffix_convention_too(self):
        contract = _find_any_real_contract()
        if contract is None:
            import pytest
            pytest.skip("master_options.json has no CE/PE contract available in this environment")
        underlying, expiry, strike, info = contract

        native_ce = info["ce_tsym"]
        marker = "C" + strike
        prefix = native_ce[: -len(marker)]
        suffix_convention_ce = f"{prefix}{strike}CE"

        result = OptionMaster.find_by_tsym(f"  {suffix_convention_ce.lower()}  ")
        assert result is not None
        assert result["option_type"] == "CE"
