"""Demo: write a single test transaction using YNAB_SANDBOX_MODE=1.

Run with: YNAB_SANDBOX_MODE=1 uv run python scripts/demo_sandbox_write.py
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ynab_client import YNABClient


def main():
    client = YNABClient()

    if not client.sandbox_mode:
        print("ERROR: YNAB_SANDBOX_MODE=1 is not set. Refusing to run against real budget.")
        sys.exit(1)

    budget_name = os.environ.get("YNAB_DEFAULT_BUDGET")
    if not budget_name:
        print("ERROR: YNAB_DEFAULT_BUDGET not set in .env")
        sys.exit(1)

    print(f"Resolving budget: {budget_name!r}")
    budget_id = client.resolve_budget_id(budget_name)
    print(f"  → budget_id: {budget_id}")

    txn = {
        "date": "2026-05-08",
        "amount": -12340,
        "payee_name": "Demo Coffee Shop",
        "memo": "claude demo write test",
        "cleared": "uncleared",
        "approved": False,
        "account_id": "will-be-overridden-by-sandbox",
    }

    print("\nTransaction to write (account_id will be redirected to Sandbox):")
    for k, v in txn.items():
        print(f"  {k}: {v}")

    confirm = input("\nWrite this transaction? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    result = client.create_transactions(budget_id, [txn])
    created = result.get("transactions", [result.get("transaction")])
    print(f"\nSuccess! Created {len(created)} transaction(s).")
    for t in created:
        if t:
            print(f"  id={t['id']}  date={t['date']}  amount={t['amount']}  payee={t.get('payee_name')}")


if __name__ == "__main__":
    main()
