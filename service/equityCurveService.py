from datetime import datetime, timedelta, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from database.equitycurvepersistence.EquityCurvePersistence import EquityCurvePersistence
from database.dashboardpersistence.DashboardPersistence import DashBoardPersistence
from service.walletbalance.WalletBalanceService import WalletBalanceService
from service.positionsService import PositionsService
from utils.assetBuckets import ASSET_TYPE_BUCKETS
from productdto.AssetClassSummaryDTO import AssetClassSummaryDTO

IST = ZoneInfo("Asia/Kolkata")

RANGE_TO_TIMEDELTA = {
    "1D": timedelta(days=1),
    "1W": timedelta(weeks=1),
    "1M": timedelta(days=30),
    "3M": timedelta(days=90),
    "1Y": timedelta(days=365),
    "All": None,
}

# Guards against duplicate snapshot rows if this background task ever runs in
# more than one process (e.g. multiple uvicorn workers) - each capture call
# is a no-op for a bucket that was already captured this recently.
MIN_CAPTURE_INTERVAL = timedelta(seconds=30)


class EquityCurveService:

    def __init__(self):
        self.equityCurvePersistence = EquityCurvePersistence()
        self.dashboardPersistence = DashBoardPersistence()
        self.walletBalanceService = WalletBalanceService()
        self.positionsService = PositionsService()

    def _getBuyingPower(self, userId) -> Decimal:
        wallet = self.walletBalanceService.getWalletBalance(userId)
        return Decimal(str(wallet["balance"])) if wallet else Decimal("0")

    def _todayStartIST(self) -> datetime:
        today = datetime.now(IST).date()
        return datetime.combine(today, time.min, tzinfo=IST)

    def _getEquitySide(self, userId):
        """Market value + unrealized P&L for equity (STOCKS) holdings only."""
        summary = self.dashboardPersistence.getHoldingsSummaryByBucket(userId, ASSET_TYPE_BUCKETS["STOCKS"])
        total_holdings = Decimal(str(summary["total_holdings"])) if summary else Decimal("0")
        unrealized_pnl = Decimal(str(summary["unrealized_pnl"])) if summary else Decimal("0")
        last_updated = summary["last_updated"] if summary else None
        return total_holdings, unrealized_pnl, last_updated

    def _getFnoSide(self, userId):
        """Realized P&L for F&O positions. There is no live F&O contract
        price feed in this platform, so open positions cannot be marked to
        market - unrealized P&L is always 0 for F&O; net_value reflects the
        realized cash impact of F&O activity only."""
        summary = self.positionsService.getPositionsSummaryForUser(userId)
        total_realized_pnl = Decimal(str(summary["total_realized_pnl"])) if summary else Decimal("0")
        last_updated = summary["last_updated"] if summary else None
        return total_realized_pnl, last_updated

    def captureSnapshotsForUser(self, userId):
        """Captures one equity snapshot per bucket (ALL/STOCKS/FNO) for a user.
        Skips a bucket if it was already captured within MIN_CAPTURE_INTERVAL,
        so concurrent/duplicate calls (e.g. multiple app processes) don't
        write duplicate rows."""
        buying_power = self._getBuyingPower(userId)
        equity_value, equity_unrealized, _ = self._getEquitySide(userId)
        fno_realized, _ = self._getFnoSide(userId)

        bucket_values = {
            "STOCKS": (buying_power + equity_value, equity_unrealized),
            "FNO": (buying_power + fno_realized, Decimal("0")),
            "ALL": (buying_power + equity_value + fno_realized, equity_unrealized),
        }

        now_utc = datetime.utcnow()
        for bucket, (net_value, unrealized_pnl) in bucket_values.items():
            latest = self.equityCurvePersistence.getLatestSnapshotTime(userId, bucket)
            if latest is not None and (now_utc - latest["captured_at"]) < MIN_CAPTURE_INTERVAL:
                continue
            self.equityCurvePersistence.insertSnapshot(userId, bucket, net_value, unrealized_pnl, buying_power)

    def getSummaryForBucket(self, userId, bucket):
        if bucket not in ASSET_TYPE_BUCKETS:
            raise ValueError(f"Unknown bucket: {bucket}")

        buying_power = self._getBuyingPower(userId)
        equity_value, equity_unrealized, equity_updated = self._getEquitySide(userId)
        fno_realized, fno_updated = self._getFnoSide(userId)

        if bucket == "STOCKS":
            net_value = buying_power + equity_value
            unrealized_pnl = equity_unrealized
            last_updated = equity_updated
        elif bucket == "FNO":
            net_value = buying_power + fno_realized
            unrealized_pnl = Decimal("0")
            last_updated = fno_updated
        else:  # ALL
            net_value = buying_power + equity_value + fno_realized
            unrealized_pnl = equity_unrealized
            candidates = [t for t in (equity_updated, fno_updated) if t is not None]
            last_updated = max(candidates) if candidates else None

        baseline = self.equityCurvePersistence.getFirstSnapshotSince(userId, bucket, self._todayStartIST())
        todays_pnl = net_value - Decimal(str(baseline["net_value"])) if baseline else Decimal("0")

        return AssetClassSummaryDTO(
            bucket=bucket,
            net_value=net_value,
            todays_pnl=todays_pnl,
            total_unrealized_pnl=unrealized_pnl,
            buying_power=buying_power,
            last_updated=last_updated
        )

    def getEquityCurve(self, userId, bucket, range_key):
        delta = RANGE_TO_TIMEDELTA[range_key]
        since = datetime.min.replace(tzinfo=IST) if delta is None else datetime.now(IST) - delta
        return self.equityCurvePersistence.getSnapshotsSince(userId, bucket, since)
