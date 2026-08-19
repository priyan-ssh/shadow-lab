import gradio as gr
import subprocess
import logging
import os
import time
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HF-INCINERATOR] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ocrmypdf stderr keywords → human readable progress stages
PROGRESS_STAGES = [
    ("start worker",   5,  "🔄 Initialising workers..."),
    ("reading",        10, "📖 Reading PDF pages..."),
    ("image",          15, "🖼️  Extracting page images..."),
    ("unpaper",        25, "🧹 Cleaning pages (unpaper)..."),
    ("tesseract",      35, "🔍 Starting OCR (Tesseract)..."),
    ("ocr",            40, "🔍 Running OCR on pages..."),
    ("page",           50, "📄 Processing pages..."),
    ("combining",      70, "🔗 Combining OCR results..."),
    ("optimize",       80, "⚡ Optimising output..."),
    ("pdfa",           85, "📋 Converting to PDF/A..."),
    ("validat",        90, "✔️  Validating PDF/A compliance..."),
    ("lineariz",       93, "📐 Linearising PDF..."),
    ("writing",        95, "💾 Writing output file..."),
    ("done",           99, "🏁 Finalising..."),
]

# Per-page detail lines that add noise — skip these
SKIP_KEYWORDS = [
    "resolution", "pil format", "imgformat", "input dpi",
    "rotation", "colorspace", "width x height", "convert",
    "emplacement", "rasterize", "license gplv2", "free software",
    "no warranty", "---", "output-file", "sheet size",
    "noise-filter", "blur-filter", "pikepdf mmap",
    "gathering info", "starting processing",
]


def parse_progress(line):
    line_lower = line.lower()
    for keyword, pct, label in PROGRESS_STAGES:
        if keyword in line_lower:
            return pct, label
    return None, None


def should_skip(line):
    line_lower = line.lower()
    return any(k in line_lower for k in SKIP_KEYWORDS)


def stream_ocrmypdf(cmd, filename, job_logs):
    """
    Run ocrmypdf, stream stderr line by line, log clean progress.
    FIX: last_pct uses nonlocal so it actually tracks state across lines.
    FIX: noisy per-page detail lines are filtered out.
    Returns (returncode, full_stderr).
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    stderr_lines = []
    last_pct = 0

    def read_stderr():
        nonlocal last_pct  # FIX: must be nonlocal to actually update

        for line in process.stderr:
            line = line.rstrip()
            if not line:
                continue
            stderr_lines.append(line)

            # FIX: skip noisy per-page detail lines
            if should_skip(line):
                continue
            if "jbig2" in line.lower():
                continue
            if "warning" in line.lower():
                continue

            pct, label = parse_progress(line)
            if pct and pct > last_pct:
                last_pct   = pct  # FIX: actually update tracked progress
                bar_filled = int(pct / 5)
                bar_empty  = 20 - bar_filled
                bar        = "█" * bar_filled + "░" * bar_empty
                msg = f"   [{filename}] [{bar}] {pct:>3}% — {label}"
                log.info(msg)
                job_logs.append(f"{time.strftime('%H:%M:%S')} {msg}")
            elif pct is None:
                # Log remaining meaningful lines without a bar
                msg = f"   [{filename}] ℹ️  {line}"
                log.info(msg)
                job_logs.append(f"{time.strftime('%H:%M:%S')} {msg}")

    stderr_thread = threading.Thread(target=read_stderr)
    stderr_thread.start()

    process.stdout.read()
    process.wait()
    stderr_thread.join()

    return process.returncode, "\n".join(stderr_lines)


def nuke_it(pdf_file):
    start_time = time.time()
    job_logs = []

    def job_log(msg):
        log.info(msg)
        job_logs.append(f"{time.strftime('%H:%M:%S')} {msg}")

    if pdf_file is None:
        raise gr.Error("No file received")

    filepath = pdf_file if isinstance(pdf_file, str) else pdf_file.get("path")
    if not filepath or not os.path.exists(filepath):
        raise gr.Error(f"File not found at path: {filepath}")

    filename   = os.path.basename(filepath)
    input_size = os.path.getsize(filepath) / 1024

    job_log("=" * 60)
    job_log(f"🔥 JOB START: {filename}")
    job_log(f"   Input size: {input_size:.1f} KB")
    job_log("=" * 60)

    output_path = f"/tmp/clean_{filename}"
    log_file_path = f"/tmp/log_{filename}.txt"

    cmd = [
        "ocrmypdf",
        "--force-ocr",
        "--optimize", "1",
        "--clean",
        "--pdfa-image-compression", "jpeg",
        "--output-type", "pdfa",
        "--jobs", "2",
        "--verbose", "1",
        filepath,
        output_path
    ]

    job_log("⚙️  Running ocrmypdf with live progress...")
    job_log(f"   [░░░░░░░░░░░░░░░░░░░░]   0% — Starting...")

    returncode, full_stderr = stream_ocrmypdf(cmd, filename, job_logs)

    if returncode != 0:
        job_log(f"💀 ocrmypdf FAILED — exit {returncode}")
        job_log(f"   stderr: {full_stderr[-500:]}")
        with open(log_file_path, "w", encoding="utf-8") as lf:
            lf.write("\n".join(job_logs))
        raise gr.Error(f"ocrmypdf failed (exit {returncode}): {full_stderr[-300:]}")

    if not os.path.exists(output_path):
        raise gr.Error("ocrmypdf exited 0 but output file is missing")

    output_size = os.path.getsize(output_path) / 1024
    duration    = time.time() - start_time

    job_log(f"   [████████████████████] 100% — Complete!")
    job_log("=" * 60)
    job_log(f"✅ JOB COMPLETE: {filename}")
    job_log(f"   Input:    {input_size:.1f} KB")
    job_log(f"   Output:   {output_size:.1f} KB")
    job_log(f"   Ratio:    {output_size / input_size:.2f}x")
    job_log(f"   Duration: {duration:.1f}s")
    job_log("=" * 60)

    with open(log_file_path, "w", encoding="utf-8") as lf:
        lf.write("\n".join(job_logs))

    return output_path, log_file_path


with gr.Blocks(title="Shadow Lab Incinerator") as demo:
    gr.Markdown("## 🔥 Shadow Lab Incinerator")
    gr.Markdown("Force-OCR PDF rebuild. Original byte stream fully discarded and reconstructed as PDF/A.")

    with gr.Row():
        input_file  = gr.File(label="Hostile PDF",    file_types=[".pdf"])
        with gr.Column():
            output_file = gr.File(label="Sanitised PDF/A")
            log_file    = gr.File(label="Incinerator Event Log")

    btn = gr.Button("Incinerate", variant="primary")
    btn.click(
        fn=nuke_it,
        inputs=input_file,
        outputs=[output_file, log_file],
        api_name="nuke_it"
    )

demo.launch(
    server_name="0.0.0.0",
    ssr_mode=False
)