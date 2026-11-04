"""Optimize up to three correlated binary positions with explicit scenarios."""

from itertools import product
from typing import Any

import numpy as np
from scipy.optimize import minimize

from kelly import (EPSILON, REMINDERS, TOLERANCE, common, full_kelly,
                   normalize, probability, settings)


Json = dict[str, Any]


def distribution(values: Any, count: int, name: str) -> np.ndarray:
    """Validate and normalize only floating-point summation tolerance."""
    if not isinstance(values, list) or len(values) != 2 ** count:
        raise ValueError(f"{name} requires {2 ** count} probabilities")
    result = np.array([probability(value, name) for value in values])
    if abs(float(result.sum()) - 1) > EPSILON:
        raise ValueError(f"{name} probabilities must sum to 1 within 1e-6")
    return result / result.sum()


def independent(marginals: np.ndarray, states: np.ndarray) -> np.ndarray:
    """Build probabilities of ordered YES-state bit tuples."""
    return np.prod(np.where(states == 1, marginals, 1 - marginals), axis=1)


def user_distribution(data: Json, marginals: np.ndarray,
                      states: np.ndarray) -> np.ndarray:
    """Resolve an explicit table, a two-event conditional, or independence."""
    count = len(marginals)
    modes = sum(("scenario_probabilities" in data,
                 "conditional_b_given_a" in data,
                 data.get("independent") is True))
    if modes > 1 or (modes != 1 and count > 1):
        raise ValueError(
            "Choose one joint table, conditional, or independence")
    if "scenario_probabilities" in data:
        probabilities = distribution(data["scenario_probabilities"], count,
                                     "scenario_probabilities")
    elif "conditional_b_given_a" in data:
        if count != 2:
            raise ValueError("P(B|A) is available only for two events")
        conditional = probability(data["conditional_b_given_a"], "P(B|A)")
        pa, pb = marginals
        both = pa * conditional
        values = [1 - pa - pb + both, pb - both, pa - both, both]
        if any(value < -TOLERANCE for value in values):
            raise ValueError("P(A), P(B), P(B|A) imply negative scenario mass")
        probabilities = distribution([max(0, float(v)) for v in values],
                                     count, "conditional scenarios")
    elif count == 1 or data.get("independent") is True:
        probabilities = independent(marginals, states)
    else:
        raise ValueError("Provide joint scenario probabilities")
    if not np.allclose(probabilities @ states, marginals, rtol=0,
                       atol=EPSILON):
        raise ValueError("Scenario marginals do not match event p values")
    return probabilities


def optimize(probabilities: np.ndarray, returns: np.ndarray,
             caps: np.ndarray, held: np.ndarray) -> np.ndarray:
    """Maximize concave log wealth with analytic gradients and cash reserve.

    held is fixed exposure as a fraction of current bankroll. Returned
    weights are additional nonnegative stakes. Raise RuntimeError if the
    optimizer fails; never return an unverified numerical iterate.
    """
    budget = 1 - EPSILON - float(held.sum())
    upper = np.maximum(0, np.minimum(caps - held, budget))
    if budget <= TOLERANCE or upper.max() <= TOLERANCE:
        return np.zeros(len(caps))
    if len(caps) == 1:
        chance = float(probabilities[returns[:, 0] > 0].sum())
        b = float(returns[returns[:, 0] > 0, 0][0])
        value = full_kelly(chance, b) - held[0]
        return np.array([min(upper[0], max(0, value))])

    def objective(weights: np.ndarray) -> float:
        """Return negative expected log growth for a feasible allocation."""
        wealth = 1 + returns @ (held + weights)
        if np.any(wealth <= 0):
            return float("inf")
        return -float(probabilities @ np.log(wealth))

    def gradient(weights: np.ndarray) -> np.ndarray:
        """Return the analytic derivative of negative expected log growth."""
        wealth = 1 + returns @ (held + weights)
        return -(returns.T @ (probabilities / wealth))

    result = minimize(
        objective, np.zeros(len(caps)), jac=gradient, method="SLSQP",
        bounds=list(zip(np.zeros(len(caps)), upper)),
        constraints=[{"type": "ineq",
                      "fun": lambda w: budget - float(w.sum()),
                      "jac": lambda w: -np.ones(len(w))}],
        options={"ftol": 1e-13, "maxiter": 2000},
    )
    weights = np.clip(result.x, 0, upper)
    if (not result.success or not np.all(np.isfinite(weights))
            or float(weights.sum()) > budget + 1e-9
            or np.any(1 + returns @ (held + weights) <= 0)):
        raise RuntimeError(f"Joint optimizer failed: {result.message}")
    if objective(weights) > objective(np.zeros(len(caps))) + 1e-10:
        raise RuntimeError(
            "Optimizer returned a worse allocation than no trade")
    return weights


def size_joint(data: Json) -> Json:
    """Size selected sides using correlated scenarios and optional holdings.

    Each event supplies p, a baseline, a selected side, and executable quotes.
    Existing positions are fixed long exposures; k mode with holdings is
    rejected because scaling the whole portfolio would sell fixed holdings.
    """
    from kelly_intake import assess_inputs
    ready = assess_inputs(data, "joint")
    if ready["status"] != "ready":
        return ready
    events = data.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= 3:
        raise ValueError("Joint mode supports one to three binary events")
    if any(not isinstance(event, dict) for event in events):
        raise ValueError("Each event must be an object")
    names = [event.get("name") for event in events]
    if (any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("Event names must be nonempty and unique")
    bankroll, cap, days = common(data)
    normalized = [normalize(event) for event in events]
    mode, weight = settings(data, all(row["q"] is not None
                                      for row in normalized))
    base: Json = {"schema_version": 1, "mode": mode, "weight": weight,
                  "purpose": data.get("purpose", "decision"),
                  "reminders": REMINDERS}
    if any(row["arbitrage"] for row in normalized):
        return {**base, "status": "arbitrage_out_of_scope"}
    p = np.array([probability(event.get("p"), "event p")
                  for event in events])
    if (any(value in (0, 1) for value in p)
            and data.get("certainty_confirmed") is not True):
        return {**base, "status": "confirmation_required",
                "reason": "Confirm event certainty before sizing"}
    quotes = []
    for event, row in zip(events, normalized):
        quote = next((q for q in row["outcomes"]
                      if q["name"] == event.get("side")), None)
        if quote is None:
            raise ValueError("Each selected side requires its execution quote")
        quotes.append(quote)
    count = len(events)
    states = np.array(list(product((0, 1), repeat=count)))
    user = user_distribution(data, p, states)
    q_values = [row["q"] for row in normalized]
    benchmark = None
    if all(value is not None for value in q_values):
        q = np.array(q_values, dtype=float)
        if "benchmark_probabilities" in data:
            benchmark = distribution(data["benchmark_probabilities"], count,
                                     "benchmark_probabilities")
            if not np.allclose(benchmark @ states, q, rtol=0, atol=EPSILON):
                raise ValueError("Benchmark marginals do not match event q")
        else:
            benchmark = independent(q, states)
    elif "benchmark_probabilities" in data:
        raise ValueError("A joint benchmark also requires all marginal q")
    adjusted = (weight * user + (1 - weight) * benchmark
                if mode == "lambda" else user)
    yes_sides = np.array([event["side"] == "YES" for event in events])
    wins = states == yes_sides
    odds = np.array([row["b"] for row in quotes])
    returns = np.where(wins, odds, -1)
    held = np.array([probability(event.get("existing_fraction", 0),
                                 "existing_fraction") for event in events])
    caps = np.array([probability(event.get("cap", cap), "event cap")
                     for event in events])
    if float(held.sum()) > 1 - EPSILON or np.any(held > caps):
        raise ValueError("Existing holdings exceed the cash reserve or caps")
    if mode == "k" and np.any(held > 0):
        raise ValueError("Use lambda mode for fixed existing positions")
    raw = optimize(user, returns, np.ones(count), held)
    uncapped = (optimize(adjusted, returns, np.ones(count), held)
                if mode == "lambda" else weight * raw)
    fractions = (optimize(adjusted, returns, caps, held)
                 if mode == "lambda" else np.minimum(uncapped, caps))
    fractions[fractions < TOLERANCE] = 0
    total = held + fractions
    if not np.any(fractions > TOLERANCE):
        return {**base, "status": "no_edge", "direction": "NONE",
                "reason": "No positive additional position under constraints",
                "fractions": fractions.tolist(),
                "existing_fractions": held.tolist()}
    scenario_wealth = 1 + returns @ total
    expected_growth = float(adjusted @ np.log(scenario_wealth))
    positions = []
    for i, (event, quote) in enumerate(zip(events, quotes)):
        chance = float(adjusted @ wins[:, i])
        amount = bankroll * float(fractions[i])
        positions.append({
            "name": event["name"], "side": event["side"], "b": quote["b"],
            "break_even": quote["break_even"], "probability": chance,
            "edge": chance - quote["break_even"],
            "full_fraction": float(raw[i]),
            "uncapped_fraction": float(uncapped[i]),
            "fraction": float(fractions[i]), "amount": amount,
            "existing_fraction": float(held[i]),
            "total_fraction": float(total[i]),
            "cap_applied": bool(uncapped[i] + held[i] > caps[i] + 1e-9),
            "units": amount / quote["unit_cost"]
            if quote["unit_cost"] is not None else None,
        })
    return {
        **base, "status": "ok", "fractions": fractions.tolist(),
        "full_fractions": raw.tolist(), "total_fractions": total.tolist(),
        "positions": positions, "growth": expected_growth,
        "incremental_growth": expected_growth - float(
            adjusted @ np.log(1 + returns @ held)),
        "daily_growth": expected_growth / days if days is not None else None,
        "scenarios": [
            {"yes_state": state.tolist(), "probability_user": float(user[i]),
             "probability_benchmark": float(benchmark[i])
             if benchmark is not None else None,
             "probability": float(adjusted[i]),
             "wealth": bankroll * float(scenario_wealth[i])}
            for i, state in enumerate(states)
        ],
        "benchmark_assumption": "user_joint_table"
        if "benchmark_probabilities" in data else "independent_marginals",
        "cash_reserve": EPSILON,
        "warnings": ["Joint sizing assumes all payoffs settle together"],
    }
