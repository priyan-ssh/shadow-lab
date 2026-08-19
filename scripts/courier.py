import os
import time
import uuid
import json
import logging
import requests
import re

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

if not HF_TOKEN or not SPACE_NAME or SPACE_NAME == "your_username/your_space_name":
    raise RuntimeError(
        "HF_TOKEN and HF_SPACE_NAME must be set in your .env file.\n"
        "  HF_TOKEN=hf_yourtoken\n"
        "  HF_SPACE_NAME=your_username/your_space_name"
    )

if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+", SPACE_NAME):
    raise RuntimeError("HF_SPACE_NAME must be 'owner/space_name' — no slashes, protocols, or dots.")

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
                        if "success" not in payload or payload.get("success") is not True:
                            raise RuntimeError(
                                f"HF did not explicitly report success=true. "
                                f"Payload: {json.dumps(payload)[:500]}"
                            )
                        data = output.get("data", [])
                        clean_path = None
                        log_path = None
                        if len(data) >= 2:
                            item0 = data[0]
                            item1 = data[1]
                            clean_path = item0.get("path") or item0.get("url") if isinstance(item0, dict) else str(item0)
                            log_path = item1.get("path") or item1.get("url") if isinstance(item1, dict) else str(item1)
                        elif len(data) >= 1:
                            item0 = data[0]
                            clean_path = item0.get("path") or item0.get("url") if isinstance(item0, dict) else str(item0)
                        log.info(f"   [{filename}] ✅ process_completed — clean result: {clean_path}, log: {log_path}")
                        return clean_path, log_path

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
    clean_path, log_path = poll_sse(session_hash, filename, start_time)

    if not clean_path:
        raise RuntimeError("No result path received — check HF Space Logs tab")

    # --- STEP 4: Download ---
    result_url = clean_path if clean_path.startswith("http") else f"{api_base}/file={clean_path}"
    log.info(f"   [{filename}] Downloading clean PDF from: {result_url}")
    dl = requests.get(result_url, headers=HEADERS, timeout=120)
    dl.raise_for_status()

    content_type = dl.headers.get("content-type", "")
    if "pdf" not in content_type.lower():
        raise RuntimeError(f"Unexpected content-type from server: {content_type!r} — refusing to write")
    if not dl.content.startswith(b"%PDF-"):
        raise RuntimeError("Response does not start with PDF magic bytes — refusing to write")

    log_content = None
    if log_path:
        log_url = log_path if log_path.startswith("http") else f"{api_base}/file={log_path}"
        log.info(f"   [{filename}] Downloading log from: {log_url}")
        try:
            dl_log = requests.get(log_url, headers=HEADERS, timeout=60)
            dl_log.raise_for_status()
            log_content = dl_log.content
        except Exception as e:
            log.warning(f"⚠️  [{filename}] Failed to download log file: {e}")

    return dl.content, log_content


def watch_airlock():
    log.info("🏍️  Courier online — watching airlock for packages")
    log.info(f"   HF Space:      {SPACE_NAME}")
    log.info(f"   API Base:      {api_base}")
    log.info(f"   Outbound:      {AIRLOCK_OUT}")
    log.info(f"   Inbound:       {AIRLOCK_IN}")
    log.info(f"   Max wait:      {MAX_WAIT_SECONDS}s")
    log.info(f"   Max reconnects:{MAX_RECONNECTS}")

    while True:
        # Self-healing for stuck .retry files
        import glob
        STALE_SECONDS = 600
        if os.path.exists(AIRLOCK_OUT):
            for f in glob.glob(os.path.join(AIRLOCK_OUT, "*.retry")):
                try:
                    if time.time() - os.path.getmtime(f) > STALE_SECONDS:
                        log.warning(f"⚠️ Purging stale retry file: {f}")
                        os.remove(f)
                except Exception as e:
                    log.error(f"Failed to check/remove stale retry file {f}: {e}")

        if os.path.exists(AIRLOCK_OUT):
            for filename in os.listdir(AIRLOCK_OUT):

                if not filename.endswith('.pdf'):
                    continue

                filepath = os.path.join(AIRLOCK_OUT, filename)
                log.info(f"📦 [{filename}] STAGE: Picked up from airlock/outbound")
                log.info(f"🌐 [{filename}] STAGE: Sending to Hugging Face — {SPACE_NAME}")

                try:
                    clean_bytes, log_bytes = send_to_hf(filepath, filename)

                    tmp_inbound   = os.path.join(AIRLOCK_IN, filename + ".tmp")
                    final_inbound = os.path.join(AIRLOCK_IN, filename)

                    with open(tmp_inbound, 'wb') as f:
                        f.write(clean_bytes)
                    os.rename(tmp_inbound, final_inbound)

                    if log_bytes:
                        log_filename = filename.replace('.pdf', '_log.txt')
                        tmp_log_inbound   = os.path.join(AIRLOCK_IN, log_filename + ".tmp")
                        final_log_inbound = os.path.join(AIRLOCK_IN, log_filename)
                        with open(tmp_log_inbound, 'wb') as f:
                            f.write(log_bytes)
                        os.rename(tmp_log_inbound, final_log_inbound)

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