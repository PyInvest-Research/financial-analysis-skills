"""Check confirmed input fields before any binary-event result is produced.

This module validates structured data; it does not infer intent, prices,
costs, bankroll, or settlement facts from a conversation.
"""

from typing import Any

from kelly import Json, number, positive, probability


QUESTIONS = {
    "event": "Provide the missing event or settlement fields listed here.",
    "quotes": "Provide the missing executable quote or payout terms listed.",
    "fees": "Confirm the missing fees, or supply final all-in cost/net odds.",
    "probability_reason": "Provide your missing probability or its reason.",
    "bankroll": "What total bankroll is available for this type of decision?",
    "related_positions": "Are there existing or planned related positions?",
    "baseline": "Provide the benchmark/source, or explicitly choose k.",
    "joint": "Provide missing related events, held fractions, or scenarios.",
    "example": "Did you explicitly request a synthetic worked example?",
}


def field(prefix: str, name: str) -> str:
    """Prefix a field path without changing any supplied value."""
    return f"{prefix}.{name}" if prefix else name


def text_present(value: Any) -> bool:
    """Require actual nonempty text, not a truthy placeholder type."""
    return isinstance(value, str) and bool(value.strip())


def quote_missing(row: Json, prefix: str) -> dict[str, str]:
    """Find missing execution terms and reject malformed known values.

    Both raw fee models must be explicit, including explicit zero. Final
    net_odds already includes costs. A contract total_cost with
    fees_included=true also includes every cost and requires no fee guesses.
    """
    if not isinstance(row, dict):
        raise ValueError("Each outcome must be an object")
    missing: dict[str, str] = {}
    if row.get("name") is None:
        missing[field(prefix, "name")] = "quotes"
    elif row["name"] not in ("YES", "NO"):
        raise ValueError("Each outcome must have name YES or NO")
    kind = row.get("format")
    formats = {"contract", "decimal", "american", "stake", "net"}
    if kind is None:
        missing[field(prefix, "format")] = "quotes"
    elif not isinstance(kind, str) or kind not in formats:
        raise ValueError("Unknown outcome format")
    required = {
        "contract": [] if row.get("total_cost") is not None else ["price"],
        "decimal": ["odds"], "american": ["odds"],
        "stake": ["stake", "payout", "payout_kind"], "net": ["net_odds"],
    }.get(kind, [])
    for name in required:
        if row.get(name) is None:
            missing[field(prefix, name)] = "quotes"
    if row.get("payout_kind") not in (None, "net", "total"):
        raise ValueError("payout_kind must be net or total")
    if "fees_included" in row and type(row["fees_included"]) is not bool:
        raise ValueError("fees_included must be a boolean")
    included = row.get("fees_included") is True
    if included and "net_odds" not in row:
        if kind != "contract" or row.get("total_cost") is None:
            raise ValueError("All-in cost requires contract total_cost")
        if row.get("profit_commission", 0) != 0:
            raise ValueError(
                "All-in cost cannot add another profit commission")
    if row.get("net_odds") is None and not included:
        for name in ("purchase_fee", "profit_commission"):
            if name == "purchase_fee" and row.get("total_cost") is not None:
                continue
            if row.get(name) is None:
                missing[field(prefix, name)] = "fees"
    for name in ("price", "total_cost", "odds", "stake", "payout",
                 "net_odds", "purchase_fee", "profit_commission"):
        if row.get(name) is not None:
            number(row[name], field(prefix, name))
    return missing


def quotes_missing(data: Json, prefix: str = "") -> dict[str, str]:
    """Require executable quotes and never create an opposite side."""
    rows = data.get("outcomes")
    if rows is None or rows == []:
        return {field(prefix, "outcomes"): "quotes"}
    if not isinstance(rows, list) or len(rows) > 2:
        raise ValueError("outcomes must contain one or two binary quotes")
    missing: dict[str, str] = {}
    names = []
    for index, row in enumerate(rows):
        missing.update(quote_missing(row, field(prefix, f"outcomes[{index}]")))
        if row.get("name") is not None:
            names.append(row["name"])
    if len(names) != len(set(names)):
        raise ValueError("Duplicate outcome names")
    return missing


def event_missing(data: Json, prefix: str) -> dict[str, str]:
    """Require settlement facts and the user's own probability reason."""
    event = data.get("event")
    if event is None:
        event = {}
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    missing: dict[str, str] = {}
    for name in ("definition", "settlement_at", "resolver",
                 "cancellation_rule"):
        if not text_present(event.get(name)):
            missing[field(prefix, "event." + name)] = "event"
    if data.get("p") is None:
        missing[field(prefix, "p")] = "probability_reason"
    else:
        probability(data["p"], field(prefix, "p"))
    reason = data.get("probability_reason", event.get("probability_reason"))
    if not text_present(reason):
        missing[field(prefix, "probability_reason")] = "probability_reason"
    return missing


def baseline_missing(data: Json, mode: str, prefix: str) -> dict[str, str]:
    """Distinguish a sourced benchmark from execution odds and prices."""
    if data.get("q") is not None:
        probability(data["q"], field(prefix, "q"))
    benchmark = data.get("benchmark")
    if mode == "k" and data.get("q") is None and benchmark is None:
        return {}
    if data.get("q") is not None:
        if benchmark is not None:
            raise ValueError("Supply either q or a benchmark object")
        return {} if text_present(data.get("q_source")) else {
            field(prefix, "q_source"): "baseline"}
    if benchmark is None:
        return {field(prefix, "q"): "baseline"}
    if not isinstance(benchmark, dict):
        raise ValueError("benchmark must be an object")
    kind = benchmark.get("kind")
    required = {"midpoint": ["bid", "ask"], "external": ["value"],
                "devig": []}
    if not isinstance(kind, str) or kind not in required:
        raise ValueError("Unknown benchmark kind")
    missing: dict[str, str] = {}
    if kind == "devig":
        names = {row.get("name") for row in (data.get("outcomes") or [])}
        for name in ("YES", "NO"):
            if name not in names:
                missing[field(prefix, "outcomes." + name)] = "quotes"
    for name in required[kind]:
        value = benchmark.get(name)
        if value is None:
            missing[field(prefix, "benchmark." + name)] = "baseline"
        else:
            probability(value, field(prefix, "benchmark." + name))
    if not text_present(benchmark.get("source")):
        missing[field(prefix, "benchmark.source")] = "baseline"
    return missing


def numeric_settings(data: Json) -> str:
    """Reject malformed known numbers when other fields are missing."""
    mode = data.get("mode", "lambda")
    if mode not in ("lambda", "k"):
        raise ValueError("mode must be lambda or k")
    if (mode == "lambda" and "k" in data
            or mode == "k" and "lambda" in data):
        raise ValueError("Choose lambda or k, not both")
    for name in ("lambda", "k", "cap"):
        if data.get(name) is not None:
            probability(data[name], name)
    for name in ("bankroll", "days"):
        if data.get(name) is not None:
            positive(data[name], name)
    return mode


def position_missing(data: Json, operation: str,
                     events: list[Json]) -> dict[str, str]:
    """Require exposure disclosure and map held positions to events."""
    positions = data.get("related_positions")
    if positions is None:
        return {"related_positions": "related_positions"}
    if positions == "none":
        positions = []
    if (not isinstance(positions, list)
            or any(not text_present(name) for name in positions)):
        raise ValueError(
            "related_positions must be none or an event-name list")
    if operation == "single":
        return {"events": "joint"} if positions else {}
    names = [event.get("name") for event in events]
    missing = {"events": "joint"} if set(positions) - set(names) else {}
    for index, event in enumerate(events):
        held = event.get("existing_fraction")
        if held is None:
            missing[f"events[{index}].existing_fraction"] = "joint"
        else:
            probability(held, "existing_fraction")
            if held > 0 and event.get("name") not in positions:
                raise ValueError(
                    "Held event is missing from related_positions")
    return missing


def assess_inputs(data: Json, operation: str) -> Json:
    """Return a non-advisory waiting state or readiness without filling fields.

    Missing fields are grouped into at most two follow-up questions. Known
    invalid values still raise ValueError. Confirmation of facts and their
    origin remains the caller's responsibility, not an NLP task in Python.
    """
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    if operation not in ("single", "joint", "normalize"):
        raise ValueError(
            "intake operation must be single, joint, or normalize")
    mode = numeric_settings(data)
    missing: dict[str, str] = {}
    events = [data]
    if operation == "joint":
        events = data.get("events", [])
        if not isinstance(events, list) or len(events) > 3:
            raise ValueError("Joint mode supports one to three events")
        if any(not isinstance(event, dict) for event in events):
            raise ValueError("Each joint event must be an object")
        if not events:
            missing["events"] = "joint"
    for index, event in enumerate(events):
        prefix = f"events[{index}]" if operation == "joint" else ""
        missing.update(quotes_missing(event, prefix))
        if operation != "normalize":
            missing.update(event_missing(event, prefix))
        if (operation != "normalize" or event.get("q") is not None
                or event.get("benchmark") is not None):
            missing.update(baseline_missing(event, mode, prefix))
        if operation == "joint":
            if not text_present(event.get("name")):
                missing[field(prefix, "name")] = "joint"
            if event.get("side") not in ("YES", "NO"):
                missing[field(prefix, "side")] = "quotes"
            elif not any(row.get("name") == event["side"]
                         for row in (event.get("outcomes") or [])):
                missing[field(prefix, "outcomes")] = "quotes"
    if operation != "normalize":
        if data.get("bankroll") is None:
            missing["bankroll"] = "bankroll"
        if mode == "k" and data.get("k") is None:
            missing["k"] = "baseline"
        missing.update(position_missing(data, operation, events))
        purpose = data.get("purpose", "decision")
        if purpose not in ("decision", "example"):
            raise ValueError("purpose must be decision or example")
        if purpose == "example" and data.get("example_requested") is not True:
            missing["example_requested"] = "example"
        if (operation == "joint" and len(events) > 1
                and data.get("scenario_probabilities") is None
                and data.get("conditional_b_given_a") is None
                and data.get("independent") is not True):
            missing["scenario_probabilities"] = "joint"
    if missing:
        groups = set(missing.values())
        questions = [{"field": group, "prompt": prompt,
                      "missing_fields": [path for path in missing
                                         if missing[path] == group]}
                     for group, prompt in QUESTIONS.items() if group in groups]
        return {"schema_version": 1, "status": "awaiting_input",
                "operation": operation, "missing_fields": list(missing),
                "questions": questions[:2]}
    # Reuse numerical validation after checking presence. No investment
    # figures are exposed by this readiness result.
    from kelly import normalize, normalized_outcome
    for event in events:
        if operation == "normalize":
            for row in event["outcomes"]:
                normalized_outcome(row)
        else:
            normalize(event)
    result: Json = {"schema_version": 1, "status": "ready",
                    "operation": operation, "missing_fields": [],
                    "questions": []}
    if operation != "joint":
        names = [row["name"] for row in data["outcomes"]]
        result.update(sides_evaluable=names,
                      sides_unknown=[name for name in ("YES", "NO")
                                     if name not in names])
    return result
