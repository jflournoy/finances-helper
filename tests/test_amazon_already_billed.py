"""Parcels paid for by a charge this run cannot write to.

An Amazon charge that was approved, reconciled, or split on an earlier run is
invisible to `filter_uncategorized_writable`. Before this behaviour existed,
the parcel it paid for therefore stayed "unmatched" forever, with two costs:

1. it was offered as a near-miss candidate for every later charge in the
   window — noise the reviewer had to read past; and
2. because both parcels of a two-parcel order looked unmatched, the
   whole-order rollup offered a fused sum the order was never charged.

The anchor case throughout is real: Amazon order 112-3967129-9713830 shipped
in two parcels, $99.18 and $21.54, and was billed as $99.18 to the card plus
$7.09 — $14.45 of the second parcel came off in rewards points, which the
order-history export does not record. The $99.18 charge was categorized on an
earlier run. The reviewer's job is to recognise the $21.54 parcel as the
remainder behind the $7.09 charge, and the export alone will never say so.
"""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

import decide
from amazon_matcher import (
    AmazonItem,
    AmazonShipment,
    filter_already_billed_amazon_transactions,
    match_shipments_to_transactions,
)
from tag import write_unified_changeset


ORDER = "112-3967129-9713830"


def _parcel(total, ship_date, order_id=ORDER, product="Item", row_index=1):
    item = AmazonItem(
        order_id=order_id,
        ship_date=ship_date,
        asin="B00000000",
        product_name=product,
        quantity=1,
        unit_price=Decimal(total),
        unit_price_tax=Decimal("0.00"),
        raw_row_index=row_index,
    )
    return AmazonShipment(
        order_id=order_id,
        ship_date=ship_date,
        payment_method_raw="Visa - 5219",
        payment_method_last4="5219",
        is_split_tender=False,
        currency="USD",
        item_subtotal=Decimal(total),
        tax=Decimal("0.00"),
        shipping=Decimal("0.00"),
        discounts=Decimal("0.00"),
        total_amount=Decimal(total),
        items=[item],
        shipment_status="Shipped",
    )


def _txn(txn_id, txn_date, amount_dollars, **kw):
    txn = {
        "id": txn_id,
        "account_id": "acct-1",
        "account_name": "Amazon",
        "date": txn_date,
        "amount": int(Decimal(amount_dollars) * -1000),
        "payee_name": "Amazon",
        "category_id": None,
        "cleared": "cleared",
        "deleted": False,
    }
    txn.update(kw)
    return txn


# The two parcels, and the three charges: one settled, one still to review.
PARCEL_BIG = lambda: _parcel("99.18", date(2026, 8, 3), product="Slant Tweezer", row_index=1)
PARCEL_SMALL = lambda: _parcel("21.54", date(2026, 8, 2), product="Manicure Case", row_index=5)
SETTLED_CHARGE = lambda: _txn("txn-99", "2026-08-04", "99.18", approved=True)
REMAINDER_CHARGE = lambda: _txn("txn-709", "2026-08-03", "7.09")


# ============================================================================
# Matcher: the settled charge consumes its parcel
# ============================================================================


class TestMatcherConsumesSettledCharges:

    def test_settled_parcel_leaves_the_unmatched_pool(self):
        result = match_shipments_to_transactions(
            [REMAINDER_CHARGE()],
            [PARCEL_BIG(), PARCEL_SMALL()],
            already_billed_txns=[SETTLED_CHARGE()],
        )

        assert [s.total_amount for s in result.unmatched_shipments] == [Decimal("21.54")]
        assert [m.shipment.total_amount for m in result.already_billed] == [Decimal("99.18")]

    def test_settled_charge_produces_no_proposal(self):
        """`matched` becomes split proposals and then PATCHes. It must stay clean."""
        result = match_shipments_to_transactions(
            [REMAINDER_CHARGE()],
            [PARCEL_BIG(), PARCEL_SMALL()],
            already_billed_txns=[SETTLED_CHARGE()],
        )

        assert result.matched == []
        assert "txn-99" not in {t["id"] for t, _ in result.unmatched_ynab}

    def test_whole_order_rollup_is_not_offered_once_a_parcel_is_settled(self):
        """The order was billed twice, so its $120.72 sum was never charged.

        This is the defect: with the $99.18 parcel wrongly looking unmatched,
        Phase 5 fused both parcels and matched — or offered — a total that
        never appeared on the card.
        """
        result = match_shipments_to_transactions(
            [REMAINDER_CHARGE(), _txn("txn-120", "2026-08-03", "120.72")],
            [PARCEL_BIG(), PARCEL_SMALL()],
            already_billed_txns=[SETTLED_CHARGE()],
        )

        assert [m.shipment.total_amount for m in result.matched] == []
        assert Decimal("120.72") not in {m.shipment.total_amount for m in result.already_billed}

    def test_without_the_settled_charge_the_bogus_rollup_still_fires(self):
        """Pins the pre-fix behaviour, so the test above is testing the fix."""
        result = match_shipments_to_transactions(
            [REMAINDER_CHARGE(), _txn("txn-120", "2026-08-03", "120.72")],
            [PARCEL_BIG(), PARCEL_SMALL()],
        )

        assert [m.shipment.total_amount for m in result.matched] == [Decimal("120.72")]


# ============================================================================
# Partitioning the window
# ============================================================================


class TestPartitioningTheWindow:

    def test_settled_charges_are_the_complement_of_writable(self):
        settled = SETTLED_CHARGE()
        remainder = REMAINDER_CHARGE()

        billed = filter_already_billed_amazon_transactions(
            [settled, remainder], [remainder]
        )

        assert [t["id"] for t in billed] == ["txn-99"]

    def test_non_amazon_and_deleted_charges_are_dropped(self):
        grocery = _txn("txn-tj", "2026-08-04", "30.00", payee_name="Trader Joe's")
        gone = _txn("txn-gone", "2026-08-04", "30.00", deleted=True)

        assert filter_already_billed_amazon_transactions([grocery, gone], []) == []

    def test_a_charge_in_both_lists_raises(self):
        """Writable or settled, not both — deciding by accident is not acceptable."""
        txn = REMAINDER_CHARGE()

        with pytest.raises(ValueError) as exc_info:
            match_shipments_to_transactions([txn], [], already_billed_txns=[txn])

        assert "txn-709" in str(exc_info.value)


# ============================================================================
# Tiebreaks
# ============================================================================


class TestWritableChargesWinOutright:
    """Settled charges never compete with writable ones for a parcel."""

    def test_writable_wins_even_when_the_settled_charge_is_closer(self):
        """A settled charge must not spend a parcel the reviewer could still claim.

        The reviewer sees and can reject a wrong proposal. A parcel a settled
        charge takes just disappears from the candidate lists, unseen.
        """
        parcel = _parcel("50.00", date(2026, 8, 3), order_id="112-CONTEST")

        result = match_shipments_to_transactions(
            [_txn("txn-writable", "2026-08-06", "50.00")],
            [parcel],
            already_billed_txns=[_txn("txn-settled", "2026-08-03", "50.00", approved=True)],
        )

        assert [m.ynab_txn["id"] for m in result.matched] == ["txn-writable"]
        assert result.already_billed == []

    def test_contended_parcels_are_left_for_the_human(self):
        """Two writable charges already contend for this parcel; a settled charge
        must not step in and decide it."""
        parcel = _parcel("50.00", date(2026, 8, 3), order_id="112-CONTEST")

        result = match_shipments_to_transactions(
            [
                _txn("txn-w1", "2026-08-03", "50.00"),
                _txn("txn-w2", "2026-08-03", "50.00"),
            ],
            [parcel],
            already_billed_txns=[_txn("txn-settled", "2026-08-03", "50.00", approved=True)],
        )

        assert result.already_billed == []


class TestAmbiguityBlocksConsumption:
    """Where several settled charges could explain a parcel, take none of them.

    Drawn from the live data: three unrelated $21.54 parcels shipped 2026-08-02,
    -04 and -05, and two settled $21.54 charges on 08-05 and 08-06. A greedy
    "resolve singletons first" pass spent the 08-02 parcel — the one that was
    actually billed $7.09 after points, and the one the reviewer needed — on the
    08-05 charge, because it was that parcel's only option. Nothing here is
    unambiguous, so nothing may be taken.
    """

    def _three_parcels(self):
        return [
            _parcel("21.54", date(2026, 8, 2), order_id="112-AAA", row_index=1),
            _parcel("21.54", date(2026, 8, 4), order_id="113-BBB", row_index=2),
            _parcel("21.54", date(2026, 8, 5), order_id="113-CCC", row_index=3),
        ]

    def _two_settled(self):
        return [
            _txn("txn-a", "2026-08-05", "21.54", approved=True),
            _txn("txn-b", "2026-08-06", "21.54", approved=True),
        ]

    def test_no_parcel_is_consumed(self):
        result = match_shipments_to_transactions(
            [], self._three_parcels(), already_billed_txns=self._two_settled()
        )

        assert result.already_billed == []
        assert {s.order_id for s in result.unmatched_shipments} == {
            "112-AAA", "113-BBB", "113-CCC"
        }

    def test_one_settled_charge_and_one_parcel_is_consumed(self):
        """The rule is ambiguity, not caution for its own sake."""
        result = match_shipments_to_transactions(
            [],
            [_parcel("21.54", date(2026, 8, 2), order_id="112-AAA")],
            already_billed_txns=[_txn("txn-a", "2026-08-05", "21.54", approved=True)],
        )

        assert [m.shipment.order_id for m in result.already_billed] == ["112-AAA"]
        assert result.unmatched_shipments == []

    def test_consumption_repeats_until_nothing_more_is_unique(self):
        """Retiring an unambiguous pair can make the next pair unambiguous."""
        near = _parcel("40.00", date(2026, 8, 2), order_id="112-NEAR", row_index=1)
        far = _parcel("40.00", date(2026, 8, 9), order_id="112-FAR", row_index=2)
        # txn-near is in window for both parcels; txn-far only for `far`.
        settled = [
            _txn("txn-near", "2026-08-03", "40.00", approved=True),
            _txn("txn-far", "2026-08-10", "40.00", approved=True),
        ]

        result = match_shipments_to_transactions(
            [], [near, far], already_billed_txns=settled
        )

        pairs = {m.shipment.order_id: m.ynab_txn["id"] for m in result.already_billed}
        assert pairs == {"112-NEAR": "txn-near", "112-FAR": "txn-far"}


# ============================================================================
# Changeset: candidates, annotation, audit trail
# ============================================================================


def _changeset_for(match_result, tmp_path):
    _, json_path = write_unified_changeset(
        flat_results=[],
        skipped=[],
        unmatched_amazon=list(match_result.unmatched_ynab),
        split_proposals=[],
        source_txns=[],
        budget_id="b123",
        since_date="2026-07-01",
        days_back=60,
        K=312,
        confidence_threshold=0.0096,
        dump_path=None,
        out_dir=tmp_path / "changesets",
        match_result=match_result,
        now=datetime(2026, 9, 8, 12, 0, 0),
    )
    return json.loads(json_path.read_text())["amazon"]


class TestChangeset:

    def _real_case(self):
        return match_shipments_to_transactions(
            [REMAINDER_CHARGE()],
            [PARCEL_BIG(), PARCEL_SMALL()],
            already_billed_txns=[SETTLED_CHARGE()],
            date_window_days=3,
        )

    def test_settled_parcel_is_not_offered_as_a_candidate(self, tmp_path):
        amazon = self._changeset(tmp_path)
        unmatched = amazon["unmatched_ynab"]

        assert [u["transaction_id"] for u in unmatched] == ["txn-709"]
        amounts = [c["amount_dollars"] for c in unmatched[0]["candidate_shipments"]]
        assert "99.18" not in amounts

    def test_no_whole_order_candidate_is_offered(self, tmp_path):
        """The order's parcels were billed separately — their sum is not a charge."""
        amazon = self._changeset(tmp_path)
        candidates = amazon["unmatched_ynab"][0]["candidate_shipments"]

        assert all(c.get("parcels", 1) == 1 for c in candidates)
        assert "120.72" not in [c["amount_dollars"] for c in candidates]

    def test_surviving_candidate_is_annotated_as_the_remainder(self, tmp_path):
        """The one fact the export never states, and the reviewer needed Amazon for."""
        amazon = self._changeset(tmp_path)
        candidate = amazon["unmatched_ynab"][0]["candidate_shipments"][0]

        assert candidate["amount_dollars"] == "21.54"
        assert candidate["order_billed_elsewhere"] == [
            {"ship_date": "2026-08-03", "amount_dollars": "99.18", "charge_date": "2026-08-04"}
        ]

    def test_settled_parcels_are_recorded_for_audit(self, tmp_path):
        """A parcel absent from the candidates must be findable, not simply gone."""
        amazon = self._changeset(tmp_path)

        assert len(amazon["already_billed_shipments"]) == 1
        entry = amazon["already_billed_shipments"][0]
        assert entry["shipment"]["order_id"] == ORDER
        assert entry["charge"]["transaction_id"] == "txn-99"
        assert entry["charge"]["amount_dollars"] == "-99.18"

    def _changeset(self, tmp_path):
        return _changeset_for(self._real_case(), tmp_path)


# ============================================================================
# Review walk
# ============================================================================


def test_walk_prints_the_remainder_note(monkeypatch, capsys):
    """The reviewer sees why this candidate's amount does not equal the charge."""
    changeset = {
        "amazon": {
            "unmatched_ynab": [
                {
                    "transaction_id": "txn-709",
                    "payee_name": "Amazon",
                    "amount_dollars": "-7.09",
                    "date": "2026-08-03",
                    "memo": None,
                    "reason": "no matching shipment in dump",
                    "candidate_shipments": [
                        {
                            "order_id": ORDER,
                            "ship_date": "2026-08-02",
                            "amount_dollars": "21.54",
                            "amount_delta_dollars": "14.45",
                            "date_delta_days": -1,
                            "parcels": 1,
                            "order_billed_elsewhere": [
                                {
                                    "ship_date": "2026-08-03",
                                    "amount_dollars": "99.18",
                                    "charge_date": "2026-08-04",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "s")

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index={}, item_index_warning=None
    )

    out = capsys.readouterr().out
    assert "$99.18" in out
    assert "billed separately on 2026-08-04" in out
    assert "remainder" in out


def test_walk_prints_no_note_when_nothing_was_billed_elsewhere(monkeypatch, capsys):
    changeset = {
        "amazon": {
            "unmatched_ynab": [
                {
                    "transaction_id": "txn-709",
                    "payee_name": "Amazon",
                    "amount_dollars": "-7.09",
                    "date": "2026-08-03",
                    "memo": None,
                    "reason": "no matching shipment in dump",
                    "candidate_shipments": [
                        {
                            "order_id": ORDER,
                            "ship_date": "2026-08-02",
                            "amount_dollars": "21.54",
                            "amount_delta_dollars": "14.45",
                            "date_delta_days": -1,
                            "parcels": 1,
                        }
                    ],
                }
            ]
        },
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "s")

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index={}, item_index_warning=None
    )

    assert "remainder" not in capsys.readouterr().out
