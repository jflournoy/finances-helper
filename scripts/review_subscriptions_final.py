#!/usr/bin/env python3
"""Interactive subscription reviewer - simplified and robust.

Uses category-first matching, then smarter payee name matching.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

from ynab_client import YNABClient
import os
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


def get_all_transactions(days_back=1825):
    """Fetch all transactions from the past N days."""
    client = YNABClient()
    budget_name = os.environ.get('YNAB_DEFAULT_BUDGET', 'My Budget')
    budget_id = client.resolve_budget_id(budget_name)
    budget = client.get_budget(budget_id)

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days_back)

    txns, _ = client.get_transactions(budget['id'], start_date.isoformat())
    return txns


def find_transactions_for_payee(all_txns, payee_pattern):
    """Find transactions for a payee using smart matching.

    Strategy:
    1. Try exact payee name match
    2. Try category match (e.g., "Flickr (addup)" for Flickr payee)
    3. Try longest words from payee pattern (>4 chars for specificity)
    """
    payee_lower = payee_pattern.lower()

    matching = []

    # Strategy 1: Exact payee match
    for t in all_txns:
        if payee_lower == (t.get('payee_name') or '').lower():
            matching.append(t)

    if matching:
        return sorted(matching, key=lambda x: x['date'])

    # Strategy 2: Category match (if payee name appears in category)
    for t in all_txns:
        cat = (t.get('category_name') or '').lower()
        if payee_lower in cat or any(w in cat for w in payee_lower.split() if len(w) > 4):
            matching.append(t)

    if matching:
        return sorted(matching, key=lambda x: x['date'])

    # Strategy 3: Match on longest words (>4 chars) from payee pattern
    # This catches "Dri Flickr" when searching for "dri flickr"
    long_words = [w for w in payee_lower.split() if len(w) > 4]

    if long_words:
        # Must match ALL long words (to avoid "google" matching unrelated stuff)
        for t in all_txns:
            payee_name = (t.get('payee_name') or '').lower()
            if all(w in payee_name for w in long_words):
                matching.append(t)

    return sorted(matching, key=lambda x: x['date'])


def display_payee(payee_name, classification, transactions):
    """Display information about a payee."""
    print(f"\n{'='*110}")
    print(f"  {payee_name.upper()}")
    print(f"{'='*110}")

    print(f"  Detected: {classification['billing_period']:11} | Gap: {classification['detected_period_days']:6.0f}d | Conf: {classification['confidence']:.2f}")
    status_text = f"ACTIVE ({classification['days_since_last']}d)" if classification['status'] == 'active' else f"INACTIVE ({classification['days_since_last']}d)"
    print(f"  Status: {status_text}")

    if not transactions:
        print(f"\n  ⚠️  NO TRANSACTIONS FOUND - Check payee name or try manual lookup")
        return

    # Calculate stats
    amounts = [t['amount_dollars'] for t in transactions]
    dates = [t['date'] for t in transactions]
    categories = list(set(t.get('category_name', 'N/A') for t in transactions))

    print(f"\n  Transactions found: {len(transactions)}")
    print(f"    Amount range: ${min(amounts):.2f} - ${max(amounts):.2f}")
    print(f"    Categories: {', '.join(categories)}")

    # Calculate gaps
    if len(dates) > 1:
        gaps = []
        for i in range(len(dates) - 1):
            d1 = datetime.fromisoformat(dates[i]).date()
            d2 = datetime.fromisoformat(dates[i + 1]).date()
            gap = (d2 - d1).days
            gaps.append(gap)
        if gaps:
            print(f"    Recent gaps: {gaps[-5:]}")

    print(f"\n  Recent Transactions:")
    for t in transactions[-8:]:  # Show last 8
        payee = t.get('payee_name', 'N/A')
        print(f"    {t['date']} | ${t['amount_dollars']:8.2f} | {payee:42} | {t.get('category_name', 'N/A')}")


def ask_is_subscription():
    """Ask if this is actually a subscription."""
    while True:
        response = input(f"\n  Is this a SUBSCRIPTION? (y/n/skip): ").strip().lower()
        if response in ['y', 'yes']:
            return True
        elif response in ['n', 'no']:
            return False
        elif response in ['s', 'skip', '']:
            return None
        print("  Please enter y, n, or skip")


def ask_is_current(classification):
    """Ask if subscription is still current."""
    if classification['status'] != 'active':
        while True:
            response = input(f"\n  Still CURRENT/ACTIVE? (y/n/skip): ").strip().lower()
            if response in ['y', 'yes']:
                return True
            elif response in ['n', 'no']:
                return False
            elif response in ['s', 'skip', '']:
                return None
            print("  Please enter y, n, or skip")
    else:
        while True:
            response = input(f"\n  Still CURRENT/ACTIVE? (y/n/skip) [default: yes]: ").strip().lower()
            if response in ['y', 'yes', '']:
                return True
            elif response in ['n', 'no']:
                return False
            elif response in ['s', 'skip']:
                return None
            print("  Please enter y, n, or skip")


def ask_billing_details():
    """Ask about billing period."""
    print(f"\n  Billing period? (m/q/s/a/x/skip)")
    print(f"    m=monthly, q=quarterly, s=semi-annual, a=annual, x=mixed")

    while True:
        response = input(f"  Enter: ").strip().lower()
        if response in ['m']:
            return 'monthly'
        elif response in ['q']:
            return 'quarterly'
        elif response in ['s']:
            return 'semi-annual'
        elif response in ['a']:
            return 'annual'
        elif response in ['x']:
            return 'mixed'
        elif response in ['skip', '']:
            return None
        print("  Please enter m, q, s, a, x, or skip")


def ask_mixed_details():
    """Ask about mixed subscriptions."""
    response = input(f"  Describe (press enter to skip): ").strip()
    return response if response else None


def main():
    """Main interactive review loop."""
    results_file = (
        Path(__file__).parent.parent / "data" / "cache" / "subscriptions-full-history-v8-current.json"
    )

    if not results_file.exists():
        print("Error: subscriptions-full-history-v8-current.json not found")
        sys.exit(1)

    with open(results_file) as f:
        all_results = json.load(f)

    candidates = [r for r in all_results if r['billing_period'] in ['annual', 'semi-annual']]
    candidates.sort(key=lambda x: (x['status'] != 'active', x['days_since_last']))

    print("Fetching 5 years of transaction history...")
    all_txns = get_all_transactions(days_back=1825)
    print(f"Loaded {len(all_txns)} transactions\n")

    verified = {
        'subscriptions_active': [],
        'subscriptions_discontinued': [],
        'not_subscriptions': [],
        'skipped': [],
    }

    print(f"{'='*110}")
    print("SUBSCRIPTION REVIEW")
    print(f"{'='*110}\n")

    for i, candidate in enumerate(candidates, 1):
        payee = candidate['payee']

        txns = find_transactions_for_payee(all_txns, payee)

        display_payee(payee, candidate, txns)

        is_sub = ask_is_subscription()

        if is_sub is False:
            verified['not_subscriptions'].append({'payee': payee, 'billing_period': candidate['billing_period']})
            print("  ✗ NOT A SUBSCRIPTION")
        elif is_sub is True:
            is_current = ask_is_current(candidate)

            if is_current is None:
                verified['skipped'].append(payee)
                print("  ⊘ SKIPPED")
            else:
                billing = ask_billing_details()

                if billing is None:
                    verified['skipped'].append(payee)
                    print("  ⊘ SKIPPED")
                else:
                    record = {
                        'payee': payee,
                        'billing_period': billing,
                        'amount': candidate['min_amt'],
                    }
                    if is_current:
                        verified['subscriptions_active'].append(record)
                        print(f"  ✓ ACTIVE ({billing})")
                    else:
                        verified['subscriptions_discontinued'].append(record)
                        print(f"  ✓ DISCONTINUED ({billing})")
        else:
            verified['skipped'].append(payee)
            print("  ⊘ SKIPPED")

        print(f"  [{i}/{len(candidates)}]")

    # Save results
    output_file = Path(__file__).parent.parent / "data" / "cache" / "subscriptions-verified.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w') as f:
        json.dump(verified, f, indent=2)

    # Summary
    print(f"\n{'='*110}")
    print("COMPLETE")
    print(f"{'='*110}\n")

    print(f"✓ ACTIVE ({len(verified['subscriptions_active'])}):")
    for s in verified['subscriptions_active']:
        print(f"  {s['payee']:45} | {s['billing_period']:12} | ${s['amount']:8.2f}")

    print(f"\n✓ DISCONTINUED ({len(verified['subscriptions_discontinued'])}):")
    for s in verified['subscriptions_discontinued']:
        print(f"  {s['payee']:45} | {s['billing_period']:12} | ${s['amount']:8.2f}")

    print(f"\n✗ NOT SUBSCRIPTIONS ({len(verified['not_subscriptions'])}):")
    for s in verified['not_subscriptions'][:15]:
        print(f"  {s['payee']:45} | {s['billing_period']:12}")

    print(f"\n⊘ SKIPPED ({len(verified['skipped'])}):")
    for s in verified['skipped'][:15]:
        print(f"  {s}")

    print(f"\nSaved to {output_file}")


if __name__ == "__main__":
    main()
