#!/usr/bin/env python
"""
Diagnostic script: Check Shoonya connection and token validity.

Run: python scripts/diagnose_shoonya.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Add repo root to path
repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

load_dotenv()

from marketengine.ShoonyaConnection import ShoonyaConnection
import logging

logger = logging.getLogger(__name__)

def diagnose():
    logger.info("=" * 70)
    logger.info("SHOONYA DIAGNOSTIC")
    logger.info("=" * 70)

    # Check env vars
    logger.info("\n1. Environment Variables:")
    logger.info(f"   SHOONYA_USER_ID:        {os.getenv('SHOONYA_USER_ID') or '(missing)'}")
    logger.info(f"   SHOONYA_PASSWORD:       {'***' if os.getenv('SHOONYA_PASSWORD') else '(missing)'}")
    logger.info(f"   SHOONYA_VENDOR_CODE:    {os.getenv('SHOONYA_VENDOR_CODE') or '(missing)'}")
    logger.info(f"   SHOONYA_API_SECRET:     {'***' if os.getenv('SHOONYA_API_SECRET') else '(missing)'}")
    logger.info(f"   SHOONYA_IMEI:           {os.getenv('SHOONYA_IMEI') or '(missing)'}")
    logger.info(f"   SHOONYA_TOTP_SECRET:    {'***' if os.getenv('SHOONYA_TOTP_SECRET') else '(missing)'}")
    session_token = os.getenv('SHOONYA_SESSION_TOKEN')
    access_token = os.getenv('SHOONYA_ACCESS_TOKEN')
    logger.info(f"   SHOONYA_SESSION_TOKEN:  {session_token[:20] + '...' if session_token else '(missing)'}")
    logger.info(f"   SHOONYA_ACCESS_TOKEN:   {access_token[:20] + '...' if access_token else '(missing)'}")

    # Try connection
    logger.info("\n2. Attempting Connection:")
    try:
        shoonya = ShoonyaConnection()
        logger.info(f"   Created ShoonyaConnection instance")

        if shoonya.connect():
            logger.info(f"   [OK] Successfully connected")

            # Test each index token
            logger.info("\n3. Testing Index Tokens:")
            indices = [
                ("Nifty 50", "NSE", "26000"),
                ("Sensex", "BSE", "1"),
                ("Bank Nifty", "NSE", "26009"),
                ("India VIX", "NSE", "26017"),
                ("Fin Nifty", "NSE", "26037"),
                ("Midcap Nifty", "NSE", "26074"),
            ]

            for name, exchange, token in indices:
                try:
                    quote = shoonya.get_index_quote(exchange, token)
                    if quote:
                        logger.info(f"   [OK] {name:20} ({exchange}:{token:5}) ltp={quote['ltp']}")
                    else:
                        logger.error(f"   [FAIL] {name:20} ({exchange}:{token:5}) returned None")
                except Exception as e:
                    logger.error(f"   [FAIL] {name:20} ({exchange}:{token:5}) error: {e}")

        else:
            logger.error(f"   [FAIL] Connection failed")
            logger.info(f"   Check: .env has SHOONYA_SESSION_TOKEN and SHOONYA_ACCESS_TOKEN")
            logger.info(f"   If tokens are stale, run: GET /admin/shoonya/auth-url and follow OAuth flow")

    except Exception as e:
        logger.error(f"   [FAIL] Exception: {e}")
        import traceback
        traceback.print_exc()

    logger.info("\n" + "=" * 70)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    diagnose()
