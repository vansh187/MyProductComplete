#!/usr/bin/env python
"""
Manual/staging verification for the wallet-credit race-condition fix in
RazorPayPersistence.insertUpdateWallet (see git log for
"fix: unlocked read-modify-write race in Razorpay wallet-credit path").

Unlike tests/test_razorpay_wallet_idempotency.py (which mocks psycopg2
entirely and only proves the Python control flow), this script makes real
connections to whatever DATABASE_URL/.env currently points at and proves
the actual Postgres-level locking (pg_advisory_xact_lock + the atomic
`balance = balance + %s` UPDATE) behaves correctly under real concurrent
connections.

wallets.user_id has a foreign key to a real `users` table (Supabase auth),
so this script takes an existing, real user_id as an argument rather than
inventing synthetic ones - it adds real (small, clearly-labeled) test
deposits on top of that user's current balance rather than creating/
deleting user rows.

Run: python scripts/verify_wallet_credit_fix.py <user_id>
"""

import sys
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

from database.PostgresConnectionFactory import PostgresConnectionFactory
from database.razorpaypersistence.RazorPayPersistence import RazorPayPersistence


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


def count_wallet_rows(user_id):
    conn = PostgresConnectionFactory.create_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM wallets WHERE user_id = %s", (user_id,))
        return cursor.fetchone()[0]
    finally:
        cursor.close()
        conn.close()


def deposit(user_id, order_id, amount):
    """Runs the same three-step flow the real webhook path runs:
    pending ledger row -> flip to SUCCESS -> credit wallet."""
    persistence = RazorPayPersistence()
    wallet_ledger = SimpleNamespace(amount=Decimal(str(amount)))
    razorpay_order = {"id": order_id, "currency": "INR"}
    persistence.inssertPendingstatusOfAddFunds(wallet_ledger, razorpay_order, user_id)
    was_newly_processed = persistence.updatePaymentStatus(order_id, f"pay_{order_id}", user_id)
    if was_newly_processed:
        persistence.insertUpdateWallet(user_id, order_id)
    return was_newly_processed


def scenario_concurrent_deposits(user_id, num_threads=20, amount_each=Decimal("100.00")):
    print(f"\n=== Concurrent-deposit test against real user_id={user_id} ===")
    balance_before = get_wallet_balance(user_id)
    rows_before = count_wallet_rows(user_id)
    print(f"  balance BEFORE: {balance_before} (wallet rows: {rows_before})")
    if balance_before is None:
        print("  No existing wallet row for this user - this run will also exercise "
              "the brand-new-wallet advisory-lock INSERT path.")

    errors = []

    def worker(i):
        try:
            deposit(user_id, f"manual_verify_{user_id}_{i}", str(amount_each))
        except Exception as ex:
            errors.append((i, repr(ex)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        if t.is_alive():
            errors.append(("hang", "a thread did not finish within 30s - possible deadlock"))

    balance_after = get_wallet_balance(user_id)
    rows_after = count_wallet_rows(user_id)
    expected_increase = amount_each * num_threads
    expected_balance = (balance_before or Decimal("0")) + expected_increase

    print(f"  balance AFTER:  {balance_after} (wallet rows: {rows_after})")
    print(f"  expected:       {expected_balance} (increase of {expected_increase} from {num_threads} x {amount_each})")
    print(f"  errors: {errors}")

    ok = True
    if errors:
        print("  FAIL: one or more concurrent deposits raised an error or hung")
        ok = False
    if rows_after != 1:
        print(f"  FAIL: expected exactly 1 wallet row after the run, found {rows_after} (duplicate-row race)")
        ok = False
    if balance_after != expected_balance:
        print(f"  FAIL: balance mismatch - expected {expected_balance}, got {balance_after} (lost update)")
        ok = False

    print("  PASS - no lost updates, no duplicate wallet rows, all 20 concurrent credits landed" if ok else "  ONE OR MORE CHECKS FAILED - see above")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/verify_wallet_credit_fix.py <existing_user_id>")
        sys.exit(1)
    user_id = int(sys.argv[1])
    print(f"Verifying against DATABASE_URL from .env, real user_id={user_id}.")
    success = scenario_concurrent_deposits(user_id)
    sys.exit(0 if success else 1)
