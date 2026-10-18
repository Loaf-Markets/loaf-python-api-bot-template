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
    # api_key / l2_secret_key / base_url come from $LOAF_API_KEY /
    # $LOAF_L2_SECRET_KEY / $LOAF_API_BASE_URL when omitted.
    with LoafClient() as client:
        print(f"Talking to {client.base_url}")

        # portfolio.component is authenticated, so it doubles as a credentials check.
        comp = client.portfolio.component()
        # The L2 book: amounts are base units as decimal strings (USDC has 6 decimals).
        print("\nBalances (available / total):")
        for row in comp.get("balances") or []:
            scale = 10 ** row.decimals
            cash = "  (cash)" if row.isQuote else ""
            print(f"  {row.symbol:<8} {int(row.available) / scale:>16,.6f} / {int(row.total) / scale:,.6f}{cash}")
        print(f"Open positions     : {len(comp.get('positions') or [])}")
        print(f"Lifetime volume    : {comp.get('lifetimeVolume', 0):,.2f}")
        print(f"L2 trading key     : {client.l2_public_key or 'not set (orders disabled)'}")
        print(f"Max slippage       : {client.max_slippage_bps} bps (100 bps = 1%)")
        print(f"Listed properties  : {len(client.market.properties().get('properties') or [])}")


if __name__ == "__main__":
    try:
        main()
    except loaf.LoafConfigError as exc:
        raise SystemExit(str(exc))
    except loaf.LoafAuthError as exc:
        raise SystemExit(f"Auth failed: {exc.message}")
