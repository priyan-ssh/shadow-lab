"""
Shadow Lab — HF Space endpoint tester
Uses a native file picker to select a PDF, sends it to the incinerator,
saves the clean result next to the original file.

Usage:
    HF_TOKEN=hf_yourtoken python test_hf.py
    # or with token in .env:
    python test_hf.py
"""

import os
import sys
import uuid
import json
import requests
import tkinter as tk
from tkinter import filedialog

# ── Load token from env (same as courier) ────────────────────────────────────
from pathlib import Path

def load_env():
    """Load .env file from current directory if it exists."""
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

load_env()

HF_TOKEN   = os.environ.get("HF_TOKEN")
SPACE_NAME = os.environ.get("HF_SPACE_NAME", "redwolff/shadow_lab")

if not HF_TOKEN:
    print("❌ HF_TOKEN not set. Add it to .env or run:")
    print("   HF_TOKEN=hf_yourtoken python test_hf.py")
    sys.exit(1)

# ── Build Space URL ───────────────────────────────────────────────────────────
owner, spacename = SPACE_NAME.split("/")
space_url = f"https://{owner}-{spacename.replace('_', '-')}.hf.space"
api_base  = f"{space_url}/gradio_api"

HEADERS      = {"Authorization": f"Bearer {HF_TOKEN}"}
HEADERS_JSON = {**HEADERS, "Content-Type": "application/json"}


def pick_file():
    """Open native OS file picker and return selected path."""
    root = tk.Tk()
    root.withdraw()          # hide the empty tk window
    root.attributes("-topmost", True)  # bring picker to front
    path = filedialog.askopenfilename(
        title="Select a PDF to incinerate",
        filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
    )
    root.destroy()
    return path


def log(msg):
    print(msg, flush=True)


def send_to_hf(input_pdf):
    proxy_name   = f"{uuid.uuid4().hex}.pdf"
    session_hash = uuid.uuid4().hex
    output_pdf   = os.path.splitext(input_pdf)[0] + "_clean.pdf"

    log(f"\n🔥 Shadow Lab Incinerator Test")
    log(f"   Input:    {input_pdf}")
    log(f"   Output:   {output_pdf}")
    log(f"   Space:    {space_url}")
    log(f"   Proxy:    {proxy_name}\n")

    # STEP 1: Upload
    log("[1/4] Uploading...")
    with open(input_pdf, "rb") as f:
        r = requests.post(
            f"{api_base}/upload",
            headers=HEADERS,
            files={"files": (proxy_name, f, "application/pdf")},
            timeout=120
        )
    r.raise_for_status()
    server_path = r.json()[0]
    log(f"      ✅ Server path: {server_path}")

    # STEP 2: Queue join
    log("[2/4] Joining queue...")
    r = requests.post(
        f"{api_base}/queue/join",
        headers=HEADERS_JSON,
        json={
            "fn_index": 0,
            "session_hash": session_hash,
            "data": [{
                "path": server_path,
                "orig_name": proxy_name,
                "mime_type": "application/pdf",
                "size": os.path.getsize(input_pdf),
                "meta": {"_type": "gradio.FileData"}
            }]
        },
        timeout=60
    )
    r.raise_for_status()
    log(f"      ✅ Queued — {r.json()}")

    # STEP 3: Stream SSE
    log("[3/4] Waiting for ocrmypdf on HF Space...")
    result_path = None

    with requests.get(
        f"{api_base}/queue/data",
        headers=HEADERS,
        params={"session_hash": session_hash},
        stream=True,
        timeout=300
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8")

            if not decoded.startswith("data:"):
                continue
            try:
                payload = json.loads(decoded[5:].strip())
            except json.JSONDecodeError:
                continue

            msg = payload.get("msg") if isinstance(payload, dict) else None

            if msg == "process_completed":
                log(f"      Full SSE payload: {json.dumps(payload, indent=2)}")
                if not payload.get("success", True):
                    print(f"\n❌ HF returned success=false — see payload above for real error")
                    sys.exit(1)
                data = payload.get("output", {}).get("data", [])
                if data:
                    item = data[0]
                    result_path = item.get("path") or item.get("url") if isinstance(item, dict) else str(item)
                log(f"      ✅ Result path: {result_path}")
                break
            elif msg == "estimation":
                log(f"      ⏳ Queue position: {payload.get('rank', '?')}")
            elif msg == "process_starts":
                log(f"      ⚙️  Process started on HF...")
            elif msg == "process_generating":
                log(f"      ⚙️  Generating...")

    if not result_path:
        print("❌ No result path received — check HF Space Logs tab")
        sys.exit(1)

    # STEP 4: Download
    log("[4/4] Downloading clean PDF/A...")
    result_url = result_path if result_path.startswith("http") else f"{api_base}/file={result_path}"
    log(f"      URL: {result_url}")

    r = requests.get(result_url, headers=HEADERS, timeout=120)
    r.raise_for_status()

    with open(output_pdf, "wb") as f:
        f.write(r.content)

    log(f"\n✅ Done! Saved to: {output_pdf} ({len(r.content) / 1024:.1f} KB)")


if __name__ == "__main__":
    input_pdf = pick_file()
    if not input_pdf:
        print("No file selected — exiting.")
        sys.exit(0)
    send_to_hf(input_pdf)