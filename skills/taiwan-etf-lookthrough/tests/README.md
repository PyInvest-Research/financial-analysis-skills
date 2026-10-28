# ETF regression tests

Run from the skill directory with its own prepared virtual environment:

    .venv/bin/python -m unittest discover -s tests

No network, user portfolio, GPU or fresh dependency installation is required.
The core tests and small fixtures come from the existing PyInvest ETF suite.
`test_run.py` explicitly selects strict mode to preserve its synthetic ticker
and source-conflict contract. New annotated cases use numeric Taiwan tickers.
The publisher CLI validation-error case now uses an out-of-range weight,
since future holdings dates are intentionally warnings in annotated mode.

`test_estimation_first.py` covers the new behavior using synthetic inputs;
its BIFF fixture is constructed from small binary records in memory. The
Fubon HTML fixture is an existing public official-response regression sample.
Historical demo and bundled research inputs are retained byte-for-byte.
