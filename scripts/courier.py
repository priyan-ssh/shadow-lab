import os
import time
import uuid
import json
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [COURIER] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

AIRLOCK_OUT = "/airlock/outbound"
AIRLOCK_IN  = "/airlock/inbound"

HF_TOKEN   = os.environ.get("HF_TOKEN")
SPACE_NAME = os.environ.get("HF_SPACE_NAME")

if not HF_TOKEN or not SPACE_NAME:
    raise RuntimeError(
        "HF_TOKEN and HF_SPACE_NAME must be set in your .env file.\n"
        "  HF_TOKEN=hf_yourtoken\n"
        "  HF_SPACE_NAME=your_username/your_space_name"
    )

# "redwolff/shadow_lab" → "https://redwolff-shadow-lab.hf.space"
owner, spacename = SPACE_NAME.split("/")
space_url = f"https://{owner}-{spacename.replace('_', '-')}.hf.space"
api_base  = f"{space_url}/gradio_api"

HEADERS      = {"Authorization": f"Bearer {HF_TOKEN}"}
HEADERS_JSON = {**HEADERS, "Content-Type": "application/json"}

# Max time to wait for a job to complete (10 minutes)
MAX_WAIT_SECONDS  = 600
# How long to wait before reconnecting after a dropped SSE stream
RECONNECT_DELAY   = 3
# Max reconnection attempts before giving up
MAX_RECONNECTS    = 10


def upload_file(filepath, proxy_name):
    """Upload file to HF Space, returns server-side path."""
    log.info(f"   Uploading as proxy: {proxy_name}")
    with open(filepath, 'rb') as f:
        r = requests.post(
            f"{api_base}/upload",
            headers=HEADERS,
            files={"files": (proxy_name, f, "application/pdf")},
            timeout=120
        )
    r.raise_for_status()
    server_path = r.json()[0]
    log.info(f"   Upload complete — server path: {server_path}")
    return server_path


def poll_sse(session_hash, filename, start_time):
    """
    Connect to SSE stream and read until process_completed.
    Returns (result_path, done) — reconnects automatically if stream drops.
    """
    reconnects = 0

    while reconnects < MAX_RECONNECTS:
        elapsed = time.time() - start_time
        if elapsed > MAX_WAIT_SECONDS:
            raise RuntimeError(f"Job timed out after {MAX_WAIT_SECONDS}s")

        try:
            log.info(f"   [{filename}] Connecting to SSE stream (attempt {reconnects + 1})...")
            with requests.get(
                f"{api_base}/queue/data",
                headers=HEADERS,
                params={"session_hash": session_hash},
                stream=True,
                timeout=60   # 60s read timeout per chunk — reconnects if silent
            ) as stream:
                stream.raise_for_status()
                log.info(f"   [{filename}] SSE connected — waiting for ocrmypdf...")

                for line in stream.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8")
                    log.info(f"   [{filename}] SSE: {decoded[:200]}")

                    if not decoded.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(decoded[5:].strip())
                    except json.JSONDecodeError:
                        continue

                    msg = payload.get("msg") if isinstance(payload, dict) else None

                    if msg == "process_completed":
                        output = payload.get("output", {})
                        if output.get("error"):
                            raise RuntimeError(f"HF Space error: {output['error']}")
                        if not payload.get("success", True):
                            raise RuntimeError(
                                f"HF returned success=false. "
                                f"Payload: {json.dumps(payload)[:500]}"
                            )
                        data = output.get("data", [])
                        result_path = None
                        if data:
                            item = data[0]
                            if isinstance(item, dict):
                                result_path = item.get("path") or item.get("url")
                            else:
                                result_path = str(item)
                        log.info(f"   [{filename}] ✅ process_completed — result: {result_path}")
                        return result_path

                    elif msg == "process_starts":
                        log.info(f"   [{filename}] HF: process started")
                    elif msg == "estimation":
                        log.info(f"   [{filename}] HF: queue position {payload.get('rank', '?')}")
                    elif msg == "process_generating":
                        log.info(f"   [{filename}] HF: generating...")

        except requests.exceptions.ChunkedEncodingError:
            reconnects += 1
            log.warning(f"   [{filename}] SSE stream dropped (response ended prematurely) — reconnecting in {RECONNECT_DELAY}s... ({reconnects}/{MAX_RECONNECTS})")
            time.sleep(RECONNECT_DELAY)
            continue

        except requests.exceptions.ReadTimeout:
            reconnects += 1
            log.warning(f"   [{filename}] SSE read timeout — reconnecting in {RECONNECT_DELAY}s... ({reconnects}/{MAX_RECONNECTS})")
            time.sleep(RECONNECT_DELAY)
            continue

        except Exception as e:
            raise RuntimeError(f"SSE error: {e}")

        # Stream ended without process_completed — reconnect
        reconnects += 1
        log.warning(f"   [{filename}] SSE ended without result — reconnecting in {RECONNECT_DELAY}s... ({reconnects}/{MAX_RECONNECTS})")
        time.sleep(RECONNECT_DELAY)

    raise RuntimeError(f"SSE failed after {MAX_RECONNECTS} reconnection attempts")


def send_to_hf(filepath, filename):
    """
    Gradio 6.x REST flow with SSE reconnection for long-running jobs.

      1. POST /gradio_api/upload       → upload file, get server path
      2. POST /gradio_api/queue/join   → submit job
      3. GET  /gradio_api/queue/data   → SSE stream with auto-reconnect
      4. GET  /gradio_api/file=<path>  → download result
    """

    proxy_name   = f"{uuid.uuid4().hex}.pdf"
    session_hash = uuid.uuid4().hex
    log.info(f"   [{filename}] Proxy: {proxy_name}")

    # --- STEP 1: Upload ---
    server_path = upload_file(filepath, proxy_name)

    # --- STEP 2: Queue join ---
    log.info(f"   [{filename}] Joining queue (session: {session_hash})...")
    queue_resp = requests.post(
        f"{api_base}/queue/join",
        headers=HEADERS_JSON,
        json={
            "fn_index": 0,
            "session_hash": session_hash,
            "data": [{
                "path": server_path,
                "orig_name": proxy_name,
                "mime_type": "application/pdf",
                "size": os.path.getsize(filepath),
                "meta": {"_type": "gradio.FileData"}
            }]
        },
        timeout=60
    )
    queue_resp.raise_for_status()
    log.info(f"   [{filename}] Queued — {queue_resp.json()}")

    # --- STEP 3: Stream SSE with reconnection ---
    start_time  = time.time()
    result_path = poll_sse(session_hash, filename, start_time)

    if not result_path:
        raise RuntimeError("No result path received — check HF Space Logs tab")

    # --- STEP 4: Download ---
    result_url = result_path if result_path.startswith("http") else f"{api_base}/file={result_path}"
    log.info(f"   [{filename}] Downloading from: {result_url}")
    dl = requests.get(result_url, headers=HEADERS, timeout=120)
    dl.raise_for_status()

    return dl.content


def watch_airlock():
    log.info("🏍️  Courier online — watching airlock for packages")
    log.info(f"   HF Space:      {SPACE_NAME}")
    log.info(f"   API Base:      {api_base}")
    log.info(f"   Outbound:      {AIRLOCK_OUT}")
    log.info(f"   Inbound:       {AIRLOCK_IN}")
    log.info(f"   Max wait:      {MAX_WAIT_SECONDS}s")
    log.info(f"   Max reconnects:{MAX_RECONNECTS}")

    while True:
        if os.path.exists(AIRLOCK_OUT):
            for filename in os.listdir(AIRLOCK_OUT):

                if not filename.endswith('.pdf'):
                    continue

                filepath = os.path.join(AIRLOCK_OUT, filename)
                log.info(f"📦 [{filename}] STAGE: Picked up from airlock/outbound")
                log.info(f"🌐 [{filename}] STAGE: Sending to Hugging Face — {SPACE_NAME}")

                try:
                    clean_bytes = send_to_hf(filepath, filename)

                    tmp_inbound   = os.path.join(AIRLOCK_IN, filename + ".tmp")
                    final_inbound = os.path.join(AIRLOCK_IN, filename)

                    with open(tmp_inbound, 'wb') as f:
                        f.write(clean_bytes)
                    os.rename(tmp_inbound, final_inbound)

                    os.remove(filepath)
                    log.info(f"✅ [{filename}] Delivered to airlock/inbound — bouncer will retrieve")

                except Exception as e:
                    log.error(f"💀 [{filename}] Courier FAILED: {e}")
                    retry_path = filepath + ".retry"
                    os.rename(filepath, retry_path)
                    log.warning(f"⏳ [{filename}] Moved to .retry — inspect at: {retry_path}")

        time.sleep(3)


if __name__ == "__main__":
    log.info("⏳ Courier waiting 5s for bouncer to initialise airlock...")
    time.sleep(5)
    watch_airlock()