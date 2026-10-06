"""01 — Quickstart: confirm credentials and read balances + market.

    python examples/01_quickstart.py
"""

from __future__ import annotations

import loaf
from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    # api_key / agent_private_key / base_url come from $LOAF_API_KEY /
    # $LOAF_AGENT_PRIVATE_KEY / $LOAF_API_BASE_URL when omitted.
    with LoafClient() as client:
        print(f"Talking to {client.base_url}")

        # portfolio.component is authenticated, so it doubles as a credentials check.
        comp = client.portfolio.component()
        print(f"\nCash {comp.cash:,.2f} USDC  |  portfolio {comp.portfolioValue:,.2f}  "
              f"|  PnL {comp.portfolioPnl:,.2f} ({comp.portfolioPnlPercent:.2f}%)")
        print(f"Open positions     : {len(comp.get('positions') or [])}")
        print(f"Signing agent      : {client.agent_address or 'not set (orders disabled)'}")
        print(f"Max slippage       : {client.max_slippage_bps} bps (100 bps = 1%)")
        print(f"Listed properties  : {len(client.market.properties().get('properties') or [])}")


if __name__ == "__main__":
    try:
        main()
    except loaf.LoafConfigError as exc:
        raise SystemExit(str(exc))
    except loaf.LoafAuthError as exc:
        raise SystemExit(f"Auth failed: {exc.message}")
