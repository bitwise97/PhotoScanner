# Photo Scanner Automation

A Python script that automates the end-to-end workflow for digitizing old family photos — from flatbed scanner output to AI-enhanced uploads on Google Drive. The script automatically detects black & white photos and produces a grayscale-enhanced version alongside the color-enhanced version, using the Pillow image library for local image processing.

## What It Does

1. **Picks up scanned JPEGs** from the local scanner output folder (`~/Pictures`)
2. **Detects conflicts** with files already on Google Drive and renames files to avoid duplicates
3. **Detects black & white photos** automatically by measuring color saturation
4. **Enhances each photo** using the xAI image editing API (Grok) — restoring color, removing scratches, improving clarity — while preserving faces exactly as-is
5. **Uploads to Google Drive** — both the original scan and the AI-enhanced version (and a grayscale-enhanced version for B&W photos)
6. **Cleans up** local files after successful upload

## Output Files Per Photo

| File | Description |
|---|---|
| `IMG_YYYYMMDD_NNNN.jpg` | Original scan |
| `IMG_YYYYMMDD_NNNN_ai.jpg` | AI-enhanced color version |
| `IMG_YYYYMMDD_NNNN_bw_ai.jpg` | AI-enhanced grayscale version (B&W photos only) |

## Requirements

- Python 3.7+
- A Google Cloud project with the **Google Drive API** enabled
- OAuth 2.0 credentials file downloaded from the Google Cloud Console
- An **xAI API key** set as the environment variable `XAI_API_KEY`
- **Pillow** for black & white photo detection and grayscale image processing
- **launchctl** configured to monitor `~/Pictures` and trigger the script automatically when new scanned files are detected

### Install dependencies

```bash
pip install Pillow google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests
```

## Setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and enable the **Google Drive API**.
2. Create OAuth 2.0 credentials (Desktop app) and save the file as `photo_scanner_automation_credentials.json` in the project folder.
3. Set your xAI API key as an environment variable:
   ```bash
   export XAI_API_KEY=your_api_key_here
   ```
4. Create `~/.photo-scanner-config.json` with your Google Drive folder ID (see Usage section for details).
5. Configure launchctl to monitor `~/Pictures` and automatically trigger the script when new scanned files appear.
   The launchctl plist should run: `python /path/to/photo_scanner.py` (no parameters needed — it reads from the config file).

On first run, a browser window will open to authorize Google Drive access. The token is saved locally and refreshed automatically. If the token expires (e.g. after 7 days in test mode), the script will open the browser to re-authorize rather than failing.

## Usage

Place scanned photos in `~/Pictures`, then run:

```bash
python photo_scanner.py --folder-id <DRIVE_FOLDER_ID>
```

### Configuration

The script can be configured in two ways:

#### Option 1: Command-line parameter
```bash
python photo_scanner.py --folder-id 1R5UhpYBe2nzZaf5T8qtAhHha76ajhRhO
```

#### Option 2: Config file (for automated/launchctl use)

Create `~/.photo-scanner-config.json` in your home directory (e.g., `/Users/sreynoso/.photo-scanner-config.json`):
```json
{
  "folder_id": "1R5UhpYBe2nzZaf5T8qtAhHha76ajhRhO"
}
```
(The `~` symbol represents your home directory.)

Then run the script with no parameters:
```bash
python photo_scanner.py
```

If both are provided, the command-line parameter takes precedence.

The script processes all `IMG_*.jpg` files it finds, uploads them to Drive, and deletes the local copies upon success. Files where AI enhancement fails are kept locally and not uploaded.

## Notes

- The AI enhancement prompt is tuned specifically for old family photos — it preserves faces exactly, removes color casts and scratches, and boosts color vibrancy to simulate a modern digital camera look.
- Only files that complete the full pipeline (original + AI enhancement + upload) are deleted locally.
- The script is safe to re-run — it checks Drive for existing sequence numbers and avoids overwriting files.
- Credential and token files are excluded from this repository via `.gitignore`.
