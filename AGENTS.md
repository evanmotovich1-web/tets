# Tets

Standalone private algo test runner. Preserve the boundary: Yahoo virtual shadow fills or Alpaca paper endpoint only. No OpenTerminal dependency. No real-money mode.

Never commit state, keys, OAuth tokens, environments or downloaded repositories. Runtime data lives in gitignored `desk/runtime/.state/`. Keys are entered locally with `sh tets connect`, never through chat or source code.

Run `desk/runtime/.state/shadow/.venv/bin/python -m unittest discover -s scripts/tests` and `sh tets self-test`. Both installation profiles must remain available. Do not claim physical 2017 MacBook verification from an Apple Silicon test.
