# Shadow Lab — Secure Document Ingestion Pipeline

Shadow Lab is a hardened, containerised document ingestion pipeline designed to safely download, scan, and sanitise untrusted documents (PDFs and EPUBs) from the internet. It is engineered specifically for security researchers and analysts who need to investigate potentially hostile documents without exposing their host system to exploits or embedded payloads.

The project employs a **zero-trust** isolation model: no raw/untrusted file ever touches the host operating system's filesystem, and all processes are isolated via Docker containers with dropped capabilities (`cap_drop: ALL`, `no-new-privileges:true`).

---

## Architecture Overview

Shadow Lab is divided into three isolated zones:
1. **`safe_browser` Container:** A sandboxed Firefox instance running with egress-only internet access. Downloads are deposited directly into a Docker named volume (`dirty_zone`), which is inaccessible to the host.
2. **`local_bouncer` Container:** A completely offline processing agent (`network_mode: none`) that monitors `dirty_zone`, scans files using Didier Stevens' `pdfid`, and performs local cleaning (`pikepdf` for clean PDFs, chemical strip for EPUBs). Hostile PDFs are escalated to the airlock for the courier.
3. **`hf_courier` Container:** An online agent that picks up escalated hostile PDFs from the airlock, sends them to a private/personal Hugging Face Space for OCR incineration (pixel-by-pixel rebuild), and downloads the sanitised output.

For a detailed breakdown of components and security boundaries, refer to [architecture.md](file:///home/priyansh/coding/shadowlab/shadow-lab/architecture.md).

---

## Setup & Configuration

### Prerequisites
* **Docker & Docker Compose:** Installed on the host system.
* **X11 display server:**
  * **Linux:** Uses your native X server.
  * **Windows 11 (WSL2):** Built-in WSLg display server works automatically.
  * **Windows 10 (WSL2):** Requires VcXsrv or similar.

### 1. Environment Configuration
Copy the template configuration file to `.env`:
```bash
cp env.example .env
```
Open `.env` and fill in your details:
* `HF_SPACE_NAME`: Your username and private Hugging Face Space name (e.g. `myusername/my-incinerator-space`).
* `HF_TOKEN`: A fine-grained, read-only API token generated in your Hugging Face Account Settings.
  > [!IMPORTANT]
  > For security, restrict the token scope exclusively to **Read** and **Inference** on your specific Space. Avoid using write/admin tokens.

### 2. Configure Host X11 Display
Before booting the browser container, the X11 server must allow connections.

* **Linux:**
  ```bash
  xhost +local:docker
  ```
* **Windows 11 (WSLg):**
  No extra configuration needed. `DISPLAY` is forwarded automatically.
* **Windows 10 (VcXsrv):**
  Launch VcXsrv with options: *Multiple Windows*, *Start no client*, and *Disable Access Control* checked. Ensure you set the `DISPLAY` variable in your WSL shell:
  ```bash
  export DISPLAY=$(cat /etc/resolv.conf | grep nameserver | awk '{print $2}'):0
  export LIBGL_ALWAYS_INDIRECT=1
  ```

---

## How to Run & Use Shadow Lab

### 1. Build and Start the Pipeline
Run the following command to build the Docker images and start the services in the background:
```bash
docker compose up -d
```
This launches:
* The isolated bouncer watching for files.
* The courier listening for escalations.
* The sandboxed Firefox browser.

### 2. Browse and Download Safely
Once Docker compose is up, Firefox will launch natively on your host's screen.
1. Use the sandboxed Firefox to browse to your source and click download.
2. The download will land in the isolated `dirty_zone` volume.
3. The offline bouncer instantly detects the write, performs triage, and deletes the raw file from `dirty_zone`.
4. Sanitised files will automatically appear on your host machine inside:
   ```
   ./clean_library/
   ```

### 3. Testing Hugging Face OCR Flow Directly
To test your Hugging Face Space configuration and the cloud endpoint interaction directly from the host system, you can use the standalone test utility:

```bash
# Ensure dependencies are installed (requires requests)
pip install requests

# Run the test utility (will open a file picker on your host)
python test_hf.py
```
This script lets you pick a local PDF, uploads it as a randomized proxy file, waits for the Space to force-OCR it, performs integrity/PDF magic bytes checks, and writes the clean PDF/A next to your original file.

---

## Maintenance & Recovery

### Watchdog and Self-Healing
If a network error or API timeout occurs while the courier processes a hostile file, it renames the file to `.retry` in the airlock to prevent endless loops.
* **Automatic Cleanup:** The courier features a built-in watchdog that automatically purges `.retry` files older than 10 minutes (600 seconds) to prevent queue jamming.
* **Manual Purge:** If you need to immediately nuke all stuck retries, run:
  ```bash
  docker exec local_bouncer sh -c "rm -f /airlock/outbound/*.retry"
  ```
