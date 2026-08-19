import os
import time
import subprocess
import re
import zipfile
import shutil
import logging
import pikepdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BOUNCER] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

INPUT_DIR   = "/input"
OUTPUT_DIR  = "/output"
AIRLOCK_OUT = "/airlock/outbound"
AIRLOCK_IN  = "/airlock/inbound"
PDFID_PATH  = "/usr/local/bin/pdfid.py"

# Max size an EPUB is allowed to expand to when unzipped
# Protects against zip bomb attacks
MAX_EPUB_EXTRACTED_MB = 500

# Files that have no business being inside an EPUB
DANGEROUS_EXTENSIONS = (
    '.pdf', '.js', '.exe', '.zip', '.swf', '.jar',
    '.bat', '.sh', '.dll', '.vbs', '.ps1', '.py', '.rb'
)


def setup():
    for d in [AIRLOCK_OUT, AIRLOCK_IN, OUTPUT_DIR]:
        os.makedirs(d, exist_ok=True)
        log.info(f"   Directory ready: {d}")


def wait_until_written(path, interval=0.5, stable_count=3, max_attempts=1200):
    prev, count = -1, 0
    attempts = 0
    while count < stable_count and attempts < max_attempts:
        attempts += 1
        
        # If the browser is still writing to a temporary part file, the download is not done
        if os.path.exists(path + ".part") or os.path.exists(path + ".tmp"):
            count = 0
            time.sleep(interval)
            continue
            
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            return False
            
        # A completed PDF/EPUB must have a size greater than 0 bytes
        if size == prev and size > 0:
            count += 1
        else:
            count = 0
            
        prev = size
        time.sleep(interval)
    return count >= stable_count


# ─── PDF TRIAGE ───────────────────────────────────────────────────────────────

def is_guilty(filepath):
    filename = os.path.basename(filepath)
    log.info(f"🔍 [{filename}] Scanning for hostile tags...")
    red_flags = ['/JS', '/JavaScript', '/AA', '/OpenAction', '/Launch']
    try:
        result = subprocess.run(
            ["python", PDFID_PATH, "-n", filepath],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        for flag in red_flags:
            if re.search(rf"{re.escape(flag)}\s+([1-9]\d*)", result.stdout):
                log.warning(f"🚨 [{filename}] HOSTILE tag '{flag}' found")
                return True
        log.info(f"✅ [{filename}] Scan clean — no hostile tags found")
        return False
    except Exception as e:
        log.error(f"⚠️  [{filename}] Scanner glitch: {e} — presuming GUILTY")
        return True


# ─── PDF SOFT CLEAN (pikepdf) ─────────────────────────────────────────────────

def soft_clean(filepath, filename):
    log.info(f"🔪 [{filename}] STAGE: Soft Clean (pikepdf)")
    output_path = os.path.join(OUTPUT_DIR, filename)
    try:
        with pikepdf.open(filepath) as pdf:
            if '/OpenAction' in pdf.Root:
                del pdf.Root.OpenAction
                log.info(f"   [{filename}] Stripped: /OpenAction")
            if '/Names' in pdf.Root and '/JavaScript' in pdf.Root.Names:
                del pdf.Root.Names.JavaScript
                log.info(f"   [{filename}] Stripped: /JavaScript names")
            pdf.save(output_path)
            os.chmod(output_path, 0o666)

        log.info(f"✅ [{filename}] Soft Clean SUCCESS — written to clean_library")
        os.remove(filepath)
        return True

    except Exception as e:
        log.warning(f"⚠️  [{filename}] Soft Clean FAILED on '{filepath}': {e} — escalating to OCR")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


# ─── PDF OCR INCINERATION (local ocrmypdf) ───────────────────────────────────

def ocr_incinerate(filepath, filename):
    """
    Nuclear option — rasterises every page and rebuilds from scratch.
    Destroys ALL embedded content: JS, exploits, metadata, everything.
    Runs locally first — falls back to HF courier if local fails.
    """
    log.info(f"🔥 [{filename}] STAGE: Local OCR Incineration")
    output_path = os.path.join(OUTPUT_DIR, filename)

    cmd = [
        "ocrmypdf",
        "--force-ocr",
        "--optimize", "1",
        "--clean",
        "--output-type", "pdfa",
        filepath,
        output_path
    ]

    log.info(f"   [{filename}] Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        log.warning(f"⚠️  [{filename}] 'ocrmypdf' command not found locally — escalating to HF courier")
        return False
    except Exception as e:
        log.error(f"💀 [{filename}] Local OCR execution failed: {e} — escalating to HF courier")
        return False

    stdout = result.stdout.decode(errors='replace')
    stderr = result.stderr.decode(errors='replace')
    if stdout: log.info(f"   [{filename}] stdout: {stdout[:300]}")
    if stderr: log.info(f"   [{filename}] stderr: {stderr[:300]}")

    if result.returncode != 0:
        log.error(f"💀 [{filename}] Local OCR FAILED (exit {result.returncode}) — escalating to HF courier")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False

    os.chmod(output_path, 0o666)
    log.info(f"✅ [{filename}] Local OCR SUCCESS — clean PDF/A written to clean_library")
    os.remove(filepath)
    return True


# ─── EPUB CHEMICAL PEEL ───────────────────────────────────────────────────────

def scrub_epub(filepath, filename):
    log.info(f"📖 [{filename}] STAGE: EPUB Chemical Peel")
    temp_dir    = os.path.join(INPUT_DIR, "temp_epub")
    output_path = os.path.join(OUTPUT_DIR, filename)

    try:
        with zipfile.ZipFile(filepath, 'r') as zip_ref:

            # ── ZIP BOMB PROTECTION ───────────────────────────────────────
            total_size = sum(f.file_size for f in zip_ref.infolist())
            max_bytes  = MAX_EPUB_EXTRACTED_MB * 1024 * 1024
            if total_size > max_bytes:
                raise ValueError(
                    f"EPUB would extract to {total_size // 1024 // 1024} MB "
                    f"(limit: {MAX_EPUB_EXTRACTED_MB} MB) — possible zip bomb, nuking."
                )
            log.info(f"   [{filename}] Zip bomb check passed — extracted size: {total_size // 1024} KB")
            # ─────────────────────────────────────────────────────────────

            zip_ref.extractall(temp_dir)

        log.info(f"   [{filename}] Unpacked — scanning all files...")

        stripped_scripts  = 0
        stripped_handlers = 0
        stripped_iframes  = 0
        stripped_embeds   = 0
        stripped_css      = 0
        stripped_xxe      = 0

        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                full_path = os.path.join(root, file)

                # --- Remove dangerous embedded files entirely ---
                if file.lower().endswith(DANGEROUS_EXTENSIONS):
                    os.remove(full_path)
                    stripped_embeds += 1
                    log.warning(f"   [{filename}] Removed embedded: {file}")
                    continue

                # --- Strip XML/OPF/NCX files of XXE declarations ---
                if file.lower().endswith(('.opf', '.ncx', '.xml')):
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    new_content = re.sub(r'<!DOCTYPE[^>]*>', '', content, flags=re.IGNORECASE | re.DOTALL)
                    new_content = re.sub(r'<!ENTITY[^>]*>', '', new_content, flags=re.IGNORECASE)
                    if new_content != content:
                        stripped_xxe += 1
                        with open(full_path, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                    continue

                # --- Strip HTML/XHTML/SVG files ---
                if file.lower().endswith(('.html', '.xhtml', '.htm', '.svg')):
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()

                    new_content = re.sub(r'<script.*?>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
                    stripped_scripts += len(re.findall(r'<script', content, re.IGNORECASE))

                    new_content = re.sub(r'\s+on\w+="[^"]*"', '', new_content, flags=re.IGNORECASE)
                    new_content = re.sub(r"\s+on\w+='[^']*'", '', new_content, flags=re.IGNORECASE)
                    stripped_handlers += len(re.findall(r'\s+on\w+=', content, re.IGNORECASE))

                    new_content = re.sub(r'<iframe.*?>.*?</iframe>', '', new_content, flags=re.DOTALL | re.IGNORECASE)
                    stripped_iframes += len(re.findall(r'<iframe', content, re.IGNORECASE))

                    # javascript: protocol in hrefs/srcs
                    new_content = re.sub(r'(href|src|action)\s*=\s*["\']javascript:[^"\']*["\']', '', new_content, flags=re.IGNORECASE)

                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)

                # --- Strip CSS files of dangerous imports and javascript: urls ---
                elif file.lower().endswith('.css'):
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()

                    new_content = re.sub(r'@import\s+url\([^)]+\)', '', content, flags=re.IGNORECASE)
                    new_content = re.sub(r'url\(["\']?javascript:[^)]+\)', '', new_content, flags=re.IGNORECASE)
                    new_content = re.sub(r'expression\s*\(.*?\)', '', new_content, flags=re.IGNORECASE | re.DOTALL)

                    if new_content != content:
                        stripped_css += 1
                        with open(full_path, 'w', encoding='utf-8') as f:
                            f.write(new_content)

        log.info(
            f"   [{filename}] Stripped — "
            f"scripts: {stripped_scripts}, "
            f"handlers: {stripped_handlers}, "
            f"iframes: {stripped_iframes}, "
            f"embeds: {stripped_embeds}, "
            f"css: {stripped_css}, "
            f"xxe: {stripped_xxe}"
        )
        log.info(f"   [{filename}] Repacking EPUB...")

        shutil.make_archive(output_path.replace('.epub', ''), 'zip', temp_dir)
        os.rename(output_path.replace('.epub', '.zip'), output_path)
        os.chmod(output_path, 0o666)
        shutil.rmtree(temp_dir)
        os.remove(filepath)
        log.info(f"✅ [{filename}] EPUB Chemical Peel SUCCESS — written to clean_library")

    except Exception as e:
        log.error(f"💀 [{filename}] EPUB Peel FAILED: {e} — file nuked")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        if os.path.exists(filepath):
            os.remove(filepath)


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def watch_folder():
    setup()
    log.info("🕶️  Bouncer online — network: NONE — vault sealed")
    log.info(f"   Watching: {INPUT_DIR}")
    log.info(f"   Output:   {OUTPUT_DIR}")
    log.info(f"   Airlock:  {AIRLOCK_OUT} / {AIRLOCK_IN}")
    log.info(f"   Zip bomb limit: {MAX_EPUB_EXTRACTED_MB} MB")

    while True:
        os.makedirs(AIRLOCK_IN, exist_ok=True)
        os.makedirs(AIRLOCK_OUT, exist_ok=True)

        # 1. Collect returning cloud-cleaned files from courier
        for filename in os.listdir(AIRLOCK_IN):
            if filename.endswith(('.pdf', '.txt')):
                src = os.path.join(AIRLOCK_IN, filename)
                dst = os.path.join(OUTPUT_DIR, filename)
                shutil.move(src, dst)
                os.chmod(dst, 0o666)
                log.info(f"📥 [{filename}] Retrieved from airlock — written to clean_library")

        # 2. Process new dirty files
        for filename in os.listdir(INPUT_DIR):
            filepath = os.path.join(INPUT_DIR, filename)

            if os.path.isdir(filepath):
                continue
            if filename.endswith(('.part', '.tmp', '.retry')):
                continue

            log.info(f"📂 [{filename}] New file detected in dirty_zone")
            if not wait_until_written(filepath):
                log.warning(f"⚠️  [{filename}] Write timeout or file disappeared — skipping processing for now")
                continue
            log.info(f"   [{filename}] Write complete — starting triage")

            if filename.endswith(".pdf"):
                log.info(f"📄 [{filename}] STAGE: PDF Triage")

                if is_guilty(filepath):
                    log.info(f"   [{filename}] Hostile — attempting local OCR incineration")
                    if not ocr_incinerate(filepath, filename):
                        log.info(f"🚀 [{filename}] Local OCR failed — escalating to HF courier")
                        tmp_path   = os.path.join(AIRLOCK_OUT, filename + ".tmp")
                        final_path = os.path.join(AIRLOCK_OUT, filename)
                        shutil.move(filepath, tmp_path)
                        os.rename(tmp_path, final_path)
                        log.info(f"   [{filename}] Placed in airlock — awaiting courier pickup")
                else:
                    if not soft_clean(filepath, filename):
                        log.info(f"   [{filename}] Soft clean failed — trying local OCR")
                        if not ocr_incinerate(filepath, filename):
                            log.info(f"🚀 [{filename}] All local methods failed — escalating to HF courier")
                            tmp_path   = os.path.join(AIRLOCK_OUT, filename + ".tmp")
                            final_path = os.path.join(AIRLOCK_OUT, filename)
                            shutil.move(filepath, tmp_path)
                            os.rename(tmp_path, final_path)
                            log.info(f"   [{filename}] Placed in airlock — awaiting courier pickup")

            elif filename.endswith(".epub"):
                scrub_epub(filepath, filename)

            else:
                log.warning(f"🚨 [{filename}] Rejecting unknown filetype — file nuked for safety")
                try:
                    os.remove(filepath)
                except Exception as e:
                    log.error(f"Failed to remove rejected file {filepath}: {e}")

        time.sleep(5)


if __name__ == "__main__":
    watch_folder()