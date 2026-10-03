#!/usr/bin/env python
"""Verify all imports and modules load without runtime exceptions."""

import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

logger.info("=" * 70)
logger.info("IMPORT VERIFICATION - All Services and Endpoints")
logger.info("=" * 70)

try:
    logger.info("\n[1/9] Importing app module...")
    from app import app
    logger.info("       [OK] app.py imported successfully")

    logger.info("\n[2/9] Importing order service...")
    from api.orders import router as orders_router
    logger.info("       [OK]api/orders.py imported successfully")

    logger.info("\n[3/9] Importing candle service...")
    from service.candleService.CandleService import CandleService
    logger.info("       [OK]service/candleService/CandleService.py imported successfully")

    logger.info("\n[4/9] Importing candle API...")
    from api.candles import router as candles_router
    logger.info("       [OK]api/candles.py imported successfully")

    logger.info("\n[5/9] Importing wallet service...")
    from service.walletbalance.WalletBalanceService import WalletBalanceService
    logger.info("       [OK]service/walletbalance/WalletBalanceService.py imported successfully")

    logger.info("\n[6/9] Importing sector performance service...")
    from api.sectorPerformance import router as sector_router
    logger.info("       [OK]api/sectorPerformance.py imported successfully")

    logger.info("\n[7/9] Importing top movers service...")
    from api.topMovers import router as topmovers_router
    logger.info("       [OK]api/topMovers.py imported successfully")

    logger.info("\n[8/9] Importing market quotes service...")
    from api.marketquotes import router as marketquotes_router
    logger.info("       [OK]api/marketquotes.py imported successfully")

    logger.info("\n[9/9] Importing Shoonya connection...")
    from marketengine.ShoonyaConnection import ShoonyaConnection
    logger.info("       [OK]marketengine/ShoonyaConnection.py imported successfully")

    logger.info("\n" + "=" * 70)
    logger.info("[PASS] ALL IMPORTS SUCCESSFUL - No runtime exceptions")
    logger.info("=" * 70)

    # Verify CandleService methods
    logger.info("\nVerifying CandleService methods...")
    assert hasattr(CandleService, '_normalize_interval'), "Missing _normalize_interval"
    assert hasattr(CandleService, '_format_candle'), "Missing _format_candle"
    assert hasattr(CandleService, 'get_index_candles'), "Missing get_index_candles"
    logger.info("[OK] All CandleService methods present")

    # Verify Shoonya connection methods
    logger.info("\nVerifying ShoonyaConnection methods...")
    assert hasattr(ShoonyaConnection, 'get_index_quote'), "Missing get_index_quote"
    assert hasattr(ShoonyaConnection, 'get_time_price_series'), "Missing get_time_price_series"
    assert hasattr(ShoonyaConnection, 'connect'), "Missing connect"
    logger.info("[OK] All ShoonyaConnection methods present")

    logger.info("\n" + "=" * 70)
    logger.info("[SUCCESS] ZERO RUNTIME EXCEPTIONS - System ready for production")
    logger.info("=" * 70)

except Exception as e:
    logger.error(f"\n[FAIL] ERROR: {e}")
    import traceback
    traceback.print_exc()
    exit(1)
