"""Check inputs, normalize binary odds, and size stakes using JSON commands.

Import intake, normalize, single, joint, or calibrate with a JSON dict.
The CLI reads stdin or --input FILE and writes one JSON object to stdout.
No command fetches prices, estimates probabilities, or places orders.
"""

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any


Json = dict[str, Any]
EPSILON = 1e-6
TOLERANCE = 1e-12
REMINDERS = [
    "Read the settlement rules before acting.",
    "Check correlation with your other positions.",
    "Consider how long capital stays locked.",
    "Prices change: recalculate immediately before any order.",
    "This is not investment advice; check your local laws.",
    "Write down: what evidence would make me change p?",
]


def number(value: Any, name: str, lower: float | None = None,
           upper: float | None = None) -> float:
    """Return a finite numeric input or raise ValueError on invalid bounds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a JSON number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if lower is not None and result < lower:
        raise ValueError(f"{name} must be >= {lower}")
    if upper is not None and result > upper:
        raise ValueError(f"{name} must be <= {upper}")
    return result


def positive(value: Any, name: str) -> float:
    """Return a strictly positive finite number or raise ValueError."""
    result = number(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be > 0")
    return result


def probability(value: Any, name: str = "probability") -> float:
    """Validate a probability without silently clipping user assumptions."""
    return number(value, name, 0, 1)


def normalized_outcome(row: Json) -> Json:
    """Convert explicit odds and fees to net profit per all-in unit staked.

    total_cost overrides the purchase fee for a contract. net_odds overrides
    both fee models. Unit counts require an actual all-in contract cost.
    Raise ValueError for ambiguous payout semantics or impossible prices.
    """
    from kelly_intake import quote_missing
    missing = quote_missing(row, "outcome")
    if missing:
        raise ValueError("Incomplete execution inputs: " + ", ".join(missing))
    if not isinstance(row, dict) or row.get("name") not in ("YES", "NO"):
        raise ValueError("Each outcome must have name YES or NO")
    kind = row.get("format")
    price = None
    if kind == "contract":
        supplied = row.get("price")
        price = number(supplied if supplied is not None else row["total_cost"],
                       "price", 0, 1)
        if not 0 < price < 1:
            raise ValueError("Contract price must be strictly between 0 and 1")
        base_b = (1 - price) / price
    elif kind == "decimal":
        base_b = positive(row.get("odds"), "decimal odds") - 1
    elif kind == "american":
        odds = number(row.get("odds"), "American odds")
        if odds == 0:
            raise ValueError("American odds cannot be zero")
        base_b = odds / 100 if odds > 0 else 100 / abs(odds)
    elif kind == "stake":
        stake = positive(row.get("stake"), "stake")
        payout = positive(row.get("payout"), "payout")
        if row.get("payout_kind") not in ("net", "total"):
            raise ValueError("Confirm payout_kind: net profit or total return")
        base_b = payout / stake - (row["payout_kind"] == "total")
    elif kind == "net":
        base_b = positive(row.get("net_odds"), "net_odds")
    else:
        raise ValueError(
            "format must be contract, decimal, american, stake, net")
    if base_b <= 0:
        raise ValueError("Winning must produce a positive net profit")
    phi = number(row.get("purchase_fee", 0), "purchase_fee", 0)
    commission = number(row.get("profit_commission", 0),
                        "profit_commission", 0, 1)
    effective_price = (1 / (1 + base_b)) * (1 + phi)
    unit_cost = effective_price if kind == "contract" else None
    if "total_cost" in row:
        if kind != "contract":
            raise ValueError("total_cost is supported only for contracts")
        effective_price = positive(row["total_cost"], "total_cost")
        unit_cost = effective_price
    if "net_odds" in row:
        b = positive(row["net_odds"], "net_odds")
        if "total_cost" not in row:
            unit_cost = None
        effective_price = unit_cost
    else:
        if effective_price >= 1:
            raise ValueError("All-in purchase cost must be below the payout")
        b = ((1 - effective_price) / effective_price) * (1 - commission)
        if b <= 0:
            raise ValueError("Costs leave no positive net winnings")
    return {
        "name": row["name"], "format": kind, "b": b, "base_b": base_b,
        "price": price, "effective_price": effective_price,
        "unit_cost": unit_cost, "break_even": 1 / (1 + b),
        "raw_implied_probability": 1 / (1 + base_b),
    }


def normalize(data: Json) -> Json:
    """Normalize one/two quoted binary sides and a separate external baseline.

    A midpoint uses YES bid/ask; de-vig uses pre-fee implied probabilities.
    No missing complementary execution price is invented.
    """
    from kelly_intake import assess_inputs
    ready = assess_inputs(data, "normalize")
    if ready["status"] != "ready":
        return ready
    rows = data.get("outcomes")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError("outcomes must contain one or two binary quotes")
    outcomes = [normalized_outcome(row) for row in rows]
    by_name = {row["name"]: row for row in outcomes}
    if len(by_name) != len(rows):
        raise ValueError("Duplicate outcome names")
    q = None
    source = None
    if "q" in data:
        q = probability(data["q"], "q")
        source = data.get("q_source", "user-provided baseline")
    benchmark = data.get("benchmark")
    if benchmark is not None:
        if q is not None or not isinstance(benchmark, dict):
            raise ValueError("Supply either q or a benchmark object")
        kind = benchmark.get("kind")
        if kind == "midpoint":
            bid = probability(benchmark.get("bid"), "YES bid")
            ask = probability(benchmark.get("ask"), "YES ask")
            if bid > ask:
                raise ValueError("YES bid cannot exceed ask")
            q = (bid + ask) / 2
        elif kind == "devig":
            if len(outcomes) != 2:
                raise ValueError("De-vig requires both quoted sides")
            raw = [row["raw_implied_probability"] for row in outcomes]
            q = by_name["YES"]["raw_implied_probability"] / sum(raw)
        elif kind == "external":
            q = probability(benchmark.get("value"), "external q")
            if not benchmark.get("source"):
                raise ValueError("External baseline requires a source")
        else:
            raise ValueError("Unknown benchmark kind")
        source = benchmark.get("source", kind)
    arbitrage = False
    if len(outcomes) == 2:
        arbitrage = sum(row["break_even"] for row in outcomes) < 1 - TOLERANCE
        if all(row["price"] is not None for row in outcomes):
            arbitrage |= sum(row["price"] for row in outcomes) < 1 - TOLERANCE
    return {"status": "ok", "outcomes": outcomes, "q": q,
            "q_source": source, "arbitrage": bool(arbitrage)}


def settings(data: Json, has_baseline: bool) -> tuple[str, float]:
    """Validate the mode without inventing k or a baseline."""
    mode = data.get("mode", "lambda")
    if mode == "lambda":
        if not has_baseline:
            raise ValueError("No baseline to shrink toward: choose k mode")
        if "k" in data:
            raise ValueError("Choose lambda or k, not both")
        return mode, probability(data.get("lambda", 0.5), "lambda")
    if mode == "k":
        if "lambda" in data:
            raise ValueError("Choose lambda or k, not both")
        return mode, probability(data.get("k"), "k")
    raise ValueError("mode must be lambda or k")


def full_kelly(p: float, b: float) -> float:
    """Return the signed analytic optimum for edge diagnostics."""
    return p - (1 - p) / b


def growth(p: float, b: float, fraction: float) -> float | None:
    """Return expected natural-log growth, or null for a ruinous allocation.

    Zero-probability outcomes contribute zero, including a certain win at
    full allocation. JSON never receives NaN or Infinity.
    """
    total = 0.0
    for chance, wealth in ((p, 1 + fraction * b),
                           (1 - p, 1 - fraction)):
        if chance > 0:
            if wealth <= 0:
                return None
            total += chance * math.log(wealth)
    return total


def common(data: Json) -> tuple[float, float, float | None]:
    """Validate bankroll, optional per-position cap, and days to settlement."""
    bankroll = positive(data.get("bankroll"), "bankroll")
    cap = probability(data.get("cap", 1), "cap")
    days = positive(data["days"], "days") if "days" in data else None
    return bankroll, cap, days


def single(data: Json) -> Json:
    """Evaluate all supplied sides, select an edge, and compute actual wealth.

    Invalid inputs raise ValueError. No-edge, arbitrage, and unconfirmed
    certainty return structured stop states without an actionable amount.
    """
    from kelly_intake import assess_inputs
    ready = assess_inputs(data, "single")
    if ready["status"] != "ready":
        return ready
    normalized = normalize(data)
    p = probability(data.get("p"), "p")
    mode, weight = settings(data, normalized["q"] is not None)
    bankroll, cap, days = common(data)
    result: Json = {
        "schema_version": 1, "status": "ok", "mode": mode,
        "purpose": data.get("purpose", "decision"),
        "weight": weight, "p": p, "q": normalized["q"],
        "q_source": normalized["q_source"], "reminders": REMINDERS,
        "warnings": [],
    }
    if normalized["arbitrage"]:
        return {**result, "status": "arbitrage_out_of_scope",
                "reason": "Potential arbitrage: stop and verify costs/rules"}
    if p in (0, 1) and data.get("certainty_confirmed") is not True:
        return {**result, "status": "confirmation_required",
                "reason": "Confirm certainty; Kelly can imply all-in sizing"}
    adjusted = (weight * p + (1 - weight) * normalized["q"]
                if mode == "lambda" else p)
    result["p_adj"] = adjusted
    sides = []
    for quote in normalized["outcomes"]:
        yes = quote["name"] == "YES"
        original = p if yes else 1 - p
        chance = adjusted if yes else 1 - adjusted
        full = full_kelly(original, quote["b"])
        optimum = full_kelly(chance, quote["b"])
        candidate = optimum if mode == "lambda" else weight * full
        recommendation = max(0.0, candidate)
        fraction = min(cap, recommendation)
        baseline = normalized["q"]
        if baseline is not None and not yes:
            baseline = 1 - baseline
        equivalent = (mode == "lambda" and baseline is not None
                      and abs(quote["break_even"] - baseline) <= TOLERANCE)
        row = {
            **quote, "probability": chance, "full_fraction": full,
            "adjusted_full_fraction": optimum,
            "candidate_fraction": candidate,
            "uncapped_fraction": recommendation, "fraction": fraction,
            "cap_applied": recommendation > cap,
            "edge": chance - quote["break_even"],
            "growth": growth(chance, quote["b"], fraction),
            "growth_under_user_p": growth(original, quote["b"], fraction),
            "equivalent": equivalent,
            "scaled_full_fraction": weight * full,
            "equivalence_reason": (
                "fair_baseline_odds" if equivalent
                else "odds_do_not_match_baseline" if mode == "lambda"
                else "k_mode"),
        }
        if days is not None:
            row["daily_growth"] = row["growth"] / days
        sides.append(row)
    result["sides"] = sides
    if len(sides) == 1:
        result["warnings"].append(
            "Only one execution quote: other side unknown")
    eligible = [row for row in sides if row["fraction"] > TOLERANCE]
    if not eligible:
        return {**result, "status": "no_edge", "direction": "NONE",
                "reason": "No positive executable edge, or sizing/cap is zero"}
    if len(eligible) > 1:
        return {**result, "status": "arbitrage_out_of_scope",
                "reason": "Both sides positive: verify quotes and rules"}
    selected = dict(eligible[0])
    amount = bankroll * selected["fraction"]
    win = bankroll + amount * selected["b"]
    lose = bankroll - amount
    selected.update({
        "amount": amount, "full_amount": bankroll * max(
            0, selected["full_fraction"]),
        "uncapped_amount": bankroll * selected["uncapped_fraction"],
        "units": amount / selected["unit_cost"]
        if selected["unit_cost"] is not None else None,
        "wealth_if_yes": win if selected["name"] == "YES" else lose,
        "wealth_if_no": lose if selected["name"] == "YES" else win,
    })
    return {**result, "direction": selected["name"], "selected": selected,
            "reason": "Positive edge at the supplied executable net odds"}


def intake(data: Json) -> Json:
    """Check accumulated confirmed inputs without guessing or giving advice."""
    from kelly_intake import assess_inputs
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    return assess_inputs(data, data.get("operation", "single"))


def joint(data: Json) -> Json:
    """Delegate correlated-event sizing to the local numerical module."""
    from kelly_joint import size_joint
    return size_joint(data)


def calibrate(data: Json) -> Json:
    """Delegate historical calibration to the local statistical module."""
    from kelly_calibration import calibrate_records
    return calibrate_records(data)


def dispatch(command: str, data: Json) -> Json:
    """Dispatch one supported operation and reject non-object JSON input."""
    commands = {"intake": intake, "normalize": normalize, "single": single,
                "joint": joint, "calibrate": calibrate}
    if command not in commands:
        raise ValueError(
            "Unknown command: intake, normalize, single, joint, calibrate")
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    return commands[command](data)


class JsonParser(argparse.ArgumentParser):
    """Convert CLI usage errors into the same structured failure contract."""

    def error(self, message: str) -> None:
        """Raise ValueError instead of writing plain-text usage to stderr."""
        raise ValueError(message)


def run_cli() -> int:
    """Read a single JSON request and emit one JSON result; return 0 or 2."""
    try:
        parser = JsonParser(description=__doc__)
        parser.add_argument("command")
        parser.add_argument("--input", type=Path)
        args = parser.parse_args()
        raw = (args.input.read_text(encoding="utf-8-sig") if args.input
               else sys.stdin.read())
        result = dispatch(args.command, json.loads(raw))
        output = json.dumps(result, ensure_ascii=False, allow_nan=False,
                            indent=2)
    except (ValueError, TypeError, KeyError, OSError, ImportError,
            ArithmeticError, RuntimeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
