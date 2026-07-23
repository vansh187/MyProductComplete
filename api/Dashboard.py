import asyncio
from dataclasses import asdict
from fastapi import APIRouter
from utils.auth_dependency import get_current_user
from fastapi import APIRouter, Depends, Query
#from service.dashboard.dashboardservice.DashboardService import DashboardService as dashboardService

from service.dashboard.dashboardservice.DashboardService import DashboardService
from service.equityCurveService import EquityCurveService
from database.portfolioPersistence import portfolioPersistence
from database.positionspersistence.PositionsPersistence import PositionsPersistence
from utils.assetBuckets import ASSET_TYPE_BUCKETS

router=APIRouter()

VALID_RANGES = ("1D", "1W", "1M", "3M", "1Y", "All")

@router.get("/getDashboardSummary")
def getDashboardSummary(currentUser=Depends(get_current_user)):
    try:
        userId=currentUser["user_id"]
        dashboardService=DashboardService()
        userDashboard=dashboardService.getDashBoarddetailForUser(userId)
        return{
            "success": True,
            "userId": userId,
            "dashboard":userDashboard
        }
    except (AttributeError, KeyError, TypeError) as e:
        return {"success": False, "message": f"Error building dashboard summary: {str(e)}"}
    except Exception:
        return {"success": False, "message": "An unexpected error occurred while fetching the dashboard summary"}


@router.get("/getAssetClassSummary")
def getAssetClassSummary(
    bucket: str = Query("ALL", description="ALL, STOCKS, or FNO"),
    currentUser=Depends(get_current_user)
):
    try:
        userId = currentUser["user_id"]
        if bucket not in ASSET_TYPE_BUCKETS:
            return {"success": False, "message": f"Invalid bucket: {bucket}"}

        summary = EquityCurveService().getSummaryForBucket(userId, bucket)
        return {"success": True, "userId": userId, "summary": asdict(summary)}
    except (AttributeError, KeyError, TypeError) as e:
        return {"success": False, "message": f"Error building asset class summary: {str(e)}"}
    except Exception:
        return {"success": False, "message": "An unexpected error occurred while fetching the asset class summary"}


@router.get("/getPortfolioEquityCurve")
def getPortfolioEquityCurve(
    bucket: str = Query("ALL", description="ALL, STOCKS, or FNO"),
    range: str = Query("1M", description="1D, 1W, 1M, 3M, 1Y, or All"),
    currentUser=Depends(get_current_user)
):
    try:
        userId = currentUser["user_id"]
        if bucket not in ASSET_TYPE_BUCKETS:
            return {"success": False, "message": f"Invalid bucket: {bucket}"}
        if range not in VALID_RANGES:
            return {"success": False, "message": f"Invalid range: {range}"}

        points = EquityCurveService().getEquityCurve(userId, bucket, range)
        return {"success": True, "userId": userId, "bucket": bucket, "range": range, "points": points}
    except (AttributeError, KeyError, TypeError) as e:
        return {"success": False, "message": f"Error building equity curve: {str(e)}"}
    except Exception:
        return {"success": False, "message": "An unexpected error occurred while fetching the equity curve"}


async def start_equity_curve_capture():
    """
    Background task: captures an ALL/STOCKS/FNO equity snapshot for every
    user with open holdings, every 60s while the market is open, every
    300s while closed. Powers the per-tab performance graphs.
    """
    from api.marketquotes import _is_market_open

    await asyncio.sleep(20)

    portfolio_persistence = portfolioPersistence()
    positions_persistence = PositionsPersistence()
    equity_curve_service = EquityCurveService()

    while True:
        try:
            # Union of equity-holding users and F&O-only users - a user with
            # no rows in `holdings` (only open F&O positions) would otherwise
            # never get an equity-curve snapshot captured.
            holdings_users = {row["user_id"] for row in portfolio_persistence.getDistinctUsersWithHoldings()}
            position_users = {row["user_id"] for row in positions_persistence.getDistinctUsersWithPositions()}
            for user_id in holdings_users | position_users:
                try:
                    equity_curve_service.captureSnapshotsForUser(user_id)
                except Exception as exc:
                    print(f"[EquityCurve] Snapshot failed for user {user_id}: {exc}")
        except Exception as exc:
            print(f"[EquityCurve] Refresh error: {exc}")

        await asyncio.sleep(60 if _is_market_open() else 300)

