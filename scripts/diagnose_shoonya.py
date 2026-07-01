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

def diagnose():
    print("=" * 70)
    print("SHOONYA DIAGNOSTIC")
    print("=" * 70)

    # Check env vars
    print("\n1. Environment Variables:")
    print(f"   SHOONYA_USER_ID:        {os.getenv('SHOONYA_USER_ID') or '(missing)'}")
    print(f"   SHOONYA_PASSWORD:       {'***' if os.getenv('SHOONYA_PASSWORD') else '(missing)'}")
    print(f"   SHOONYA_VENDOR_CODE:    {os.getenv('SHOONYA_VENDOR_CODE') or '(missing)'}")
    print(f"   SHOONYA_API_SECRET:     {'***' if os.getenv('SHOONYA_API_SECRET') else '(missing)'}")
    print(f"   SHOONYA_IMEI:           {os.getenv('SHOONYA_IMEI') or '(missing)'}")
    print(f"   SHOONYA_TOTP_SECRET:    {'***' if os.getenv('SHOONYA_TOTP_SECRET') else '(missing)'}")
    session_token = os.getenv('SHOONYA_SESSION_TOKEN')
    access_token = os.getenv('SHOONYA_ACCESS_TOKEN')
    print(f"   SHOONYA_SESSION_TOKEN:  {session_token[:20] + '...' if session_token else '(missing)'}")
    print(f"   SHOONYA_ACCESS_TOKEN:   {access_token[:20] + '...' if access_token else '(missing)'}")

    # Try connection
    print("\n2. Attempting Connection:")
    try:
        shoonya = ShoonyaConnection()
        print(f"   Created ShoonyaConnection instance")

        if shoonya.connect():
            print(f"   [OK] Successfully connected")

            # Test each index token
            print("\n3. Testing Index Tokens:")
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
                        print(f"   [OK] {name:20} ({exchange}:{token:5}) ltp={quote['ltp']}")
                    else:
                        print(f"   [FAIL] {name:20} ({exchange}:{token:5}) returned None")
                except Exception as e:
                    print(f"   [FAIL] {name:20} ({exchange}:{token:5}) error: {e}")

        else:
            print(f"   [FAIL] Connection failed")
            print(f"   Check: .env has SHOONYA_SESSION_TOKEN and SHOONYA_ACCESS_TOKEN")
            print(f"   If tokens are stale, run: GET /admin/shoonya/auth-url and follow OAuth flow")

    except Exception as e:
        print(f"   [FAIL] Exception: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 70)

if __name__ == "__main__":
    diagnose()
