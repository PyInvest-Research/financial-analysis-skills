"""Prevent advice from incomplete conversation inputs or invented prices."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

import kelly


def confirmed_request() -> dict:
    """Return an explicitly costed synthetic request, not a live forecast."""
    return {
        "event": {
            "definition": "Synthetic event A; YES iff its record is true",
            "settlement_at": "2026-12-01T00:00:00Z",
            "resolver": "Synthetic settlement ledger",
            "cancellation_rule": "No cancellation or refund outcome",
        },
        "p": 0.6,
        "probability_reason": "User-supplied synthetic assumption",
        "q": 0.7,
        "q_source": "Synthetic benchmark supplied in the request",
        "bankroll": 1000,
        "related_positions": "none",
        "outcomes": [{"name": "NO", "format": "contract", "price": 0.3,
                      "purchase_fee": 0, "profit_commission": 0}],
    }


def confirmed_joint() -> dict:
    """Return two fully disclosed independent synthetic event requests."""
    events = []
    for name in ("A", "B"):
        event = confirmed_request()
        event.pop("bankroll")
        event.pop("related_positions")
        event.update(name=name, side="NO", existing_fraction=0)
        event["event"]["definition"] = f"Synthetic binary event {name}"
        events.append(event)
    return {"bankroll": 1000, "related_positions": "none",
            "independent": True, "events": events}


class IntakeTests(unittest.TestCase):
    """Require missing inputs before producing any trading result fields."""

    def assert_waiting(self, result: dict) -> None:
        """Check that waiting is a clean stop, not a partial result card."""
        self.assertEqual(result["status"], "awaiting_input")
        self.assertTrue(result["missing_fields"])
        self.assertGreaterEqual(len(result["questions"]), 1)
        self.assertLessEqual(len(result["questions"]), 2)
        for key in ("direction", "selected", "sides", "positions",
                    "fractions", "amount", "units", "p_adj", "growth"):
            self.assertNotIn(key, result)

    def test_only_personal_and_market_probabilities_waits(self) -> None:
        """Two probability opinions do not supply any executable odds."""
        data = {"p": 0.6, "q": 0.7}
        result = kelly.dispatch("intake", data)
        self.assert_waiting(result)
        self.assertIn("outcomes", result["missing_fields"])
        self.assertIn("bankroll", result["missing_fields"])
        self.assertEqual(result, kelly.single(data))
        self.assertEqual(data, {"p": 0.6, "q": 0.7})

    def test_command_cannot_override_its_gate_with_operation(self) -> None:
        """operation selects intake only, never a sizing command's checks."""
        data = confirmed_request()
        data.pop("event")
        data["operation"] = "normalize"
        self.assert_waiting(kelly.single(data))
        joint = confirmed_joint()
        joint["events"][0]["outcomes"] = None
        self.assert_waiting(kelly.joint(joint))
        with self.assertRaises(ValueError):
            kelly.intake([])

    def test_unknown_fees_never_default_to_zero(self) -> None:
        """Reproduce the original zero-cost assumption at the real entry."""
        data = confirmed_request()
        quote = data["outcomes"][0]
        quote.pop("purchase_fee")
        quote.pop("profit_commission")
        self.assert_waiting(kelly.single(data))
        self.assert_waiting(kelly.normalize({"outcomes": [quote]}))
        with self.assertRaises(ValueError):
            kelly.normalized_outcome(quote)
        quote["purchase_fee"] = 0
        result = kelly.single(data)
        self.assert_waiting(result)
        self.assertIn("outcomes[0].profit_commission",
                      result["missing_fields"])
        quote["profit_commission"] = 0
        self.assertEqual(kelly.single(data)["status"], "ok")

    def test_missing_event_or_bankroll_cannot_show_a_result(self) -> None:
        """Actual quotes cannot replace settlement rules or a real bankroll."""
        for field in ("event", "bankroll", "related_positions"):
            with self.subTest(field=field):
                data = confirmed_request()
                data.pop(field)
                self.assert_waiting(kelly.single(data))

    def test_all_in_cost_or_net_odds_does_not_repeat_fees(self) -> None:
        """A confirmed final cost or net payoff includes cost information."""
        for quote in (
            {"name": "NO", "format": "contract", "total_cost": 0.3,
             "fees_included": True},
            {"name": "NO", "format": "net", "net_odds": 2},
        ):
            with self.subTest(quote=quote):
                data = confirmed_request()
                data["outcomes"] = [quote]
                data.pop("bankroll")
                result = kelly.intake(data)
                self.assert_waiting(result)
                self.assertEqual(result["missing_fields"], ["bankroll"])
                data["bankroll"] = 1000
                self.assertEqual(kelly.single(data)["status"], "ok")

    def test_one_side_never_produces_its_complement_quote(self) -> None:
        """Known NO execution leaves YES unknown, irrespective of q."""
        data = confirmed_request()
        result = kelly.intake(data)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["questions"], [])
        self.assertEqual(result["sides_evaluable"], ["NO"])
        self.assertEqual(result["sides_unknown"], ["YES"])
        self.assertEqual([row["name"] for row in kelly.single(data)["sides"]],
                         ["NO"])

    def test_accumulated_answers_are_not_asked_again(self) -> None:
        """The second round asks only remaining missing fields."""
        data = confirmed_request()
        data.pop("probability_reason")
        data.pop("bankroll")
        result = kelly.intake(data)
        self.assert_waiting(result)
        fields = [question["field"] for question in result["questions"]]
        self.assertEqual(fields, ["probability_reason", "bankroll"])
        self.assertEqual(result["questions"][1]["missing_fields"],
                         ["bankroll"])
        data["probability_reason"] = "No additional information"
        data["bankroll"] = 1000
        before = copy.deepcopy(data)
        ready = kelly.intake(data)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["questions"], [])
        self.assertEqual(data, before)

    def test_joint_cannot_skip_one_events_missing_cost(self) -> None:
        """All listed events must have explicit costed execution terms."""
        data = confirmed_joint()
        data["events"][1]["outcomes"][0].pop("profit_commission")
        result = kelly.joint(data)
        self.assert_waiting(result)
        self.assertIn("events[1].outcomes[0].profit_commission",
                      result["missing_fields"])
        self.assertEqual(result, kelly.intake({**data, "operation": "joint"}))

    def test_existing_positions_cannot_be_silently_ignored(self) -> None:
        """Single redirects related positions; joint needs their size."""
        data = confirmed_request()
        data["related_positions"] = ["A"]
        self.assert_waiting(kelly.single(data))
        joint = confirmed_joint()
        joint["related_positions"] = ["A"]
        joint["events"][0].pop("existing_fraction")
        self.assert_waiting(kelly.joint(joint))
        joint["events"][0]["existing_fraction"] = 0.05
        self.assertEqual(kelly.intake({**joint, "operation": "joint"})[
            "status"], "ready")

    def test_demo_requires_an_explicit_request_and_inputs(self) -> None:
        """Example mode cannot be used as a missing-data escape hatch."""
        data = confirmed_request()
        data["purpose"] = "example"
        self.assert_waiting(kelly.single(data))
        data["example_requested"] = True
        result = kelly.single(data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["purpose"], "example")
        data.pop("bankroll")
        self.assert_waiting(kelly.single(data))

    def test_ambiguous_gross_vs_net_payout_waits(self) -> None:
        """A colloquial payout needs its principal/net-profit meaning."""
        data = confirmed_request()
        data["outcomes"] = [{"name": "YES", "format": "stake",
                             "stake": 100, "payout": 150,
                             "purchase_fee": 0, "profit_commission": 0}]
        result = kelly.single(data)
        self.assert_waiting(result)
        self.assertIn("outcomes[0].payout_kind", result["missing_fields"])

    def test_invalid_numbers_still_raise_instead_of_being_hidden(self) -> None:
        """An incomplete request does not make NaN a valid probability."""
        with self.assertRaises(ValueError):
            kelly.intake({"p": float("nan"), "q": 0.7})

    def test_devig_waits_for_its_missing_execution_side(self) -> None:
        """A one-sided quote cannot supply a two-sided de-vig baseline."""
        data = confirmed_request()
        data.pop("q")
        data.pop("q_source")
        data["benchmark"] = {"kind": "devig", "source": "Synthetic book"}
        for result in (kelly.intake(data), kelly.single(data),
                       kelly.normalize(data)):
            self.assert_waiting(result)
            self.assertIn("outcomes.YES", result["missing_fields"])

    def test_null_joint_scenarios_are_missing_not_ready(self) -> None:
        """Null is not a supplied joint table or a conditional probability."""
        for field in ("scenario_probabilities", "conditional_b_given_a"):
            data = confirmed_joint()
            data.pop("independent")
            data[field] = None
            self.assert_waiting(kelly.intake({**data, "operation": "joint"}))
            self.assert_waiting(kelly.joint(data))
            data["independent"] = True
            self.assertEqual(kelly.joint(data)["status"], "ok")

    def test_k_with_null_q_needs_no_invented_baseline(self) -> None:
        """An explicitly chosen k mode works with a missing optional q."""
        data = confirmed_request()
        data.update(mode="k", k=0.5, q=None)
        data.pop("q_source")
        self.assertEqual(kelly.intake(data)["status"], "ready")
        self.assertEqual(kelly.single(data)["status"], "ok")

    def test_invalid_midpoint_cannot_report_ready(self) -> None:
        """Readiness and sizing use the same complete benchmark validator."""
        data = confirmed_request()
        data.pop("q")
        data["benchmark"] = {"kind": "midpoint", "bid": 0.8, "ask": 0.6,
                             "source": "Invalid synthetic quote"}
        with self.assertRaises(ValueError):
            kelly.intake(data)

    def test_intake_cli_stops_with_one_json_document(self) -> None:
        """Missing inputs are an ordinary waiting state, not guessed advice."""
        script = Path(__file__).with_name("kelly.py")
        result = subprocess.run(
            [sys.executable, str(script), "intake"],
            input=json.dumps({"p": 0.6, "q": 0.7}), capture_output=True,
            text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_waiting(json.loads(result.stdout))
