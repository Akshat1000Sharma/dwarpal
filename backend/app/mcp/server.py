"""MCP server exposing the merchant's catalog.

An arriving agent can enumerate the offering conversationally, read the machine-readable purchase
constraints on every item, fetch the signed policy terms, and obtain a quote, without a human in
the loop and without reading the HTTP API documentation first.

This is also the surface the upstream AP2 reference shopping agent points at. It is deliberately
read-and-quote only: completing a checkout requires presenting the credential chain, which goes
through the HTTP endpoint where the full verification pipeline runs.

    python -m app.mcp.server              stdio, for an MCP client that spawns the process
    python -m app.mcp.server --http       streamable HTTP on MCP_PORT
"""

# No postponed annotations here: FastMCP inspects the real annotation objects when it builds each
# tool's schema, and would see plain strings instead.
import argparse
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.catalog import discovery, policy_terms
from app.catalog import service as catalog
from app.checkout.quote import QuoteError, RequestedLine, create_quote, quote_document
from app.correlation import new_correlation_id
from app.db.base import session_scope
from app.settings import settings

mcp = FastMCP("dwarpal-merchant")


@mcp.tool()
def merchant_profile() -> str:
    """Describe this merchant: protocols spoken, credentials accepted, and trust anchors."""
    with session_scope() as session:
        return json.dumps(discovery.discovery_document(session), indent=2)


@mcp.tool()
def browse_catalog(category: str | None = None, limit: int = 25, offset: int = 0) -> str:
    """List catalog items with live availability and their machine-readable purchase constraints.

    Args:
        category: restrict to one category, or omit for everything.
        limit: how many items to return, at most 100.
        offset: how many items to skip, for paging.
    """
    with session_scope() as session:
        entries = catalog.browse(session, category=category, limit=min(limit, 100), offset=offset)
        return json.dumps([e.as_document() for e in entries], indent=2)


@mcp.tool()
def search_catalog(query: str, limit: int = 25) -> str:
    """Search the catalog by title, description, sku or category.

    Args:
        query: free text to match.
        limit: how many items to return, at most 50.
    """
    with session_scope() as session:
        entries = catalog.search(session, query, limit=min(limit, 50))
        return json.dumps([e.as_document() for e in entries], indent=2)


@mcp.tool()
def get_item(sku: str) -> str:
    """Fetch one item, including whether it is returnable, age restricted or region locked.

    Args:
        sku: the item's stock keeping unit.
    """
    with session_scope() as session:
        entry = catalog.by_sku(session, sku)
        if entry is None:
            return json.dumps({"error": "ITEM_UNKNOWN", "sku": sku})
        return json.dumps(entry.as_document(), indent=2)


@mcp.tool()
def list_categories() -> str:
    """List every category the merchant sells in."""
    with session_scope() as session:
        return json.dumps({"categories": catalog.categories(session)})


@mcp.tool()
def get_policy_terms() -> str:
    """Fetch the merchant's signed policy terms and the content hash a checkout must acknowledge."""
    with session_scope() as session:
        return json.dumps(policy_terms.active_terms(session).as_document(), indent=2)


@mcp.tool()
def quote_cart(items: list[dict[str, Any]], agent_id: str = "agent:mcp") -> str:
    """Freeze prices, hold stock, and return the merchant-signed Checkout to build a mandate on.

    Args:
        items: a list of objects with a sku and a quantity.
        agent_id: the identifier this agent wants its holds counted against.
    """
    lines = [
        RequestedLine(sku=str(entry["sku"]), quantity=int(entry.get("quantity", 1)))
        for entry in items
    ]
    with session_scope() as session:
        try:
            result = create_quote(
                session, agent_id=agent_id, correlation_id=new_correlation_id(), lines=lines
            )
        except QuoteError as exc:
            return json.dumps(
                {"error": exc.reason_code.value, "message": exc.message, "detail": exc.detail},
                indent=2,
            )
        document = quote_document(result)
    endpoint = settings.PUBLIC_BASE_URL.rstrip("/") + "/checkout/complete"
    document["how_to_complete"] = (
        f"POST the four AP2 credentials to {endpoint}. The closed Checkout Mandate must carry "
        "the checkout_jwt and checkout_hash above verbatim."
    )
    return json.dumps(document, indent=2)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Dwarpal catalog MCP server.")
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
