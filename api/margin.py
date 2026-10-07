"""
Broker-computed SPAN + exposure margin for a basket of F&O legs - see
service/spanMargin/SpanMarginService.py.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.models import OrderSide, ProductType
from service.spanMargin.SpanMarginService import SpanMarginService
from utils.auth_dependency import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["Margin"])

_spanMarginService = SpanMarginService()

MAX_LEGS = 50

# Reasons that mean the request itself was unusable (400) or the broker
# session is down (503); anything else is an upstream failure (502).
_CLIENT_ERROR_REASONS = {
    "no_valid_legs", "unknown_contract", "invalid_quantity", "unsupported_product_type", "invalid_leg",
    "invalid_side", "contract_expired", "quantity_not_lot_multiple",
}
_UNAVAILABLE_REASONS = {"shoonya_disconnected", "broker_unavailable", "broker_timeout"}


class MarginLeg(BaseModel):
    tsym: str = Field(..., min_length=1, max_length=50, description="Trading symbol, e.g. NIFTY06OCT26C25000")
    side: OrderSide = Field(..., description="BUY or SELL")
    quantity: int = Field(..., gt=0, description="Quantity in units (lots x lot size)")
    product_type: ProductType = Field(default=ProductType.NRML, description="NRML or MIS")


class MarginRequest(BaseModel):
    legs: list[MarginLeg] = Field(..., min_length=1, max_length=MAX_LEGS)


@router.post("/margin")
async def calculate_margin(body: MarginRequest, request: Request, current_user=Depends(get_current_user)):
    """
    SPAN + exposure margin for a list of legs, from the broker's SpanCalc.
    Legs on the same contract are netted, so hedged baskets get the
    exchange's margin benefit. All-or-nothing: if any leg can't be resolved
    the request fails with 400 and per-leg errors, never a partial margin.

    Request:
    {"legs": [{"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 75, "product_type": "NRML"},
              {"tsym": "NIFTY06OCT26C25200", "side": "BUY",  "quantity": 75, "product_type": "NRML"}]}

    Response:
    {"success": true, "span": 41250.0, "exposure": 9870.5, "total_margin": 51120.5,
     "positions": [...as sent to the broker...], "errors": []}
    """
    try:
        legs = [
            {
                "tsym": leg.tsym,
                "side": leg.side.value,
                "quantity": leg.quantity,
                "product_type": leg.product_type.value,
            }
            for leg in body.legs
        ]
        shoonya = getattr(request.app.state, "shoonya", None)
        result, errors = await _spanMarginService.calculate(shoonya, legs)
    except Exception as exc:
        logger.error(f"[Margin] SPAN request failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to calculate margin")

    if result is None:
        reasons = {error.get("reason") for error in errors}
        if reasons & _UNAVAILABLE_REASONS:
            status_code = 503
        elif reasons and reasons <= _CLIENT_ERROR_REASONS:
            status_code = 400
        else:
            status_code = 502
        raise HTTPException(status_code=status_code, detail={"message": "Margin could not be calculated", "errors": errors})

    return {
        "success": True,
        "span": result["span"],
        "exposure": result["exposure"],
        "total_margin": result["total_margin"],
        "positions": result["positions"],
        "errors": errors,
    }
