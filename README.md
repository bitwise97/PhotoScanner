# Photo Scanner Automation

A Python script that automates the end-to-end workflow for digitizing old family photos — from flatbed scanner output to AI-enhanced uploads on Google Drive. The script automatically detects black & white photos and produces a grayscale-enhanced version alongside the color-enhanced version, using the Pillow image library for local image processing.

## What It Does

1. **Picks up scanned JPEGs** from the local scanner output folder (`~/Pictures`)
2. **Detects conflicts** with files already on Google Drive and renames files to avoid duplicates
3. **Detects black & white photos** automatically by measuring color saturation
4. **Enhances each photo** using the Topaz Gigapixel (High Fidelity V2) API by default — restoring clarity and detail while preserving original content without hallucinating new details
5. **xAI fallback** — individual files can be routed to xAI instead of Topaz by prefixing the filename with `xAI_` (see Usage section)
6. **Uploads to Google Drive** — both the original scan and the AI-enhanced version (and a grayscale-enhanced version for B&W photos)
7. **Cleans up** local files after successful upload

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
- A **Topaz API key** set as the environment variable `TOPAZ_API_KEY` (obtain from [developer.topazlabs.com](https://developer.topazlabs.com))
- An **xAI API key** set as the environment variable `XAI_API_KEY` (only required for xAI fallback)
- **Pillow** for black & white photo detection and grayscale image processing
- **launchctl** configured to monitor `~/Pictures` and trigger the script automatically when new scanned files are detected

### Install dependencies

```bash
pip install Pillow google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests
```

## Setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and enable the **Google Drive API**.
2. Create OAuth 2.0 credentials (Desktop app) and save the file as `photo_scanner_automation_credentials.json` in the project folder.
3. Set your Topaz API key as an environment variable:
   ```bash
   export TOPAZ_API_KEY=your_topaz_api_key_here
   ```
4. Optionally, set your xAI API key if you plan to use the xAI fallback:
   ```bash
   export XAI_API_KEY=your_xai_api_key_here
   ```
5. Create `~/.photo-scanner-config.json` with your Google Drive folder ID (see Usage section for details).
6. Configure launchctl to monitor `~/Pictures` and automatically trigger the script when new scanned files appear.
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
(The `~` symbol represents your home directory. To show hidden files in Finder, press `Cmd + Shift + .`)

Then run the script with no parameters:
```bash
python photo_scanner.py
```

If both are provided, the command-line parameter takes precedence.

### xAI Fallback

If you're not satisfied with the Topaz output for a specific photo, you can route it through xAI instead by prefixing the filename with `xAI_` before dropping it in `~/Pictures`:

| Input filename | Processed by | Output filename |
|---|---|---|
| `IMG_20260702_0015.jpg` | Topaz (default) | `IMG_20260702_0015_ai.jpg` |
| `xAI_IMG_20260702_0015_ai.jpg` | xAI (fallback) | `IMG_20260702_0015_ai.jpg` |

The `xAI_` prefix is stripped from the output filename automatically.

## Notes

- Topaz Gigapixel High Fidelity V2 was chosen specifically for its "maximum source preservation" — it enhances what's already in the photo without inventing details or altering faces.
- Only files that complete the full pipeline (original + AI enhancement + upload) are deleted locally.
- The script is safe to re-run — it checks Drive for existing sequence numbers and avoids overwriting files.
- Credential and token files are excluded from this repository via `.gitignore`.
