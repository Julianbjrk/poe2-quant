#!/usr/bin/env python3
"""QUANT v1.0 — read-only PoE2 currency decision-support. Stdlib only.

The run command never changes:
    python quant.py            serves http://localhost:8377
    python quant.py --doctor   API + database + model health check
    python quant.py --once     one dry-run poll, printed as JSON
    python quant.py --backtest walk-forward replay of your tick history
    python quant.py --host 0.0.0.0   LAN mode (token printed at startup)

The code lives in the quant/ package; v0.4's single file is in git history.
"""
from quant.main import main

if __name__ == "__main__":
    main()
