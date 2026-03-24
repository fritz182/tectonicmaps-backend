"""
FastAPI backend for Topography website.
Receives GPX uploads, runs route2tile to generate real 3D terrain,
and returns STL (for browser preview) + 3MF (for download/email).
"""

import asyncio
import json
import logging
import os
import sys
import uuid
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import smtplib

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Add route2tile package to Python path
# On Hetzner: /opt/tectonicmaps/route2tile  (set via ROUTE2TILE_DIR env var)
# Locally:    ../../Mapping code/route2tile-main
ROUTE2TILE_DIR = os.environ.get(
    "ROUTE2TILE_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "Mapping code", "route2tile-main"))
)
sys.path.insert(0, ROUTE2TILE_DIR)

app = FastAPI(title="Topography API")

# Allow the Cloudflare-hosted frontend to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://tectonicmaps.com",
        "https://www.tectonicmaps.com",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Directory for generated output files
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Serve generated files statically
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")


_executor = ThreadPoolExecutor(max_workers=2)

# Path to the route2tile CLI
# On Hetzner: /opt/tectonicmaps/venv/bin/route2tile  (set via ROUTE2TILE_BIN env var)
# Locally:    ./venv/bin/route2tile
ROUTE2TILE_BIN = os.environ.get(
    "ROUTE2TILE_BIN",
    os.path.join(os.path.dirname(__file__), ".venv", "bin", "route2tile")
)


def _run_generation(gpx_path, job_dir, stl_path, threemf_path):
    """
    Run the full route2tile CLI — the exact same pipeline used for printing.
    Includes terrain, water cutouts, buildings, roads, and route ribbon
    with all the calibrated defaults from cli.py.

    Generates 3MF (multi-colour for download + viewer) and STL (fallback).
    """
    import subprocess

    # Generate 3MF — uses 256 grid for speed (OSM queries run in parallel via cli.py)
    threemf_result = subprocess.run(
        [
            ROUTE2TILE_BIN,
            "--gpx", gpx_path,
            "--out", threemf_path,
            "--grid", "256",
        ],
        cwd=job_dir,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if threemf_result.returncode != 0:
        raise RuntimeError(
            f"route2tile failed:\n{threemf_result.stderr}\n{threemf_result.stdout}"
        )

    # STL fallback skipped — 3MF is used for both preview and download


@app.post("/api/generate")
async def generate_model(gpx: UploadFile = File(...)):
    """
    Upload a GPX file -> run route2tile -> return URLs for STL preview + 3MF download.
    """
    if not gpx.filename.lower().endswith(".gpx"):
        raise HTTPException(400, "File must be a .gpx file")

    content = await gpx.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10MB)")

    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    gpx_path = os.path.join(job_dir, "input.gpx")
    with open(gpx_path, "wb") as f:
        f.write(content)

    stl_path = os.path.join(job_dir, "model.stl")
    threemf_path = os.path.join(job_dir, "model.3mf")

    try:
        # Run heavy computation in thread pool so we don't block the event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            _executor, _run_generation, gpx_path, job_dir, stl_path, threemf_path
        )

        return {
            "job_id": job_id,
            "stl_url": f"/output/{job_id}/model.stl",
            "threemf_url": f"/output/{job_id}/model.3mf",
        }

    except Exception as e:
        import logging
        logging.error(f"Job {job_id} failed: {e}")
        # Keep job dir for debugging — don't delete
        raise HTTPException(500, f"Model generation failed: {str(e)}")


@app.get("/api/download/{job_id}")
async def download_3mf(job_id: str):
    """Download the generated 3MF file."""
    threemf_path = os.path.join(OUTPUT_DIR, job_id, "model.3mf")
    if not os.path.exists(threemf_path):
        raise HTTPException(404, "File not found — it may have expired")
    return FileResponse(
        threemf_path,
        media_type="application/vnd.ms-3mfdocument",
        filename=f"topography-{job_id}.3mf",
    )


# --- Email config (set via environment variables on Hetzner) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
NOTIFY_EMAIL = "hello@tectonicmaps.com"

# --- Orders directory ---
ORDERS_DIR = os.path.join(os.path.dirname(__file__), "orders")
os.makedirs(ORDERS_DIR, exist_ok=True)


def _send_order_email(order: dict, gpx_bytes: bytes, pdf_bytes: bytes | None):
    """Send order notification email to hello@tectonicmaps.com."""
    if not SMTP_HOST or not SMTP_USER:
        logging.warning("SMTP not configured — skipping email. Set SMTP_HOST, SMTP_USER, SMTP_PASS env vars.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"New Order: {order['map_title']} — {order['customer_name']}"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL

    order_id = order['order_id']
    body = f"""New TectonicMaps Order
{'='*40}

Map Title:     {order['map_title']}
Job ID:        {order.get('job_id', 'N/A')}
Price:         £{order['price']}
Discount:      {order.get('discount_code') or 'None'}

Customer:      {order['customer_name']}
Email:         {order['customer_email']}

Shipping Address:
  {order['address']}
  {order.get('address_2') or ''}
  {order['city']}
  {order['postcode']}
  {order['country']}

Stats:         {order.get('stats', 'N/A')}

Order Date:    {order['order_date']}
Order ID:      {order_id}

Downloads:
  3MF Model:   https://api.tectonicmaps.com/api/download/{order.get('job_id', '')}
  GPX File:    https://api.tectonicmaps.com/api/order-file/{order_id}/gpx
  PDF Background: https://api.tectonicmaps.com/api/order-file/{order_id}/pdf
"""
    msg.set_content(body)

    # Attach GPX file only (small enough for email)
    if gpx_bytes:
        msg.add_attachment(
            gpx_bytes,
            maintype="application",
            subtype="gpx+xml",
            filename=f"{order['map_title'].replace(' ', '_')}.gpx",
        )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def _send_customer_confirmation(order: dict):
    """Send order confirmation email to the customer."""
    if not SMTP_HOST or not SMTP_USER:
        return

    msg = EmailMessage()
    msg["Subject"] = f"Order Confirmed — {order['map_title']} | TectonicMaps"
    msg["From"] = SMTP_USER
    msg["To"] = order["customer_email"]

    address_line_2 = f"<br>{order['address_2']}" if order.get('address_2') else ""

    # Plain text fallback
    plain = f"""Hi {order['customer_name']},

Thank you for your order! We've received it and will begin preparing your custom 3D map.

Map: {order['map_title']}
Price: £{order['price']}
Order ID: {order['order_id']}

Delivery Address:
{order['address']}
{order.get('address_2') or ''}
{order['city']}, {order['postcode']}
{order['country']}

We'll email you when your map is ready and on its way.

TectonicMaps — hello@tectonicmaps.com
"""
    msg.set_content(plain)

    # HTML version
    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f5f3f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f3f0;padding:32px 16px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;max-width:600px;">

        <!-- Header -->
        <tr>
          <td style="background:#AD4E38;padding:32px;text-align:center;">
            <img src="https://tectonicmaps.com/assets/Logo/Logo%20TechtonicMaps%20WHITE.png" alt="TectonicMaps" width="180" style="max-width:180px;">
          </td>
        </tr>

        <!-- Confirmation -->
        <tr>
          <td style="padding:40px 32px 24px;text-align:center;">
            <div style="width:56px;height:56px;border-radius:50%;background:#e8f5e9;margin:0 auto 16px;line-height:56px;font-size:28px;color:#2d7d3a;">&#10003;</div>
            <h1 style="margin:0 0 8px;font-size:24px;color:#1a1a1a;font-weight:600;">Order Confirmed</h1>
            <p style="margin:0;color:#666;font-size:15px;">Thanks {order['customer_name']}, we've received your order!</p>
          </td>
        </tr>

        <!-- Order Details -->
        <tr>
          <td style="padding:0 32px 32px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9f7f5;border-radius:8px;padding:24px;">
              <tr>
                <td style="padding:24px;">
                  <h2 style="margin:0 0 16px;font-size:16px;color:#AD4E38;text-transform:uppercase;letter-spacing:1px;">Order Details</h2>
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:8px 0;color:#666;font-size:14px;border-bottom:1px solid #e8e4e0;">Map</td>
                      <td style="padding:8px 0;color:#1a1a1a;font-size:14px;text-align:right;font-weight:500;border-bottom:1px solid #e8e4e0;">{order['map_title']}</td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#666;font-size:14px;border-bottom:1px solid #e8e4e0;">Price</td>
                      <td style="padding:8px 0;color:#1a1a1a;font-size:14px;text-align:right;font-weight:500;border-bottom:1px solid #e8e4e0;">&pound;{order['price']}</td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#666;font-size:14px;">Order ID</td>
                      <td style="padding:8px 0;color:#1a1a1a;font-size:14px;text-align:right;font-weight:500;">{order['order_id']}</td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Delivery Address -->
        <tr>
          <td style="padding:0 32px 32px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9f7f5;border-radius:8px;">
              <tr>
                <td style="padding:24px;">
                  <h2 style="margin:0 0 12px;font-size:16px;color:#AD4E38;text-transform:uppercase;letter-spacing:1px;">Delivery Address</h2>
                  <p style="margin:0;color:#1a1a1a;font-size:14px;line-height:1.6;">
                    {order['address']}{address_line_2}<br>
                    {order['city']}, {order['postcode']}<br>
                    {order['country']}
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- What's Next -->
        <tr>
          <td style="padding:0 32px 40px;text-align:center;">
            <h2 style="margin:0 0 8px;font-size:18px;color:#1a1a1a;">What happens next?</h2>
            <p style="margin:0;color:#666;font-size:14px;line-height:1.6;">We'll begin preparing your custom 3D map and email you when it's ready and on its way.</p>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f0ece8;padding:24px 32px;text-align:center;">
            <p style="margin:0 0 4px;color:#999;font-size:13px;">Questions? Reply to this email or contact us at</p>
            <a href="mailto:hello@tectonicmaps.com" style="color:#AD4E38;font-size:13px;text-decoration:none;">hello@tectonicmaps.com</a>
            <p style="margin:16px 0 0;color:#ccc;font-size:12px;">TectonicMaps &mdash; Custom 3D Terrain Maps</p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


@app.post("/api/order")
async def place_order(
    job_id: str = Form(""),
    map_title: str = Form("Custom Map"),
    customer_name: str = Form(...),
    customer_email: str = Form(...),
    address: str = Form(...),
    address_2: str = Form(""),
    city: str = Form(...),
    postcode: str = Form(...),
    country: str = Form("United Kingdom"),
    price: str = Form("45"),
    discount_code: str = Form(""),
    stats: str = Form(""),
    gpx_file: Optional[UploadFile] = File(None),
    pdf_file: Optional[UploadFile] = File(None),
):
    """Place an order — saves order details and emails notification."""
    order_id = uuid.uuid4().hex[:12]
    order = {
        "order_id": order_id,
        "job_id": job_id,
        "map_title": map_title,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "address": address,
        "address_2": address_2,
        "city": city,
        "postcode": postcode,
        "country": country,
        "price": price,
        "discount_code": discount_code,
        "stats": stats,
        "order_date": datetime.utcnow().isoformat(),
        "status": "received",
    }

    # Save order to JSON file
    order_path = os.path.join(ORDERS_DIR, f"{order_id}.json")
    with open(order_path, "w") as f:
        json.dump(order, f, indent=2)

    # Read uploaded files
    gpx_bytes = await gpx_file.read() if gpx_file else None
    pdf_bytes = await pdf_file.read() if pdf_file else None

    # Save GPX and PDF alongside order
    if gpx_bytes:
        with open(os.path.join(ORDERS_DIR, f"{order_id}.gpx"), "wb") as f:
            f.write(gpx_bytes)
    if pdf_bytes:
        with open(os.path.join(ORDERS_DIR, f"{order_id}.pdf"), "wb") as f:
            f.write(pdf_bytes)

    # Send email notifications (in background to not block response)
    def _send_all_emails():
        _send_order_email(order, gpx_bytes, pdf_bytes)
        _send_customer_confirmation(order)

    try:
        await asyncio.get_event_loop().run_in_executor(None, _send_all_emails)
    except Exception as e:
        logging.error(f"Failed to send emails for {order_id}: {e}")
        # Don't fail the order if email fails — order is already saved

    return {"order_id": order_id, "status": "received"}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/order-file/{order_id}/{file_type}")
async def download_order_file(order_id: str, file_type: str):
    """Download GPX or PDF file for an order."""
    if file_type not in ("gpx", "pdf"):
        raise HTTPException(400, "Invalid file type — use 'gpx' or 'pdf'")
    ext = ".gpx" if file_type == "gpx" else ".pdf"
    media = "application/gpx+xml" if file_type == "gpx" else "application/pdf"
    file_path = os.path.join(ORDERS_DIR, f"{order_id}{ext}")
    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found")
    return FileResponse(file_path, media_type=media, filename=f"order-{order_id}{ext}")


# Frontend is served by Cloudflare Pages — no static file serving needed here
