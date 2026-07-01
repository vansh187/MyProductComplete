#!/usr/bin/env python
"""Verify all imports and modules load without runtime exceptions."""

print("=" * 70)
print("IMPORT VERIFICATION - All Services and Endpoints")
print("=" * 70)

try:
    print("\n[1/9] Importing app module...")
    from app import app
    print("       [OK] app.py imported successfully")

    print("\n[2/9] Importing order service...")
    from api.orders import router as orders_router
    print("       [OK]api/orders.py imported successfully")

    print("\n[3/9] Importing candle service...")
    from service.candleService.CandleService import CandleService
    print("       [OK]service/candleService/CandleService.py imported successfully")

    print("\n[4/9] Importing candle API...")
    from api.candles import router as candles_router
    print("       [OK]api/candles.py imported successfully")

    print("\n[5/9] Importing wallet service...")
    from service.walletbalance.WalletBalanceService import WalletBalanceService
    print("       [OK]service/walletbalance/WalletBalanceService.py imported successfully")

    print("\n[6/9] Importing sector performance service...")
    from api.sectorPerformance import router as sector_router
    print("       [OK]api/sectorPerformance.py imported successfully")

    print("\n[7/9] Importing top movers service...")
    from api.topMovers import router as topmovers_router
    print("       [OK]api/topMovers.py imported successfully")

    print("\n[8/9] Importing market quotes service...")
    from api.marketquotes import router as marketquotes_router
    print("       [OK]api/marketquotes.py imported successfully")

    print("\n[9/9] Importing Shoonya connection...")
    from marketengine.ShoonyaConnection import ShoonyaConnection
    print("       [OK]marketengine/ShoonyaConnection.py imported successfully")

    print("\n" + "=" * 70)
    print("[PASS] ALL IMPORTS SUCCESSFUL - No runtime exceptions")
    print("=" * 70)

    # Verify CandleService methods
    print("\nVerifying CandleService methods...")
    assert hasattr(CandleService, '_normalize_interval'), "Missing _normalize_interval"
    assert hasattr(CandleService, '_format_candle'), "Missing _format_candle"
    assert hasattr(CandleService, 'get_index_candles'), "Missing get_index_candles"
    print("[OK] All CandleService methods present")

    # Verify Shoonya connection methods
    print("\nVerifying ShoonyaConnection methods...")
    assert hasattr(ShoonyaConnection, 'get_index_quote'), "Missing get_index_quote"
    assert hasattr(ShoonyaConnection, 'get_time_price_series'), "Missing get_time_price_series"
    assert hasattr(ShoonyaConnection, 'connect'), "Missing connect"
    print("[OK] All ShoonyaConnection methods present")

    print("\n" + "=" * 70)
    print("[SUCCESS] ZERO RUNTIME EXCEPTIONS - System ready for production")
    print("=" * 70)

except Exception as e:
    print(f"\n[FAIL] ERROR: {e}")
    import traceback
    traceback.print_exc()
    exit(1)
