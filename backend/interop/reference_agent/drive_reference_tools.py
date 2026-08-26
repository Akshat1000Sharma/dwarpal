#!/usr/bin/env python3
"""Drive the five reference-agent tools over a real MCP session, end to end.

The upstream shopping agent needs `uv`, the Google ADK and a Google API key. This script needs none
of them: it speaks MCP to the same adapter the agent spawns, calls the same five tools in the same
order, and ends in a settled Dwarpal checkout with an evidence packet behind it.

What it therefore proves, and what it does not. It proves the adapter is a working merchant over
MCP: the tools exist with the expected names, they answer from the real catalog, the quote holds
real stock, and `complete_checkout` drives the real verification pipeline to a real verdict. It
does not prove the upstream agent's own credential shapes satisfy Dwarpal, because the credentials
here are minted by Dwarpal's own mocked credential provider. That claim needs the upstream agent
itself, and the README says which of the two has actually been run.

    python interop/reference_agent/drive_reference_tools.py
    python interop/reference_agent/drive_reference_tools.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.harness import factory  # noqa: E402
from app.trust.registry import publish_key  # noqa: E402

TEA = "DWP-TEA-001"


class Report:
    def __init__(self) -> None:
        self.steps: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append((name, bool(ok), detail))
        mark = "ok" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f": {detail}" if detail else ""))
        return bool(ok)

    @property
    def failed(self) -> int:
        return sum(1 for _n, ok, _d in self.steps if not ok)


def _content(result: Any) -> dict[str, Any]:
    """MCP tool results arrive as content blocks; the adapter always returns one JSON object."""
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {}


async def drive(base: str, temp_db: Path) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    report = Report()
    print(f"Driving the AP2 reference-agent tool surface against {base}\n")

    environment = os.environ.copy()
    environment["DWARPAL_BASE_URL"] = base
    environment["TEMP_DB_DIR"] = str(temp_db)
    environment["LOGS_DIR"] = str(temp_db)

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "interop.reference_agent.merchant_mcp_server"],
        cwd=str(BACKEND_ROOT),
        env=environment,
    )

    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        expected = {
            "search_inventory",
            "check_product",
            "assemble_cart",
            "create_checkout",
            "complete_checkout",
        }
        report.add(
            "the five tools the reference agent calls are served",
            expected <= names,
            ", ".join(sorted(names)),
        )

        found = _content(
            await session.call_tool("search_inventory", {"product_description": "tea"})
        )
        matches = found.get("matches") or []
        report.add(
            "search_inventory answers from the real catalog",
            any(m["item_id"] == TEA for m in matches),
            f"{len(matches)} match(es)",
        )

        checked = _content(await session.call_tool("check_product", {"item_id": TEA}))
        report.add(
            "check_product reports live stock and price",
            checked.get("available") is True and checked.get("stock", 0) > 0,
            f"{checked.get('stock')} in stock at {checked.get('price')} {checked.get('currency')}",
        )
        report.add(
            "the item carries machine-readable purchase constraints",
            {"min_order_quantity", "returnable", "age_restricted"}
            <= set(checked.get("purchase_constraints") or {}),
        )

        cart = _content(await session.call_tool("assemble_cart", {"item_id": TEA, "qty": 2}))
        cart_id = cart.get("cart_id")
        report.add("assemble_cart quotes and holds real stock", bool(cart_id), str(cart_id))
        if not cart_id:
            return 1

        checkout = _content(
            await session.call_tool(
                "create_checkout",
                {"cart_id": cart_id, "open_checkout_mandate_id": "open-checkout"},
            )
        )
        report.add(
            "create_checkout returns the merchant's own signed Checkout",
            bool(checkout.get("checkout_jwt")) and bool(checkout.get("checkout_jwt_hash")),
        )

        # Play the credential provider the upstream agent would have, writing the mandate chains
        # into the shared store exactly where the adapter looks for them.
        principals = factory.Principals.create(
            agent_id="agent:ap2-reference-shopping-agent", register=False
        )
        publish_key(principals.issuer_id, principals.issuer.public_jwk())
        presentation = factory.present(
            principals,
            factory.spec_for_cart([(TEA, "Nilgiri Black Tea 250g", 2)]),
            checkout_jwt=checkout["checkout_jwt"],
            checkout_hash=checkout["checkout_jwt_hash"],
            amount_minor=round(float(cart["total"]) * 100),
            audience=_audience(base),
            nonce="reference-agent-nonce-1",
        )
        credentials = presentation.credentials
        temp_db.mkdir(parents=True, exist_ok=True)
        (temp_db / "open-checkout.sdjwt").write_text(credentials.open_checkout, encoding="ascii")
        (temp_db / "chk_reference.sdjwt").write_text(credentials.closed_checkout, encoding="ascii")
        (temp_db / "tok_reference_open.sdjwt").write_text(
            credentials.open_payment or "", encoding="ascii"
        )
        (temp_db / "tok_reference.sdjwt").write_text(
            credentials.closed_payment or "", encoding="ascii"
        )
        (temp_db / "ap2_token_store.json").write_text(
            json.dumps(
                {
                    "tok_reference": {
                        "payment_mandate_id": "tok_reference",
                        "open_payment_mandate_id": "tok_reference_open",
                        "used": False,
                    }
                }
            ),
            encoding="utf-8",
        )

        settled = _content(
            await session.call_tool(
                "complete_checkout",
                {
                    "payment_token": "tok_reference",
                    "checkout_mandate_id": "chk_reference",
                    "checkout_nonce": "reference-agent-nonce-1",
                    "open_checkout_mandate_id": "open-checkout",
                },
            )
        )
        report.add(
            "complete_checkout drives the real verification pipeline to a verdict",
            settled.get("status") == "Success",
            f"{settled.get('status')} {settled.get('reason_code')}",
        )
        report.add(
            "the purchase is filed as evidence",
            bool(settled.get("evidence_packet_id")),
            str(settled.get("evidence_packet_id")),
        )

        refused = _content(
            await session.call_tool(
                "complete_checkout",
                {
                    "payment_token": "tok_reference",
                    "checkout_mandate_id": "chk_reference",
                    "checkout_nonce": "reference-agent-nonce-1",
                    "open_checkout_mandate_id": "open-checkout",
                },
            )
        )
        report.add(
            "a replayed chain is refused with a reason code, not smoothed into a success",
            refused.get("status") == "Error" and bool(refused.get("reason_code")),
            f"{refused.get('reason_code')} action={refused.get('action')}",
        )

    print()
    passed = len(report.steps) - report.failed
    print(f"{passed}/{len(report.steps)} checks passed")
    return 0 if report.failed == 0 else 1


def _audience(base: str) -> str:
    """The audience the merchant publishes, which is not always the URL we dialled."""
    import urllib.request

    discovery = base.rstrip("/") + "/.well-known/ap2-merchant"
    try:
        with urllib.request.urlopen(discovery, timeout=30) as response:
            return json.loads(response.read()).get("audience") or base
    except Exception:
        return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="the merchant's origin")
    parser.add_argument("--temp-db", default="", help="the shared mandate store")
    args = parser.parse_args(argv)

    temp_db = Path(args.temp_db) if args.temp_db else Path(tempfile.mkdtemp(prefix="dwarpal-ref-"))
    return asyncio.run(drive(args.base, temp_db))


if __name__ == "__main__":
    sys.exit(main())
