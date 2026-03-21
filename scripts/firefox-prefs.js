// ============================================================
// Shadow Lab — Firefox Profile Preferences
// Applied on every launch via user.js
// ============================================================

// --- DOWNLOAD DIRECTORY ---
// Point all downloads at the dirty_zone Docker volume mount
user_pref("browser.download.dir", "/downloads");
user_pref("browser.download.folderList", 2);       // 2 = use custom dir above
user_pref("browser.download.useDownloadDir", true); // Never ask where to save

// --- NEVER OPEN / RUN FILES AFTER DOWNLOAD ---
// Disable the "Open file" prompt that appears after download completes
user_pref("browser.download.always_ask_before_handling_new_types", false);
user_pref("browser.download.open_pdf_attachments_inline", false);
user_pref("browser.download.improvements_to_download_panel", false);
user_pref("browser.download.always_ask_before_handling_new_types", true);

// --- DISABLE PDF PREVIEW (most important setting) ---
// Without this, Firefox renders the PDF in-browser before it hits disk.
// A malicious PDF exploit fires HERE, before your bouncer ever sees the file.
user_pref("pdfjs.disabled", true);

// --- DISABLE AUTO-OPEN FOR ALL KNOWN TYPES ---
// Prevents Firefox from handing downloaded files to the OS to open
user_pref("browser.helperApps.neverAsk.saveToDisk",
    "application/pdf;" +
    "application/epub+zip;" +
    "application/octet-stream;" +
    "application/x-download;" +
    "application/force-download;" +
    "binary/octet-stream"
);

// --- PRIVACY & TELEMETRY ---
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.safebrowsing.downloads.remote.enabled", false);
user_pref("browser.safebrowsing.malware.enabled", false); // Local only, no cloud calls
user_pref("media.autoplay.default", 5);                   // Block all autoplay

// --- DISABLE FIRST-RUN NOISE ---
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("trailhead.firstrun.branches", "nofirstrun");
