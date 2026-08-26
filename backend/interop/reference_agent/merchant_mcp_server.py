"""Dwarpal, wearing the interface the AP2 reference shopping agent expects of a merchant.

`shopping_agent_v2` in the published AP2 samples reaches its merchant over MCP stdio, spawning
`roles/merchant_agent_mcp/server.py` as a subprocess and calling five tools on it:

    search_inventory   check_product   assemble_cart   create_checkout   complete_checkout

The sample server answers those from a dictionary of two hard-coded products and a stubbed payment
processor. This module answers them from Dwarpal: the real catalog, real live stock, a real quote
that freezes prices and holds inventory, the merchant's real signed Checkout, and a real settlement
through `/checkout/complete`, where the whole verification pipeline and the policy kernel run.

Nothing here is a shortcut. Every tool is an HTTP call to a running Dwarpal, exactly as any other
external agent would make, so an upstream run and a curl run reach the same code and get the same
answer. In particular `complete_checkout` does not decide anything itself: it posts the credential
chain and reports what the merchant decided, reason code included. An upstream credential Dwarpal
refuses is reported as a refusal rather than smoothed into a success.

    python -m interop.reference_agent.merchant_mcp_server        stdio, as the agent spawns it

Configuration, all optional:

    DWARPAL_BASE_URL   where the merchant is        (default http://127.0.0.1:8000)
    TEMP_DB_DIR        the shared mandate store     (default ../.temp-db, as upstream uses)
    LOGS_DIR           where to write the log       (default ../.logs, as upstream uses)
"""

# No postponed annotations: FastMCP reads the real annotation objects when it builds tool schemas.
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

MERCHANT = os.environ.get("DWARPAL_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TEMP_DB = Path(os.environ.get("TEMP_DB_DIR", Path(__file__).resolve().parent / ".temp-db"))
LOG_DIR = Path(os.environ.get("LOGS_DIR", Path(__file__).resolve().parent / ".logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_DIR / "dwarpal-merchant-mcp.log"),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("dwarpal-merchant-mcp")

mcp = FastMCP("dwarpal-merchant")

# The agent identity this adapter transacts under, so upstream traffic is distinguishable from
# console and corpus traffic in the merchant's verdict log.
AGENT_ID = os.environ.get("DWARPAL_AGENT_ID", "agent:ap2-reference-shopping-agent")

_CARTS: dict[str, dict[str, Any]] = {}


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        MERCHANT + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Agent-Id": AGENT_ID,
            "ngrok-skip-browser-warning": "1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")[:400]}
    except urllib.error.URLError as exc:
        return 0, {"error": "merchant_unreachable", "message": str(exc)}


def _rupees(minor: int) -> float:
    """Upstream talks in major units as a float; Dwarpal is integer minor units throughout."""
    return round(minor / 100, 2)


def _as_match(item: dict[str, Any]) -> dict[str, Any]:
    availability = item.get("availability") or {}
    return {
        "item_id": item["sku"],
        "name": item["title"],
        "price": _rupees(int(item["price"]["amount"])),
        "currency": item["price"]["currency"],
        # What is actually buyable right now, which is the shelf minus everyone else's holds.
        "stock": int(availability.get("available_quantity", 0)),
        "description": item.get("description", ""),
        "category": item.get("category"),
        # The part a generated catalog cannot offer: what the merchant will actually allow.
        "purchase_constraints": item.get("purchase_constraints") or {},
    }


def _lookup(sku: str) -> dict[str, Any] | None:
    status, body = _request("GET", f"/catalog/items/{urllib.parse.quote(sku)}")
    return body if status == 200 else None


def _search(text: str) -> list[dict[str, Any]]:
    """Search the catalog, or list it when there is nothing to search for."""
    query = text.strip()
    path = f"/catalog/search?q={urllib.parse.quote(query)}" if query else "/catalog/items?limit=50"
    status, body = _request("GET", path)
    return body.get("items", []) if status == 200 else []


def _resolve(item_id: str) -> dict[str, Any] | None:
    """Find the catalog item an agent means by ``item_id``.

    The reference shopping agent does not carry a sku around. It builds an identifier by slugifying
    its own description of the product and appending a variant index, because the sample merchant
    it was written against fabricates an item to match whatever slug it is handed. A merchant with
    a real catalog cannot do that and should not pretend to: what it can do is work out which of
    the things it actually sells the agent is describing.

    So an exact sku is used as given, and anything else is turned back into words and searched for.
    A slug that matches nothing returns nothing, and the tool says so.
    """
    direct = _lookup(item_id)
    if direct is not None:
        return direct

    words = re.sub(r"_\d+$", "", item_id)
    words = re.sub(r"[^a-zA-Z0-9]+", " ", words).strip()
    if not words:
        return None

    # Every word has to match, and no guessing beyond that. Dropping qualifiers until something
    # matches sounds helpful and is not: asked for "red wireless gaming headphones" it would answer
    # with a bottle of red wine. A merchant that says it does not stock the thing, and lists what it
    # does stock, is more use to an agent than one that confidently hands back the wrong item.
    matches = _search(words)
    return matches[0] if matches else None


@mcp.tool()
def search_inventory(
    product_description: str,
    constraint_price_cap: float | None = None,
) -> dict[str, Any]:
    """Search the merchant's catalog for products matching a description.

    Args:
      product_description: A description of the product to search for.
      constraint_price_cap: An optional cap, in major currency units, from the human's mandate.
    """

    # An empty or placeholder description lists the catalog rather than refusing: an agent that
    # has lost track of what it was looking for needs to be shown what is actually for sale.
    matches = [_as_match(item) for item in _search(product_description or "")]
    if constraint_price_cap is not None:
        matches = [m for m in matches if m["price"] <= constraint_price_cap]
    logger.info("search_inventory %r -> %d matches", product_description, len(matches))
    if not matches:
        everything = [_as_match(item) for item in _search("")]
        return {
            "matches": [],
            "message": (
                f"No catalog item matches {product_description!r}"
                + (f" under {constraint_price_cap}" if constraint_price_cap is not None else "")
                + ". This merchant sells a fixed catalog; these are the items it stocks."
            ),
            "catalog": [
                {"item_id": m["item_id"], "name": m["name"], "price": m["price"]}
                for m in everything
            ],
        }
    return {
        "matches": matches,
        "message": (
            f"Found {len(matches)} matching product(s). Stock is live: these are real counts, "
            "and a hold is placed when the cart is assembled."
        ),
    }


@mcp.tool()
def check_product(
    item_id: str,
    constraint_price_cap: float | None = None,
) -> dict[str, Any]:
    """Return the current price and availability for one item.

    Args:
      item_id: The item's sku, as returned by search_inventory.
      constraint_price_cap: An optional cap, in major currency units, to compare the price against.
    """
    body = _resolve(item_id)
    if body is None:
        logger.info("check_product %r -> no catalog item matches", item_id)
        return {
            "error": "item_not_found",
            "item_id": item_id,
            "message": (
                "This merchant sells a fixed catalog and does not create items on demand. "
                "Call search_inventory to see what it actually stocks."
            ),
            "available_items": [m["item_id"] for m in (_as_match(i) for i in _search(""))][:12],
        }

    match = _as_match(body)
    # available means in stock, as it does upstream. Whether the price fits the human's cap is a
    # separate question and is answered separately, because conflating them would tell an agent an
    # item is unavailable when it is merely dearer than it hoped.
    available = match["stock"] > 0
    within_cap = constraint_price_cap is None or match["price"] <= constraint_price_cap
    logger.info(
        "check_product %r -> %s stock=%s price=%s cap=%s",
        item_id,
        match["item_id"],
        match["stock"],
        match["price"],
        constraint_price_cap,
    )
    return {
        **match,
        "available": available,
        "within_price_cap": within_cap,
        "message": (
            f"{match['name']} at {match['price']} {match['currency']}, {match['stock']} in stock"
            + ("." if within_cap else f", which is above the cap of {constraint_price_cap}.")
            if available
            else f"{match['name']} is out of stock."
        ),
    }


@mcp.tool()
def assemble_cart(item_id: str, qty: int) -> dict[str, Any]:
    """Quote a cart with the merchant, which freezes the price and holds the stock.

    Args:
      item_id: The item's sku.
      qty: Number of units.

    Returns:
      cart_id, to pass to create_checkout, plus the line items, total and currency.
    """
    resolved = _resolve(item_id)
    if resolved is None:
        logger.info("assemble_cart %r -> no catalog item matches", item_id)
        return {
            "error": "item_not_found",
            "item_id": item_id,
            "message": "no catalog item matches that identifier; call search_inventory first",
        }
    status, body = _request(
        "POST", "/checkout/quote", {"items": [{"sku": resolved["sku"], "quantity": int(qty)}]}
    )
    if status != 200:
        error = body.get("error", body)
        logger.info("assemble_cart %r -> quote refused: %s", item_id, error.get("code"))
        return {
            "error": error.get("code", "quote_refused"),
            "message": error.get("message", str(body)[:300]),
            # The merchant says what the agent should do next rather than leaving it to guess.
            "action": error.get("action"),
        }

    cart_id = body["checkout_id"]
    _CARTS[cart_id] = body
    logger.info("assemble_cart %s x%s -> %s", item_id, qty, cart_id)
    return {
        "cart_id": cart_id,
        "line_items": [
            {
                "item_id": line["item"]["id"],
                "name": line["item"]["title"],
                "qty": line["quantity"],
                # A Checkout line item carries the unit price as bare minor units, not an amount
                # object; the catalog document is the one that carries currency alongside it.
                "price": _rupees(int(line["item"]["price"])),
            }
            for line in body["checkout"]["line_items"]
        ],
        "total": _rupees(int(body["total"]["amount"])),
        "currency": body["total"]["currency"],
        "expires_at": body["expires_at"],
        "policy_hash": body["policy_hash"],
        "message": (
            "Stock is held against this cart until it expires. The policy hash above is the one "
            "the closed Checkout Mandate must acknowledge."
        ),
    }


@mcp.tool()
def create_checkout(cart_id: str, open_checkout_mandate_id: str = "") -> dict[str, Any]:
    """Return the merchant's own signed Checkout for a quoted cart.

    Dwarpal signs the Checkout at quote time, because that signature is the merchant's commitment
    to fulfil at that sku, price and shipping. This tool returns what was already signed rather
    than minting a second one.

    Args:
      cart_id: From assemble_cart.
      open_checkout_mandate_id: The human's open mandate id, recorded for the audit trail.

    Returns:
      checkout_jwt and checkout_jwt_hash, both of which the closed Checkout Mandate must carry
      verbatim.
    """
    cart = _CARTS.get(cart_id)
    if cart is None:
        status, cart = _request("GET", f"/checkout/{urllib.parse.quote(cart_id)}")
        if status != 200:
            return {"error": "cart_not_found", "message": f"No cart for cart_id={cart_id}"}
        _CARTS[cart_id] = cart

    if "checkout_jwt" not in cart:
        return {
            "error": "cart_not_quotable",
            "message": "this checkout was not created through assemble_cart",
        }
    logger.info("create_checkout %s (mandate %s)", cart_id, open_checkout_mandate_id or "-")
    return {
        "checkout_jwt": cart["checkout_jwt"],
        "checkout_jwt_hash": cart["checkout_hash"],
        "policy_hash": cart["policy_hash"],
        "total": _rupees(int(cart["total"]["amount"])),
        "currency": cart["total"]["currency"],
        "message": (
            "Signed by the merchant. The closed Checkout Mandate must carry the checkout_jwt and "
            "checkout_jwt_hash unchanged, and acknowledge the policy hash."
        ),
    }


# A mandate id names a file in the shared store. The agent supplies it, so it is checked as input
# rather than trusted as a name: letters, digits, dash and underscore only.
_MANDATE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _load_mandate(mandate_id: str) -> str | None:
    """Read a mandate chain from the store the upstream agent writes it to."""
    # The id arrives from a model through JSON, so it is not necessarily a string.
    if not isinstance(mandate_id, str) or not _MANDATE_ID.match(mandate_id):
        logger.info("refused a mandate id that is not a plain name: %r", str(mandate_id)[:80])
        return None
    path = TEMP_DB / f"{mandate_id}.sdjwt"
    try:
        # Belt and braces: the pattern above already forbids a separator, and this confirms the
        # resolved path did not leave the store anyway.
        path.resolve().relative_to(TEMP_DB.resolve())
    except (OSError, ValueError):
        logger.info("refused a mandate id that resolved outside the store: %r", mandate_id[:80])
        return None
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _load_token_store() -> dict[str, Any]:
    try:
        return json.loads((TEMP_DB / "ap2_token_store.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@mcp.tool()
def complete_checkout(
    payment_token: str,
    checkout_mandate_id: str,
    checkout_nonce: str,
    open_checkout_mandate_id: str = "",
) -> dict[str, Any]:
    """Present the credential chain to the merchant and report what it decided.

    This tool decides nothing. It posts the four AP2 credentials to /checkout/complete, where the
    seven verification steps and the policy kernel run, and returns the merchant's answer verbatim,
    including the reason code when the answer is no.

    Args:
      payment_token: The token the credential provider issued, which the payment mandate is bound
        to.
      checkout_mandate_id: The id of the closed Checkout Mandate chain, stored as
        <id>.sdjwt in the shared mandate directory.
      checkout_nonce: The nonce the shopping agent used in its key-binding proof.
      open_checkout_mandate_id: The id of the human's open Checkout Mandate chain.

    Returns:
      status, order_id and the merchant's signed receipt when the purchase settles; otherwise the
      reason code and the action the agent should take next.
    """
    if not checkout_nonce:
        return {"error": "missing_checkout_nonce", "message": "checkout_nonce is required"}

    closed_checkout = _load_mandate(checkout_mandate_id)
    if not closed_checkout:
        return {
            "error": "mandate_not_found",
            "message": f"could not load {checkout_mandate_id}.sdjwt from {TEMP_DB}",
        }

    open_checkout = _load_mandate(open_checkout_mandate_id)
    token_data = _load_token_store().get(payment_token) or {}
    open_payment = _load_mandate(token_data.get("open_payment_mandate_id", "")) or _load_mandate(
        f"{payment_token}_open"
    )
    closed_payment = _load_mandate(token_data.get("payment_mandate_id", "")) or _load_mandate(
        payment_token
    )

    if not open_checkout:
        return {
            "error": "open_mandate_not_found",
            "message": (
                "the human's open Checkout Mandate is required: pass open_checkout_mandate_id, "
                f"stored as <id>.sdjwt in {TEMP_DB}"
            ),
        }

    status, outcome = _request(
        "POST",
        "/checkout/complete",
        {
            "open_checkout_mandate": open_checkout,
            "closed_checkout_mandate": closed_checkout,
            "open_payment_mandate": open_payment,
            "closed_payment_mandate": closed_payment,
            "nonce": checkout_nonce,
        },
    )
    logger.info("complete_checkout -> HTTP %s %s", status, outcome.get("reason_code"))

    if outcome.get("status") in ("completed", "awaiting_payment"):
        return {
            "status": "Success",
            "order_id": outcome.get("checkout_id"),
            "payment_receipt": outcome.get("receipt"),
            "receipt_jwt": outcome.get("receipt_jwt"),
            "evidence_packet_id": outcome.get("evidence_packet_id"),
            "reason_code": outcome.get("reason_code"),
            "message": "The merchant verified the chain, the kernel approved, and it is recorded.",
        }

    error = outcome.get("error") if isinstance(outcome.get("error"), dict) else {}
    return {
        "status": "Error",
        "order_id": None,
        "payment_receipt": None,
        "reason_code": outcome.get("reason_code") or error.get("code"),
        "action": outcome.get("action") or error.get("action"),
        "evidence_packet_id": outcome.get("evidence_packet_id"),
        "message": (
            error.get("message")
            or "The merchant refused. The reason code says why and the action says what to do."
        ),
        "detail": outcome.get("detail") or error.get("detail"),
    }


def main() -> None:
    logger.info("serving Dwarpal at %s to an MCP client, mandate store %s", MERCHANT, TEMP_DB)
    mcp.run()


if __name__ == "__main__":
    main()
