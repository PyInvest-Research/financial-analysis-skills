"""Protect snapshot JSON interchange and deterministic content hashes."""

from dataclasses import replace
from datetime import date
import json
import unittest

from support import make_snapshot
from lookthrough.models import (
    Holding, SchemaError, snapshot_from_dict, snapshot_sha256,
    snapshot_to_dict,
)


class SnapshotModelTests(unittest.TestCase):
    """Exercise actual model conversion with only synthetic holdings."""

    def test_round_trip_preserves_snapshot(self) -> None:
        """JSON conversion preserves every field and holding order."""
        snapshot = make_snapshot()
        restored = snapshot_from_dict(snapshot_to_dict(snapshot))
        self.assertEqual(restored, snapshot)

    def test_serialization_is_json_ready(self) -> None:
        """Dates use ISO text and holdings become an ordered JSON array."""
        payload = snapshot_to_dict(make_snapshot())
        self.assertEqual(payload["holdings_date"], "2026-10-22")
        self.assertEqual(payload["holdings"], [
            {"code": "2330", "name": "台積電", "kind": "stock",
             "weight": 0.6, "exposure_weight": None},
            {"code": "2317", "name": "鴻海", "kind": "stock",
             "weight": 0.2, "exposure_weight": None},
            {"code": "2454", "name": "聯發科", "kind": "stock",
             "weight": 0.15, "exposure_weight": None},
            {"code": "TXFK6", "name": "臺股期貨", "kind": "futures",
             "weight": None, "exposure_weight": 0.03},
            {"code": "TWD", "name": "現金", "kind": "cash",
             "weight": 0.04, "exposure_weight": None},
        ])
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_unknown_schema_version_rejected(self) -> None:
        """Unsupported or non-string versions cannot silently become v1."""
        for version in ("2", "", 1, None):
            with self.subTest(version=version):
                payload = snapshot_to_dict(make_snapshot())
                payload["schema_version"] = version
                with self.assertRaises(SchemaError):
                    snapshot_from_dict(payload)

    def test_hash_is_order_independent_of_dict_keys(self) -> None:
        """Object key order must not alter the content identity."""
        snapshot = make_snapshot()
        payload = dict(reversed(snapshot_to_dict(snapshot).items()))
        payload["holdings"] = [
            dict(reversed(row.items())) for row in payload["holdings"]
        ]
        self.assertEqual(snapshot_sha256(snapshot),
                         snapshot_sha256(snapshot_from_dict(payload)))

    def test_hash_matches_canonical_utf8_json(self) -> None:
        """The digest uses compact sorted JSON without ASCII escaping."""
        self.assertEqual(
            snapshot_sha256(make_snapshot()),
            "ee1d26392944f8c2d29b90dbbbdf46357152196225bfaf0cd79650148284b488",
        )

    def test_hash_includes_holding_order_and_metadata(self) -> None:
        """A different holding sequence or source timestamp has new bytes."""
        snapshot = make_snapshot()
        variants = (
            replace(snapshot, holdings=tuple(reversed(snapshot.holdings))),
            replace(snapshot, fetched_at="2026-10-24T00:00:00+00:00"),
            replace(snapshot, holdings_date=date(2026, 10, 21)),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(snapshot_sha256(snapshot),
                                    snapshot_sha256(variant))

    def test_empty_holdings_round_trip(self) -> None:
        """An empty snapshot stays empty rather than acquiring fake rows."""
        snapshot = replace(make_snapshot(), holdings=())
        payload = snapshot_to_dict(snapshot)
        self.assertEqual(payload["holdings"], [])
        self.assertEqual(snapshot_from_dict(payload), snapshot)

    def test_zero_and_missing_weights_remain_distinct(self) -> None:
        """Unknown futures exposures remain null, while real zero survives."""
        snapshot = replace(make_snapshot(), holdings=(
            Holding("ZERO", "Zero", "stock", 0.0),
            Holding("TX", "Unknown", "futures", None),
        ))
        restored = snapshot_from_dict(snapshot_to_dict(snapshot))
        self.assertEqual(restored.holdings[0].weight, 0.0)
        self.assertIsNone(restored.holdings[1].weight)
        self.assertIsNone(restored.holdings[1].exposure_weight)

    def test_optional_exposure_weight_can_be_absent(self) -> None:
        """Rows can omit the optional notional exposure field."""
        payload = snapshot_to_dict(make_snapshot())
        del payload["holdings"][0]["exposure_weight"]
        self.assertEqual(snapshot_from_dict(payload), make_snapshot())

    def test_conversion_does_not_share_mutable_holdings(self) -> None:
        """Editing a JSON row cannot mutate the decoded or original model."""
        snapshot = make_snapshot()
        payload = snapshot_to_dict(snapshot)
        restored = snapshot_from_dict(payload)
        payload["holdings"][0]["weight"] = 0.1
        self.assertEqual(snapshot.holdings[0].weight, 0.6)
        self.assertEqual(restored.holdings[0].weight, 0.6)

    def test_invalid_calendar_date_rejected(self) -> None:
        """Impossible dates do not produce a fabricated holdings date."""
        payload = snapshot_to_dict(make_snapshot())
        payload["holdings_date"] = "2026-02-30"
        with self.assertRaises(SchemaError):
            snapshot_from_dict(payload)

    def test_bad_snapshot_structure_has_stable_schema_error(self) -> None:
        """Malformed JSON objects fail before becoming typed snapshots."""
        variants = [None, [], "snapshot", 1]
        for field in snapshot_to_dict(make_snapshot()):
            missing = snapshot_to_dict(make_snapshot())
            del missing[field]
            variants.append(missing)
        variants.append(snapshot_to_dict(make_snapshot()) | {"extra": 1})
        for payload in variants:
            with self.subTest(payload=payload):
                with self.assertRaises(SchemaError):
                    snapshot_from_dict(payload)

    def test_bad_snapshot_field_types_are_rejected(self) -> None:
        """Do not coerce identifiers, activity flags or provenance fields."""
        variants = (
            ("etf_symbol", 50), ("issuer", None), ("is_active", "false"),
            ("is_active", 0), ("source_url", []), ("source_kind", "broker"),
            ("source_kind", None), ("holdings", {}), ("holdings", None),
        )
        for key, value in variants:
            with self.subTest(key=key, value=value):
                payload = snapshot_to_dict(make_snapshot()) | {key: value}
                with self.assertRaises(SchemaError):
                    snapshot_from_dict(payload)

    def test_date_and_timestamp_structure_are_rejected(self) -> None:
        """Dates are extended ISO and fetch timestamps have UTC offsets."""
        variants = (
            ("holdings_date", "20261022"), ("holdings_date", None),
            ("holdings_date", "2026-10-22T00:00:00"),
            ("fetched_at", "2026-10-23"), ("fetched_at", 123),
            ("fetched_at", "2026-10-23T00:00:00"),
            ("fetched_at", "2026-10-23T08:00:00+08:00"),
        )
        for key, value in variants:
            with self.subTest(key=key, value=value):
                payload = snapshot_to_dict(make_snapshot()) | {key: value}
                with self.assertRaises(SchemaError):
                    snapshot_from_dict(payload)

    def test_bad_holding_structure_and_enums_are_rejected(self) -> None:
        """A row must match the holding schema, including required weight."""
        variants = [None, [], "2330", {},
                    {"code": "2330", "name": "Stock", "kind": "stock"}]
        row = snapshot_to_dict(make_snapshot())["holdings"][0]
        variants.extend(row | {key: value} for key, value in (
            ("kind", "etf"), ("code", 2330), ("name", None), ("extra", 1),
        ))
        for holding in variants:
            with self.subTest(holding=holding):
                payload = snapshot_to_dict(make_snapshot())
                payload["holdings"] = [holding]
                with self.assertRaises(SchemaError):
                    snapshot_from_dict(payload)

    def test_nonfinite_and_non_numeric_weights_are_rejected(self) -> None:
        """Reject invalid numbers without converting strings or booleans."""
        for field in ("weight", "exposure_weight"):
            for value in (float("nan"), float("inf"), -float("inf"),
                          "0.6", True, [], {}):
                with self.subTest(field=field, value=value):
                    payload = snapshot_to_dict(make_snapshot())
                    payload["holdings"][0][field] = value
                    with self.assertRaises(SchemaError):
                        snapshot_from_dict(payload)

    def test_integer_weights_and_z_timestamp_remain_supported(self) -> None:
        """Valid JSON numeric zeros and UTC Z timestamps need no coercion."""
        payload = snapshot_to_dict(make_snapshot())
        payload["holdings"][0]["weight"] = 0
        payload["fetched_at"] = "2026-10-23T00:00:00Z"
        restored = snapshot_from_dict(payload)
        self.assertEqual(restored.holdings[0].weight, 0)
        self.assertEqual(restored.fetched_at, "2026-10-23T00:00:00Z")
