"""
Admin dashboard routes for order management.
Cookie-based auth with HMAC-signed tokens.
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Cookie, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

admin_router = APIRouter(prefix="/admin")

# --- Config ---
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change-me-in-production")
COOKIE_NAME = "admin_token"
COOKIE_MAX_AGE = 86400  # 24 hours
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

ORDERS_DIR = os.path.join(os.path.dirname(__file__), "orders")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
ADMIN_DIR = os.path.join(os.path.dirname(__file__), "admin")

STATUS_PIPELINE = ["received", "printing", "packaging", "shipped", "delivered", "complete"]

# Valid hex order ID pattern
ORDER_ID_RE = re.compile(r"^[0-9a-f]{12}$")


# --- Auth helpers ---

def _sign_token(timestamp: str) -> str:
    """Create HMAC signature for a timestamp."""
    return hmac.new(
        ADMIN_SECRET.encode(), timestamp.encode(), hashlib.sha256
    ).hexdigest()


def _make_cookie_value() -> str:
    """Create a signed cookie value: timestamp.signature"""
    ts = str(int(time.time()))
    return f"{ts}.{_sign_token(ts)}"


def _verify_cookie(value: str) -> bool:
    """Verify cookie signature and expiry."""
    if not value or "." not in value:
        return False
    ts, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign_token(ts)):
        return False
    try:
        created = int(ts)
    except ValueError:
        return False
    return (time.time() - created) < COOKIE_MAX_AGE


def _require_auth(admin_token: Optional[str]):
    """Raise 401 if cookie is missing or invalid."""
    if not _verify_cookie(admin_token or ""):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _validate_order_id(order_id: str):
    """Validate order ID is a safe hex string."""
    if not ORDER_ID_RE.match(order_id):
        raise HTTPException(400, "Invalid order ID")


# --- Order helpers ---

def _load_order(order_id: str) -> dict:
    """Load an order JSON, applying default fields for backwards compatibility."""
    _validate_order_id(order_id)
    path = os.path.join(ORDERS_DIR, f"{order_id}.json")
    if not os.path.exists(path):
        raise HTTPException(404, "Order not found")
    with open(path) as f:
        order = json.load(f)
    # Defaults for new fields
    order.setdefault("status", "received")
    order.setdefault("status_history", [])
    order.setdefault("completed_date", None)
    order.setdefault("tracking_number", None)
    order.setdefault("anonymised", False)
    return order


def _save_order(order: dict):
    """Save order dict back to JSON."""
    order_id = order["order_id"]
    _validate_order_id(order_id)
    path = os.path.join(ORDERS_DIR, f"{order_id}.json")
    with open(path, "w") as f:
        json.dump(order, f, indent=2)


def _list_all_orders() -> List[dict]:
    """Load all orders from the orders directory."""
    orders = []
    if not os.path.isdir(ORDERS_DIR):
        return orders
    for fname in os.listdir(ORDERS_DIR):
        if fname.endswith(".json"):
            order_id = fname[:-5]
            try:
                orders.append(_load_order(order_id))
            except (HTTPException, json.JSONDecodeError):
                continue
    return orders


# --- Auth routes ---

@admin_router.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the login page."""
    path = os.path.join(ADMIN_DIR, "login.html")
    with open(path) as f:
        return HTMLResponse(f.read())


@admin_router.post("/login")
async def login(response: Response, password: str = Form(...)):
    """Validate password and set auth cookie."""
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "ADMIN_PASSWORD not configured")
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        time.sleep(1)
        raise HTTPException(401, "Invalid password")
    cookie_val = _make_cookie_value()
    resp = RedirectResponse(url="/admin/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        cookie_val,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return resp


@admin_router.post("/logout")
async def logout():
    """Clear auth cookie."""
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- Dashboard ---

@admin_router.get("/", response_class=HTMLResponse)
async def dashboard(admin_token: Optional[str] = Cookie(None)):
    """Serve the dashboard page."""
    _require_auth(admin_token)
    path = os.path.join(ADMIN_DIR, "dashboard.html")
    with open(path) as f:
        return HTMLResponse(f.read())


# --- API endpoints ---

@admin_router.get("/api/orders")
async def list_orders(
    admin_token: Optional[str] = Cookie(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: str = Query("newest"),
):
    """List all orders, optionally filtered and sorted."""
    _require_auth(admin_token)
    orders = _list_all_orders()

    if status and status != "all":
        orders = [o for o in orders if o.get("status") == status]

    if search:
        q = search.lower()
        orders = [
            o for o in orders
            if q in o.get("order_id", "").lower()
            or q in o.get("map_title", "").lower()
            or q in o.get("customer_name", "").lower()
            or q in o.get("customer_email", "").lower()
        ]

    reverse = sort != "oldest"
    orders.sort(key=lambda o: o.get("order_date", ""), reverse=reverse)

    return {"orders": orders, "total": len(orders)}


@admin_router.get("/api/orders/{order_id}")
async def get_order(order_id: str, admin_token: Optional[str] = Cookie(None)):
    """Get a single order with file availability info."""
    _require_auth(admin_token)
    order = _load_order(order_id)
    # Check which files exist
    order["files"] = {
        "gpx": os.path.exists(os.path.join(ORDERS_DIR, f"{order_id}.gpx")),
        "pdf": os.path.exists(os.path.join(ORDERS_DIR, f"{order_id}.pdf")),
        "threemf": os.path.exists(
            os.path.join(OUTPUT_DIR, order.get("job_id", ""), "model.3mf")
        ) if order.get("job_id") else False,
    }
    return order


@admin_router.patch("/api/orders/{order_id}/status")
async def update_status(
    order_id: str,
    request: Request,
    admin_token: Optional[str] = Cookie(None),
):
    """Update order status with optional note and tracking number."""
    _require_auth(admin_token)
    body = await request.json()
    new_status = body.get("status")
    note = body.get("note", "")
    tracking_number = body.get("tracking_number")

    if new_status not in STATUS_PIPELINE:
        raise HTTPException(400, f"Invalid status. Must be one of: {STATUS_PIPELINE}")

    order = _load_order(order_id)
    order["status"] = new_status
    order["status_history"].append({
        "status": new_status,
        "timestamp": datetime.utcnow().isoformat(),
        "note": note,
    })

    if new_status == "complete":
        order["completed_date"] = datetime.utcnow().isoformat()

    if tracking_number is not None:
        order["tracking_number"] = tracking_number

    _save_order(order)
    return {"ok": True, "order": order}


@admin_router.get("/api/orders/{order_id}/download/{file_type}")
async def download_file(
    order_id: str,
    file_type: str,
    admin_token: Optional[str] = Cookie(None),
):
    """Download GPX, PDF, or 3MF file for an order."""
    _require_auth(admin_token)
    _validate_order_id(order_id)

    if file_type == "gpx":
        path = os.path.join(ORDERS_DIR, f"{order_id}.gpx")
        media = "application/gpx+xml"
        fname = f"order-{order_id}.gpx"
    elif file_type == "pdf":
        path = os.path.join(ORDERS_DIR, f"{order_id}.pdf")
        media = "application/pdf"
        fname = f"order-{order_id}.pdf"
    elif file_type == "3mf":
        order = _load_order(order_id)
        job_id = order.get("job_id", "")
        if not job_id:
            raise HTTPException(404, "No job ID associated with this order")
        path = os.path.join(OUTPUT_DIR, job_id, "model.3mf")
        media = "application/vnd.ms-3mfdocument"
        fname = f"order-{order_id}.3mf"
    else:
        raise HTTPException(400, "Invalid file type — use 'gpx', 'pdf', or '3mf'")

    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type=media, filename=fname)


# --- Data retention ---

@admin_router.get("/api/retention")
async def list_retention(admin_token: Optional[str] = Cookie(None)):
    """List completed orders past the retention window."""
    _require_auth(admin_token)
    orders = _list_all_orders()
    cutoff = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).isoformat()
    eligible = [
        o for o in orders
        if o.get("status") == "complete"
        and o.get("completed_date", o.get("order_date", "")) < cutoff
        and not o.get("anonymised")
    ]
    eligible.sort(key=lambda o: o.get("completed_date", o.get("order_date", "")))
    return {"orders": eligible, "retention_days": RETENTION_DAYS}


@admin_router.post("/api/retention/anonymise")
async def anonymise_orders(request: Request, admin_token: Optional[str] = Cookie(None)):
    """Anonymise selected orders — replace personal data, delete GPX/PDF files."""
    _require_auth(admin_token)
    body = await request.json()
    order_ids = body.get("order_ids", [])
    results = []

    for oid in order_ids:
        try:
            order = _load_order(oid)
            order["customer_name"] = "[Anonymised]"
            order["customer_email"] = "[Anonymised]"
            order["address"] = "[Anonymised]"
            order["address_2"] = ""
            order["city"] = "[Anonymised]"
            order["postcode"] = "[Anonymised]"
            order["anonymised"] = True

            # Delete personal files
            for ext in (".gpx", ".pdf"):
                fpath = os.path.join(ORDERS_DIR, f"{oid}{ext}")
                if os.path.exists(fpath):
                    os.remove(fpath)

            _save_order(order)
            results.append({"order_id": oid, "ok": True})
        except HTTPException as e:
            results.append({"order_id": oid, "ok": False, "error": e.detail})

    return {"results": results}


@admin_router.delete("/api/retention/delete")
async def delete_orders(request: Request, admin_token: Optional[str] = Cookie(None)):
    """Delete selected orders — remove JSON, files, and output directory."""
    _require_auth(admin_token)
    body = await request.json()
    order_ids = body.get("order_ids", [])
    results = []

    for oid in order_ids:
        try:
            _validate_order_id(oid)
            # Read order first to get job_id before deleting
            order_path = os.path.join(ORDERS_DIR, f"{oid}.json")
            job_id = None
            if os.path.exists(order_path):
                with open(order_path) as f:
                    job_id = json.load(f).get("job_id")

            # Remove order JSON and associated files
            for ext in (".json", ".gpx", ".pdf"):
                fpath = os.path.join(ORDERS_DIR, f"{oid}{ext}")
                if os.path.exists(fpath):
                    os.remove(fpath)

            # Remove output directory if job_id is known
            if job_id:
                job_dir = os.path.join(OUTPUT_DIR, job_id)
                if os.path.isdir(job_dir):
                    shutil.rmtree(job_dir)

            results.append({"order_id": oid, "ok": True})
        except HTTPException as e:
            results.append({"order_id": oid, "ok": False, "error": e.detail})

    return {"results": results}
