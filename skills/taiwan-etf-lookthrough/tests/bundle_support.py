"""Write synthetic bundles with independently calculated content hashes."""

import hashlib
import json
from pathlib import Path
import tempfile
from unittest import TestCase


def write_bundle(root: Path, payloads: list[dict]) -> Path:
    """Save source-shaped snapshots and a content-bound bundle index."""
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for payload in payloads:
        code = payload['etf_symbol']
        fields = {k: v for k, v in payload.items() if k != 'fetched_at'}
        sha = hashlib.sha256(json.dumps(
            fields, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        rows.append({'symbol': code, 'holdings_date': payload['holdings_date'],
                     'snapshot_sha256': sha,
                     'source_kind': payload['source_kind']})
        (root / f'{code}.json').write_text(json.dumps(payload))
    (root / 'index.json').write_text(json.dumps({
        'schema_version': '1', 'generated_at': '2026-11-02T00:00:00Z',
        'source': 'gcs:etf-holdings/v1', 'etfs': rows}))
    return root


def bundle_from_public(owner: TestCase, source: Path) -> Path:
    """Package real test publisher output without invoking a transport."""
    temporary = tempfile.TemporaryDirectory()
    owner.addCleanup(temporary.cleanup)
    return write_bundle(Path(temporary.name).resolve(), [
        json.loads(path.read_bytes())
        for path in sorted(source.glob('*/latest.json'))])
