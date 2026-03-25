"""
PayPal integration routes.
Server-side price enforcement + PayPal Orders API v2.
"""

import logging
import os
import re
import time

import requests as http_requests
from fastapi import APIRouter, HTTPException, Request

paypal_router = APIRouter(prefix="/api/paypal")

# --- Config ---
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")
PAYPAL_BASE = (
    "https://api-m.paypal.com" if PAYPAL_MODE == "live"
    else "https://api-m.sandbox.paypal.com"
)

# --- Pricing ---
GPX_MAP_PRICE = 45.00
CURRENCY = "GBP"
DISCOUNTS = {
    "LAUNCH20": 20,
    "TOPO10": 10,
}

# --- OAuth token cache ---
_token_cache = {"token": "", "expires": 0}

# Valid PayPal order ID pattern (alphanumeric, typically 17 chars)
PAYPAL_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9]{10,30}$")


def _get_access_token() -> str:
    """Get PayPal OAuth2 access token (cached)."""
    if _token_cache["token"] and time.time() < _token_cache["expires"]:
        return _token_cache["token"]

    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(500, "PayPal not configured")

    resp = http_requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        logging.error("PayPal OAuth failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "Payment service unavailable")

    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + data.get("expires_in", 3600) - 60
    return _token_cache["token"]


def _calculate_price(item_count: int, discount_code: str = "") -> dict:
    """Calculate total price with optional discount. All values server-enforced."""
    if item_count < 1 or item_count > 50:
        raise HTTPException(400, "Invalid item count")

    subtotal = round(GPX_MAP_PRICE * item_count, 2)
    discount_pct = 0

    if discount_code:
        code = discount_code.strip().upper()
        discount_pct = DISCOUNTS.get(code, 0)

    discount_amount = round(subtotal * discount_pct / 100, 2)
    total = round(subtotal - discount_amount, 2)

    return {
        "subtotal": subtotal,
        "discount_pct": discount_pct,
        "discount_amount": discount_amount,
        "total": total,
        "currency": CURRENCY,
        "unit_price": GPX_MAP_PRICE,
    }


@paypal_router.get("/client-id")
async def get_client_id():
    """Return the PayPal client ID for the frontend SDK."""
    if not PAYPAL_CLIENT_ID:
        raise HTTPException(500, "PayPal not configured")
    return {"client_id": PAYPAL_CLIENT_ID, "currency": CURRENCY}


@paypal_router.post("/validate-discount")
async def validate_discount(request: Request):
    """Validate a discount code and return percentage."""
    body = await request.json()
    code = body.get("code", "").strip().upper()
    if len(code) > 50:
        raise HTTPException(400, "Invalid code")
    pct = DISCOUNTS.get(code, 0)
    return {"valid": pct > 0, "percentage": pct}


@paypal_router.post("/create-order")
async def create_paypal_order(request: Request):
    """Create a PayPal order with server-enforced pricing."""
    body = await request.json()
    item_count = int(body.get("item_count", 1))
    discount_code = body.get("discount_code", "")

    if len(discount_code) > 50:
        raise HTTPException(400, "Invalid discount code")

    pricing = _calculate_price(item_count, discount_code)
    token = _get_access_token()

    paypal_order = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": CURRENCY,
                "value": f"{pricing['total']:.2f}",
                "breakdown": {
                    "item_total": {
                        "currency_code": CURRENCY,
                        "value": f"{pricing['subtotal']:.2f}",
                    },
                    "discount": {
                        "currency_code": CURRENCY,
                        "value": f"{pricing['discount_amount']:.2f}",
                    },
                },
            },
            "items": [{
                "name": "Custom GPX 3D Map",
                "quantity": str(item_count),
                "unit_amount": {
                    "currency_code": CURRENCY,
                    "value": f"{GPX_MAP_PRICE:.2f}",
                },
                "category": "PHYSICAL_GOODS",
            }],
        }],
    }

    resp = http_requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        json=paypal_order,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        logging.error("PayPal create-order failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "Could not create payment")

    data = resp.json()
    return {"id": data["id"], "pricing": pricing}


@paypal_router.post("/capture-order")
async def capture_paypal_order(request: Request):
    """Capture a PayPal order after buyer approves."""
    body = await request.json()
    paypal_order_id = body.get("order_id", "")

    if not paypal_order_id or not PAYPAL_ORDER_ID_RE.match(paypal_order_id):
        raise HTTPException(400, "Invalid PayPal order ID")

    token = _get_access_token()

    resp = http_requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders/{paypal_order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        logging.error("PayPal capture failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "Payment capture failed")

    data = resp.json()

    if data.get("status") != "COMPLETED":
        raise HTTPException(400, "Payment not completed")

    capture = data["purchase_units"][0]["payments"]["captures"][0]

    return {
        "status": "COMPLETED",
        "paypal_order_id": data["id"],
        "capture_id": capture["id"],
        "amount": capture["amount"]["value"],
        "currency": capture["amount"]["currency_code"],
    }
