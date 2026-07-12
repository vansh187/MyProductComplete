#!/usr/bin/env python
"""
Manual/staging verification for the wallet check-then-debit race fix in
WalletBalancePersistence.debitWalletIfSufficient (used by api/orders.py for
BUY order placement).

Fires many concurrent debit attempts (real separate DB connections, not
mocked) against a single real wallet, deliberately requesting more total
than the wallet holds - proves (a) the total debited never exceeds the
starting balance (no over-debit / race), (b) the balance never goes
negative, and (c) exactly the right number of attempts succeed vs. fail.

Run: python scripts/verify_wallet_debit_fix.py <existing_user_id>
"""

import sys
import threading
from decimal import Decimal
from pathlib import Path

repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

from database.PostgresConnectionFactory import PostgresConnectionFactory
from database.walletbalancepersistence.WalletBalancePersistence import WalletBalancePersistence


def get_wallet_balance(user_id):
    conn = PostgresConnectionFactory.create_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        return Decimal(str(row[0])) if row else None
    finally:
        cursor.close()
        conn.close()


def scenario_concurrent_debits(user_id, num_threads=50, amount_each=Decimal("100.00")):
    print(f"\n=== Concurrent-debit test against real user_id={user_id} ===")
    balance_before = get_wallet_balance(user_id)
    if balance_before is None:
        print("  No wallet row for this user - aborting.")
        return False

    total_requested = amount_each * num_threads
    expected_successes = int(balance_before // amount_each)
    expected_balance = balance_before - (expected_successes * amount_each)

    print(f"  balance BEFORE: {balance_before}")
    print(f"  requesting {num_threads} x {amount_each} = {total_requested} total (deliberately more than the balance)")
    print(f"  expected: exactly {expected_successes} debits succeed, final balance = {expected_balance}")

    results = []
    results_lock = threading.Lock()
    errors = []

    def worker():
        try:
            persistence = WalletBalancePersistence()
            ok = persistence.debitWalletIfSufficient(user_id, amount_each)
            with results_lock:
                results.append(ok)
        except Exception as ex:
            errors.append(repr(ex))

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        if t.is_alive():
            errors.append("a thread did not finish within 30s - possible deadlock")

    balance_after = get_wallet_balance(user_id)
    successful = sum(1 for r in results if r)
    failed = sum(1 for r in results if not r)

    print(f"  balance AFTER:  {balance_after}")
    print(f"  successful debits: {successful}, failed (insufficient funds): {failed}, errors: {errors}")

    ok = True
    if errors:
        print("  FAIL: unexpected errors/hangs during concurrent debits")
        ok = False
    if balance_after < 0:
        print(f"  FAIL: balance went negative ({balance_after}) - over-debit race")
        ok = False
    if balance_after != expected_balance:
        print(f"  FAIL: balance mismatch - expected {expected_balance}, got {balance_after}")
        ok = False
    if successful != expected_successes:
        print(f"  FAIL: expected exactly {expected_successes} successful debits, got {successful}")
        ok = False

    print("  PASS - no over-debit, no lost/duplicated debits, balance never negative" if ok else "  ONE OR MORE CHECKS FAILED")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/verify_wallet_debit_fix.py <existing_user_id>")
        sys.exit(1)
    user_id = int(sys.argv[1])
    print(f"Verifying against DATABASE_URL from .env, real user_id={user_id}.")
    success = scenario_concurrent_debits(user_id)
    sys.exit(0 if success else 1)
