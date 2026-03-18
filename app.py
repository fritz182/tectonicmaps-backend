"""
FastAPI backend for Topography website.
Receives GPX uploads, runs route2tile to generate real 3D terrain,
and returns STL (for browser preview) + 3MF (for download/email).
"""

import asyncio
import os
import sys
import uuid
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
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
        shutil.rmtree(job_dir, ignore_errors=True)
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


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Frontend is served by Cloudflare Pages — no static file serving needed here
