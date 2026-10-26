"""01 — Quickstart: confirm credentials and read balances + market.

    python examples/01_quickstart.py
"""

from __future__ import annotations

from decimal import Decimal

import loaf
from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    # api_key / secret_key / base_url come from $LOAF_API_KEY /
    # $LOAF_SECRET_KEY / $LOAF_API_BASE_URL when omitted.
    with LoafClient() as client:
        print(f"Talking to {client.base_url}")

        # portfolio.component is authenticated, so it doubles as a credentials check.
        comp = client.portfolio.component()
        balances = comp.get("balances") or []
        # Amounts are base-unit strings; the isQuote rows are your cash.
        cash = sum(
            (loaf.from_base_units(b.available, b.decimals) for b in balances if b.isQuote),
            Decimal(0),
        )
        print(f"\nCash available     : {cash:,.2f}")
        print(f"Balances rows      : {len(balances)}")
        print(f"Holdings           : {len(comp.get('holdings') or [])}")
        print(f"Trading key        : {client.public_key or 'not set (orders disabled)'}")
        print(f"Max slippage       : {client.max_slippage_bps} bps (100 bps = 1%)")
        print(f"Listed markets     : {len(client.market.list().get('markets') or [])}")


if __name__ == "__main__":
    try:
        main()
    except loaf.LoafConfigError as exc:
        raise SystemExit(str(exc))
    except loaf.LoafAuthError as exc:
        raise SystemExit(f"Auth failed: {exc.message}")
