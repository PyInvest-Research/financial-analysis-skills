"""Verify the specification, numerical properties, and JSON CLI contract."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np

import kelly


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "assets/test-cases.json").read_text())


def case_input(case: dict) -> dict:
    """Expand synthetic repetition shorthand used only by the test table."""
    value = copy.deepcopy(case["input"])
    if "repeat" in value:
        repeat = value.pop("repeat")
        value["records"] = [dict(repeat["record"])
                            for _ in range(repeat["count"])]
    return value


class SpecificationTests(unittest.TestCase):
    """Execute every numbered row from the supplied specification."""

    def test_published_video_result_matches_script(self) -> None:
        """Ensure the website's fixed example comes from current Python."""
        data = json.loads(
            (ROOT / "assets/video-example-input.json").read_text())
        saved = json.loads(
            (ROOT / "assets/video-example-result.json").read_text())
        self.assertEqual(kelly.single(data), saved)

    def test_all_sixteen_cases(self) -> None:
        """Match the exact formulas rather than rounded table percentages."""
        self.assertEqual(len(CASES), 16)
        for case in CASES:
            with self.subTest(case=case["id"], label=case["label"]):
                result = kelly.dispatch(case["command"], case_input(case))
                expected = case["expected"]
                if "status" in expected:
                    self.assertEqual(result["status"], expected["status"])
                if "direction" in expected:
                    self.assertEqual(
                        result["direction"], expected["direction"])
                if "q" in expected:
                    self.assertAlmostEqual(
                        result["q"], expected["q"], places=8)
                if "lambda_hat" in expected:
                    self.assertEqual(result["lambda_hat"], 0)
                if "fractions" in expected:
                    np.testing.assert_allclose(
                        result["fractions"], expected["fractions"], atol=1e-6)
                if "total_fraction" in expected:
                    self.assertAlmostEqual(sum(result["fractions"]),
                                           expected["total_fraction"],
                                           delta=1e-6)
                if result.get("status") == "ok" and "fraction" in expected:
                    selected = result["selected"]
                    for key in ("fraction", "full_fraction", "amount",
                                "units", "wealth_if_yes", "wealth_if_no",
                                "growth", "b", "effective_price",
                                "scaled_full_fraction", "equivalent"):
                        if key in expected:
                            self.assertAlmostEqual(
                                selected[key], expected[key], delta=1e-6)


class PropertyTests(unittest.TestCase):
    """Exercise the four required properties with reproducible randomness."""

    def test_no_two_positive_sides_without_arbitrage(self) -> None:
        """Fair or overround complementary sides cannot both have an edge."""
        rng = np.random.default_rng(20261104)
        for _ in range(250):
            price = rng.uniform(0.05, 0.9)
            other = rng.uniform(1 - price, 0.99)
            result = kelly.single({
                **case_input(CASES[0]),
                "p": float(rng.uniform()), "q": float(price),
                "lambda": float(rng.uniform()), "bankroll": 1000,
                "outcomes": [
                    {"name": "YES", "format": "contract", "price": price,
                     "purchase_fee": 0, "profit_commission": 0},
                    {"name": "NO", "format": "contract", "price": other,
                     "purchase_fee": 0, "profit_commission": 0},
                ],
            })
            positives = [row["fraction"] > 1e-12
                         for row in result["sides"]]
            self.assertLessEqual(sum(positives), 1)

    def test_independent_identical_events(self) -> None:
        """Joint positions are symmetric and below standalone size."""
        event = case_input(CASES[12])["events"][0]
        result = kelly.joint({"lambda": 1, "bankroll": 1000,
                              "related_positions": "none",
                              "independent": True,
                              "events": [event, {**event, "name": "B"}]})
        self.assertGreater(result["fractions"][0], 0)
        self.assertLess(result["fractions"][0], 0.25)
        self.assertAlmostEqual(*result["fractions"], delta=1e-6)

    def test_informative_forecaster_gets_large_lambda(self) -> None:
        """Synthetic outcomes drawn from p favor p over a noisy benchmark."""
        rng = np.random.default_rng(20261104)
        p = rng.uniform(0.05, 0.95, size=1600)
        q = np.clip(p + rng.normal(0, 0.25, size=len(p)), 0.01, 0.99)
        y = rng.binomial(1, p)
        records = [{"p": float(a), "q": float(b), "y": int(c)}
                   for a, b, c in zip(p, q, y)]
        result = kelly.calibrate({"records": records})
        self.assertGreater(result["lambda_hat"], 0.5)
        self.assertEqual(result["bootstrap"]["replicates"], 1000)

    def test_fair_odds_shrinkage_equals_fractional_kelly(self) -> None:
        """Without costs or spread the signed fractions agree algebraically."""
        rng = np.random.default_rng(20261104)
        for _ in range(150):
            q, p, weight = rng.uniform(0.05, 0.95, size=3)
            result = kelly.single({
                **case_input(CASES[0]),
                "p": float(p), "q": float(q), "lambda": float(weight),
                "bankroll": 1000,
                "outcomes": [{"name": "YES", "format": "contract",
                              "price": float(q), "purchase_fee": 0,
                              "profit_commission": 0}],
            })["sides"][0]
            self.assertTrue(result["equivalent"])
            self.assertAlmostEqual(result["candidate_fraction"],
                                   weight * result["full_fraction"], places=12)


class BoundaryTests(unittest.TestCase):
    """Cover ambiguity, caps, scenarios, existing exposure, and leakage."""

    def test_plain_language_and_cost_overrides(self) -> None:
        """Payout semantics are explicit and direct net odds override fees."""
        for payout, expected in (("net", 1.5), ("total", 0.5)):
            result = kelly.normalize({"outcomes": [{
                "name": "YES", "format": "stake", "stake": 100,
                "payout": 150, "payout_kind": payout,
                "purchase_fee": 0, "profit_commission": 0,
            }]})
            self.assertAlmostEqual(result["outcomes"][0]["b"], expected)
        missing = kelly.normalize({"outcomes": [{
            "name": "YES", "format": "stake", "stake": 100,
            "payout": 150, "purchase_fee": 0, "profit_commission": 0,
        }]})
        self.assertEqual(missing["status"], "awaiting_input")
        self.assertIn("outcomes[0].payout_kind", missing["missing_fields"])
        row = {"name": "YES", "format": "contract", "price": 0.4,
               "purchase_fee": 0.5, "net_odds": 1.5}
        self.assertEqual(kelly.normalize({"outcomes": [row]})[
            "outcomes"][0]["b"], 1.5)

    def test_invalid_and_missing_inputs(self) -> None:
        """Reject invalid values and wait for a missing baseline."""
        base = case_input(CASES[0])
        for change in ({"p": float("nan")}, {"p": True}, {"p": 1.01},
                       {"mode": "wrong"}, {"lambda": -0.1}, {"bankroll": 0},
                       {"days": 0}, {"cap": 1.1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                kelly.single({**base, **change})
        self.assertEqual(kelly.single({**base, "q": None})["status"],
                         "awaiting_input")

    def test_no_edge_never_gives_amount(self) -> None:
        """Nonpositive sizes end without an actionable money field."""
        result = kelly.single(case_input(CASES[4]))
        self.assertNotIn("selected", result)
        self.assertTrue(all("amount" not in row for row in result["sides"]))
        result = kelly.single(case_input(CASES[11]))
        self.assertNotIn("selected", result)

    def test_cap_growth_and_endpoint_confirmation(self) -> None:
        """Compute wealth and growth using the actual capped stake."""
        result = kelly.single({**case_input(CASES[0]), "cap": 0.05,
                               "days": 10})["selected"]
        self.assertEqual(result["amount"], 50)
        self.assertTrue(result["cap_applied"])
        self.assertAlmostEqual(result["daily_growth"], result["growth"] / 10)
        data = {**case_input(CASES[0]), "p": 1}
        self.assertEqual(kelly.single(data)["status"],
                         "confirmation_required")
        data["certainty_confirmed"] = True
        self.assertEqual(kelly.single(data)["status"], "ok")

    def test_joint_invalid_scenarios_and_marginals(self) -> None:
        """Reject impossible conditional tables and inconsistent marginals."""
        data = case_input(CASES[13])
        data["events"][1]["p"] = 0.1
        with self.assertRaises(ValueError):
            kelly.joint(data)
        data = case_input(CASES[13])
        data.pop("conditional_b_given_a")
        data["scenario_probabilities"] = [0.2, 0.2, 0.2, 0.2]
        with self.assertRaises(ValueError):
            kelly.joint(data)
        data["scenario_probabilities"] = [0.25] * 4
        with self.assertRaises(ValueError):
            kelly.joint(data)

    def test_joint_three_events_no_side_and_existing_position(self) -> None:
        """NO returns invert correctly and held positions consume bankroll."""
        event = case_input(CASES[12])["events"][0]
        events = [dict(event, name=name) for name in ("A", "B", "C")]
        events[0] = {**events[0], "side": "NO", "p": 0.3,
                     "existing_fraction": 0.05,
                     "outcomes": [{"name": "NO", "format": "contract",
                                   "price": 0.6, "purchase_fee": 0,
                                   "profit_commission": 0}]}
        data = {"events": events, "bankroll": 1000, "lambda": 1,
                "independent": True, "related_positions": ["A"]}
        result = kelly.joint(data)
        self.assertLessEqual(sum(result["total_fractions"]), 1 - 1e-6)
        self.assertTrue(all(row["wealth"] > 0 for row in result["scenarios"]))
        self.assertGreater(result["fractions"][0], 0)
        data["mode"] = "k"
        data.pop("lambda")
        data["k"] = 0.5
        with self.assertRaises(ValueError):
            kelly.joint(data)

    def test_temporal_calibration_preserves_time_and_ties(self) -> None:
        """Outer training and test periods never share timestamps."""
        records = [{"p": 0.5, "q": 0.5, "y": i % 2,
                    "timestamp": f"2026-01-{1 + i // 4:02d}T00:00:00Z"}
                   for i in range(100)]
        result = kelly.calibrate({"records": list(reversed(records))})
        self.assertEqual(result["lambda_hat"], 0)
        self.assertEqual(result["evaluation"], "nested_rolling_origin")
        for fold in result["folds"]:
            self.assertLess(fold["training_end"], fold["test_start"])
        self.assertEqual(result["bootstrap"]["interval_90"], [0, 0])
        records[0].pop("timestamp")
        with self.assertRaises(ValueError):
            kelly.calibrate({"records": records})

    def test_four_cli_commands_and_errors_are_json(self) -> None:
        """CLI stdout is one JSON document, including failures."""
        for index in (0, 10, 12, 14):
            case = CASES[index]
            process = subprocess.run(
                [sys.executable, str(ROOT / "scripts/kelly.py"),
                 case["command"]], input=json.dumps(case_input(case)),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("status", json.loads(process.stdout))
        for arguments, payload in ((["single"], "{broken"),
                                   (["unknown"], "{}"),
                                   (["single"], "{\"p\":NaN}")):
            process = subprocess.run(
                [sys.executable, str(ROOT / "scripts/kelly.py"), *arguments],
                input=payload, capture_output=True, text=True, check=False,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["status"], "error")


if __name__ == "__main__":
    unittest.main()
