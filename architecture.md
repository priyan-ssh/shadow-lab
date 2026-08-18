# PROJECT SHADOW LAB
## Secure Document Ingestion Pipeline
### Architecture Specification v1.0

**Classification:** INTERNAL  
**Version:** 1.0  
**Date:** March 2026

---

## 1. Overview
Project Shadow Lab is a hardened document ingestion pipeline designed for safely downloading and sanitising PDF and EPUB files from untrusted internet sources. It is purpose-built for security researchers who need to acquire documents from arbitrary websites without exposing their host system to malicious file payloads, embedded scripts, or exploit-bearing content.

The core design principle is **zero-trust file handling**: every downloaded file is treated as hostile until it has passed through the full sanitisation pipeline. No raw file ever touches the host operating system's filesystem.

---

## 2. System Components

### 2.1 Zones

| Zone | Description |
| :--- | :--- |
| **dirty_zone** | Docker-managed named volume. Receives all raw downloads. Isolated from host OS, WSL, and Windows Defender. Write-only for the browser container; read-write for the bouncer. |
| **clean_library** | Destination for all successfully sanitised files. Can be bind-mounted to a host folder. Only files that have passed full triage reach this zone. |
| **browser_profile** | Persisted Docker volume storing Firefox profile, bookmarks, and extensions (e.g. uBlock Origin) across container restarts. |

### 2.2 Containers

| Container | Role & Network Access |
| :--- | :--- |
| **safe_browser** | Headless Firefox instance launched via X11 forwarding. Renders natively on the host display — no video streaming. Has egress-only internet access. Cannot reach the bouncer container. |
| **bouncer** | Python triage process. Watches `dirty_zone` for new files, classifies them, and routes them through the appropriate sanitisation path. No internet access (`network_mode: none`). |
| **hf_courier** | Online delivery agent. Picks up hostile PDFs from the airlock, sends them to the Hugging Face space for OCR-incineration, and returns a clean PDF/A. |

---

## 3. Data Flow

### 3.1 Lifecycle of a Document

```
User browses (X11 Firefox in container)
         |
         | clicks download — file never previewed in browser
         v
   dirty_zone [Docker volume]  <-- only path into the pipeline
         |
         | bouncer wakes on inotify event
         v
   +----- File Type? -----+
   |                      |
   v                      v
 EPUB                    PDF
   |                      |
   v                      v
 Script Strip          pdfid Scan
 (regex acid wash)         |
   |              +--------+--------+
   |              |                 |
   |           CLEAN             HOSTILE
   |              |                 |
   |         pikepdf soft       HF Incinerator
   |         clean locally      (OCR rebuild)
   |              |                 |
   +------+-------+-----------------+
          |
          v
    clean_library  -->  host bind mount (safe to open)

   dirty_zone original  -->  DELETED after processing
```

### 3.2 Triage Logic

| File Type | Processing Path |
| :--- | :--- |
| **EPUB** | Unzipped in memory. Every `<script>`, `<iframe>`, and inline event handler (`onload`, `onerror`, `onclick`) is stripped via regex. File is re-zipped and written to `clean_library`. |
| **PDF — clean scan** | Soft clean via `pikepdf`: removes JavaScript actions, embedded executables, launch actions, and URI actions. Metadata stripped. Output written to `clean_library`. |
| **PDF — hostile scan** | Escalated to Hugging Face Space endpoint. `ocrmypdf` performs force OCR: original byte stream is discarded, document is rebuilt pixel-by-pixel from rasterised pages. Returns PDF/A. |
| **PDF — corrupt / pikepdf failure** | Automatically escalated to cloud incinerator path regardless of `pdfid` result. |

---

## 4. Browser Isolation Architecture

### 4.1 Why X11 Forwarding Instead of KasmVNC
The `linuxserver/firefox` image (KasmVNC) encodes the entire desktop as a JPEG video stream and delivers it over a WebSocket. This introduces encoding latency, decoding overhead in the host browser, and produces a noticeably laggy experience.

X11 forwarding works differently: the containerised Firefox process emits drawing commands (*"render button at position x,y with colour #hex"*) which are sent over a Unix socket to the host's X server, which then renders them natively using the host GPU. No encoding. No streaming. The result is indistinguishable from running Firefox natively.

| Feature | KasmVNC | X11 Forwarding | Playwright Headless |
| :--- | :--- | :--- | :--- |
| **Browsing feel** | Laggy | Native | No UI |
| **Video playback** | Works | Choppy | N/A |
| **Setup overhead** | Zero | Minimal | Zero |
| **Full browsing** | Yes | Yes | Partial |
| **Display method** | JPEG stream | Draw commands | Headless only |
| **GPU acceleration** | Remote only | Host GPU | None |

### 4.2 Display Setup

#### Windows 11 (WSLg)
WSLg provides a built-in Wayland/X11 compositor. The `DISPLAY` environment variable is set automatically. No additional software required.

```yaml
# In docker-compose.yml — Windows 11
environment:
  - DISPLAY=${DISPLAY}
volumes:
  - /tmp/.X11-unix:/tmp/.X11-unix
  - /mnt/wslg:/mnt/wslg
```

#### Windows 10 (VcXsrv)
Install VcXsrv. Launch with *Multiple Windows* mode, *no root window*, and *Disable Access Control* checked. Then set `DISPLAY` in WSL:

```bash
export DISPLAY=$(cat /etc/resolv.conf | grep nameserver | awk '{print $2}'):0
export LIBGL_ALWAYS_INDIRECT=1
docker compose up browser
```

### 4.3 Firefox Hardening (Baked into Profile)

```javascript
// All PDFs download to dirty_zone, never rendered in-browser
user_pref("pdfjs.disabled", true);
user_pref("browser.download.dir", "/dirty_zone");
user_pref("browser.download.folderList", 2);
user_pref("browser.download.useDownloadDir", true);
user_pref("media.autoplay.default", 5);
user_pref("browser.safebrowsing.downloads.remote.enabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
```

---

## 5. Volume and Filesystem Security

### 5.1 Why Docker Named Volumes Over WSL Bind Mounts
When `dirty_zone` is a WSL folder (e.g. `~/dirty_zone`), several host-side processes can access it before the bouncer:
1. **Windows Defender** scans files written via the `\\wsl$` bridge, potentially opening and parsing malicious content.
2. **Windows Explorer**, other WSL processes, and host applications all have read access.
3. There is no enforced write-only constraint — any process can read back what was written.

Docker named volumes live inside Docker's storage backend (`/var/lib/docker/volumes/`) and are not exposed via the WSL bridge. Windows has no visibility into them.

> [!IMPORTANT]
> `dirty_zone` must be a Docker named volume, not a bind-mount. Only `clean_library` is safe to bind-mount to the host, because files only arrive there after full sanitisation.

### 5.2 Filesystem Permission Model

```bash
# dirty_zone permissions inside containers
chmod 1733 /dirty_zone
#  1 = sticky bit: users cannot delete others' files
#  7 = bouncer_user: full read/write/execute
#  3 = browser_user: write + execute only (cannot read back)

# Bouncer runs as dedicated low-privilege user
useradd -r bouncer_user
chown bouncer_user:bouncer_user /dirty_zone
```

### 5.3 Write Completion Detection
The bouncer uses size-stability polling rather than a fixed sleep to detect when a file has finished writing. This prevents processing a partially-written file:

```python
def wait_for_complete_write(path, interval=0.5, stable_count=3):
    prev_size, count = -1, 0
    while count < stable_count:
        size = os.path.getsize(path)
        count = count + 1 if size == prev_size else 0
        prev_size = size
        time.sleep(interval)
```

---

## 6. Threat Model

### 6.1 Addressed Threats

| Threat | Severity | Mitigation |
| :--- | :--- | :--- |
| **Browser PDF auto-preview fires exploit** | HIGH | `pdfjs.disabled=true` forces download. Browser never renders the PDF. |
| **Malicious EPUB executes JavaScript** | HIGH | All `<script>`, `<iframe>`, and event handler attributes stripped before file exits `dirty_zone`. |
| **Windows Defender opens hostile file** | MED | `dirty_zone` is a Docker volume; invisible to Windows/WSL bridge. |
| **Partially-written file processed early** | LOW | Size-stability polling replaces fixed sleep. File processed only when stable. |
| **Container escape via exploit** | MED | Containers run as non-root. `cap_drop: ALL`, `no-new-privileges:true`. Bouncer has no network. |
| **Compromised file reaches host directly** | LOW | Only `clean_library` is bind-mounted. `dirty_zone` originals are deleted post-processing. |

### 6.2 Remaining Limitations
* Video playback inside the X11 browser container is CPU-only (no GPU passthrough). Not relevant for document downloading.
* Sites requiring CAPTCHAs, logins, or complex JS navigation require manual interaction in the browser — the pipeline handles the file once downloaded.
* The Hugging Face escalation path requires outbound internet from the bouncer for API calls. In fully air-gapped setups, a local `ocrmypdf` endpoint should be substituted.

---

## 7. Docker Compose Reference

```yaml
services:
  safe_browser:
    build: ./browser
    environment:
      - DISPLAY=${DISPLAY}
    volumes:
      - /tmp/.X11-unix:/tmp/.X11-unix
      - /mnt/wslg:/mnt/wslg           # WSLg only
      - dirty_zone:/dirty_zone
      - browser_profile:/home/browser_user/.mozilla
    networks: [egress_only]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    shm_size: '1gb'

  bouncer:
    build: ./bouncer
    volumes:
      - dirty_zone:/dirty_zone
      - ./clean_library:/clean_library  # bind-mount safe here
    network_mode: none
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]

volumes:
  dirty_zone:    # Docker-managed, invisible to host OS
  browser_profile:

networks:
  egress_only:
    driver: bridge
    internal: false
```

---

## 8. Security Posture Summary

| What Is Protected | How |
| :--- | :--- |
| **Host OS filesystem** | `dirty_zone` is a Docker volume, never a host path |
| **Windows Defender touching hostile files** | Docker volumes not accessible via `\\wsl$` |
| **Browser rendering malicious PDF** | `pdfjs` disabled; PDF never previewed |
| **EPUB JavaScript execution** | Script strip before file exits `dirty_zone` |
| **Container escape** | Non-root, `cap_drop ALL`, `no-new-privileges` |
| **Network pivot from bouncer** | `network_mode: none` on bouncer container |
| **Persistent hostile file on disk** | `dirty_zone` originals deleted after processing |
