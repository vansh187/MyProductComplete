"""
SPAN + exposure margin for a basket of F&O legs, computed by the broker's
own SpanCalc (NorenApi.span_calculator) on the master account - so hedged
baskets get the same margin benefit the exchange actually grants, which the
in-house per-leg margin engine (service/marginengine) does not model.

Read-only: nothing is placed, blocked or persisted. Legs are resolved
against the scrip masters (OptionMaster/FutureMaster) and netted per
contract before the single broker call.

Deliberately instance-based (no static/class methods or class-level state).
"""

import asyncio
import logging
from datetime import datetime

from appconfig import FutureMaster, OptionMaster
from scripts.build_future_master import BFO_SYMBOL_TO_UNDERLYING as FUTURE_BFO_SYMBOLS
from scripts.build_option_master import BFO_SYMBOL_TO_UNDERLYING as OPTION_BFO_SYMBOLS
from appconfig.ScripMasterRefresher import today_ist
from utils.safe_numbers import safe_float, safe_int

logger = logging.getLogger(__name__)

SPAN_CALL_TIMEOUT_SECS = 5.0

# Our ProductType -> Shoonya product code. CNC is cash-equity only.
_PRODUCT_CODES = {"NRML": "M", "MIS": "I"}

# How Shoonya's scrip master describes a futures row's option fields.
FUTURES_OPTION_TYPE = "XX"
FUTURES_STRIKE = "-1"


class SpanMarginService:

    def __init__(self, timeout_secs: float = SPAN_CALL_TIMEOUT_SECS):
        self._timeout_secs = timeout_secs
        # SpanCalc matches on the scrip master's own Symbol column. NFO uses
        # the index name verbatim (NIFTY), but BFO uses product codes
        # (BSXOPT/SX50FUT for Sensex) - the build scripts' maps, inverted.
        self._option_symnames = self._inverted(OPTION_BFO_SYMBOLS)
        self._future_symnames = self._inverted(FUTURE_BFO_SYMBOLS)

    def _inverted(self, broker_to_underlying: dict) -> dict:
        return {underlying: broker_symbol for broker_symbol, underlying in broker_to_underlying.items()}

    def _broker_expiry(self, iso_expiry: str) -> str:
        """'2026-10-06' -> '06-OCT-2026' (the format SpanCalc expects)."""
        return datetime.strptime(iso_expiry, "%Y-%m-%d").strftime("%d-%b-%Y").upper()

    def _resolve_contract(self, tsym: str, product_code: str) -> tuple[tuple, int | None, str] | None:
        """(SpanCalc contract key, lot_size, ISO expiry) for a tradingsymbol,
        or None if neither scrip master knows it. The key is
        (prd, exch, instname, symname, exd, optt, strprc)."""
        option = OptionMaster.find_by_tsym(tsym)
        if option is not None:
            key = (
                product_code,
                option["exchange"],
                "OPTIDX",
                self._option_symnames.get(option["underlying"], option["underlying"]),
                self._broker_expiry(option["expiry"]),
                option["option_type"],
                f"{float(option['strike']):.2f}",
            )
            return key, option.get("lot_size"), option["expiry"]

        future = FutureMaster.find_by_tsym(tsym)
        if future is not None:
            key = (
                product_code,
                future["exchange"],
                "FUTIDX",
                self._future_symnames.get(future["underlying"], future["underlying"]),
                self._broker_expiry(future["expiry"]),
                FUTURES_OPTION_TYPE,
                FUTURES_STRIKE,
            )
            return key, future.get("lot_size"), future["expiry"]

        return None

    def _validate_leg(self, leg: dict, today_iso: str) -> tuple[tuple | None, str, int, str | None]:
        """Validates one leg -> (contract key, side, quantity, None), or
        (None, "", 0, reason) for the first problem found."""
        tsym = str(leg.get("tsym") or "").upper().strip()
        side = str(leg.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            return None, "", 0, "invalid_side"
        product_code = _PRODUCT_CODES.get(str(leg.get("product_type") or "NRML").upper())
        if product_code is None:
            return None, "", 0, "unsupported_product_type"
        quantity = safe_int(leg.get("quantity"), 0)
        if quantity <= 0:
            return None, "", 0, "invalid_quantity"

        contract = self._resolve_contract(tsym, product_code)
        if contract is None:
            return None, "", 0, "unknown_contract"
        key, lot_size, expiry = contract
        if expiry < today_iso:
            return None, "", 0, "contract_expired"
        if lot_size and quantity % lot_size != 0:
            return None, "", 0, "quantity_not_lot_multiple"
        return key, side, quantity, None

    def build_positions(self, legs: list[dict]) -> tuple[list[dict], list[dict]]:
        """Nets legs per contract into SpanCalc's position rows. Returns
        (positions, errors); every invalid leg is reported in errors with its
        index (calculate() then refuses the whole basket)."""
        totals: dict[tuple, list[int]] = {}
        errors: list[dict] = []
        today_iso = today_ist().isoformat()
        for index, leg in enumerate(legs):
            try:
                key, side, quantity, reason = self._validate_leg(leg, today_iso)
            except Exception as exc:
                logger.warning(f"[SpanMargin] leg {index} could not be validated: {exc}")
                key, side, quantity, reason = None, "", 0, "invalid_leg"
            if key is None:
                errors.append({"index": index, "tsym": leg.get("tsym"), "reason": reason})
                continue
            buy_sell = totals.setdefault(key, [0, 0])
            buy_sell[1 if side == "SELL" else 0] += quantity

        positions = [
            {
                "prd": prd, "exch": exch, "instname": instname, "symname": symname,
                "exd": exd, "optt": optt, "strprc": strprc,
                "buyqty": str(buy), "sellqty": str(sell), "netqty": str(buy - sell),
            }
            for (prd, exch, instname, symname, exd, optt, strprc), (buy, sell) in totals.items()
        ]
        return positions, errors

    async def calculate(self, shoonya, legs: list[dict]) -> tuple[dict | None, list[dict]]:
        """Returns ({span, exposure, total_margin, positions, broker}, errors)
        or (None, errors). Never raises."""
        try:
            positions, errors = self.build_positions(legs)
            # All-or-nothing: pricing the rest of a basket without one leg
            # (e.g. a mistyped hedge) would report naked margin as if it
            # were the hedged basket's.
            if errors:
                return None, errors
            if not positions:
                return None, [{"reason": "no_valid_legs"}]

            if shoonya is None or not shoonya.is_connected:
                return None, errors + [{"reason": "shoonya_disconnected"}]

            loop = asyncio.get_running_loop()
            reply = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: shoonya.get_span_margin(positions)),
                timeout=self._timeout_secs,
            )
            if reply is None:
                return None, errors + [{"reason": "broker_unavailable"}]
            if reply.get("stat") != "Ok":
                return None, errors + [{"reason": "broker_rejected", "detail": reply.get("emsg")}]

            span = safe_float(reply.get("span"))
            exposure = safe_float(reply.get("expo"))
            if span is None or exposure is None:
                # An Ok reply without usable numbers must not be shown as a
                # zero margin requirement.
                logger.error(f"[SpanMargin] Ok reply without span/expo: {reply}")
                return None, [{"reason": "broker_bad_reply"}]
            return {
                "span": span,
                "exposure": exposure,
                "total_margin": round(span + exposure, 2),
                "positions": positions,
                "broker": reply,
            }, errors
        except asyncio.TimeoutError:
            return None, [{"reason": "broker_timeout"}]
        except Exception as exc:
            logger.error(f"[SpanMargin] calculation failed: {exc}", exc_info=True)
            return None, [{"reason": "span_calculation_failed"}]
