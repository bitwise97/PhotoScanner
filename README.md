# Photo Scanner Automation

A Python script that automates the end-to-end workflow for digitizing old family photos — from flatbed scanner output to AI-enhanced uploads on Google Drive. The script automatically detects black & white photos and produces a grayscale-enhanced version alongside the color-enhanced version, using the Pillow image library for local image processing.

## What It Does

1. **Picks up scanned JPEGs** from the local scanner output folder (`~/Pictures`)
2. **Detects conflicts** with files already on Google Drive and renames files to avoid duplicates
3. **Detects black & white photos** automatically by measuring color saturation
4. **Removes dust and scratches** using a Pillow MedianFilter (size=5) pre-processing step before AI enhancement
5. **Enhances each photo** using xAI — correcting color casts, lifting shadows, boosting vibrance, and producing a modern, iPhone-like result while strictly preserving all facial features
6. **Topaz fallback** — individual files can be routed to Topaz instead of xAI by prefixing the filename with `topaz_` (see Usage section)
7. **Uploads to Google Drive** — both the original scan and the AI-enhanced version (and a grayscale-enhanced version for B&W photos)
8. **Cleans up** local files after successful upload

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
- A **Topaz API key** (obtain from [developer.topazlabs.com](https://developer.topazlabs.com))
- An **xAI API key** (only required for xAI fallback)
- **Pillow** for black & white photo detection and grayscale image processing
- **launchctl** configured to monitor `~/Pictures` and trigger the script automatically when new scanned files are detected

### Install dependencies

```bash
pip install Pillow google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests
```

## Setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and enable the **Google Drive API**.
2. Create OAuth 2.0 credentials (Desktop app) and save the file as `photo_scanner_automation_credentials.json` in the project folder.
3. Create `~/.photo-scanner-config.json` in your home directory with your settings:
   ```json
   {
     "folder_id": "your_google_drive_folder_id",
     "topaz_api_key": "your_topaz_api_key",
     "xai_api_key": "your_xai_api_key"
   }
   ```
   The `xai_api_key` is optional — only needed if you use the xAI fallback feature.
4. Configure launchctl to monitor `~/Pictures` and automatically trigger the script when new scanned files appear.
   The launchctl plist should run: `python /path/to/photo_scanner.py` (no parameters needed — all settings are read from the config file).

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

#### Option 2: Config file (recommended — required for launchctl)

The config file at `~/.photo-scanner-config.json` (in your home directory) stores all settings in one place, including API keys. This is the recommended approach and the only one that works with launchctl, since launchctl does not load your shell profile or environment variables.

```json
{
  "folder_id": "1R5UhpYBe2nzZaf5T8qtAhHha76ajhRhO",
  "topaz_api_key": "your_topaz_api_key",
  "xai_api_key": "your_xai_api_key"
}
```

(The `~` symbol represents your home directory. To show hidden files in Finder, press `Cmd + Shift + .`)

Then run the script with no parameters:
```bash
python photo_scanner.py
```

If `--folder-id` is passed on the command line it takes precedence over the config file. API keys in the config file take precedence over environment variables.

### Topaz Fallback

If you want to route a specific photo through Topaz instead of xAI, prefix the filename with `topaz_` before dropping it in `~/Pictures`:

| Input filename | Processed by | Output filename |
|---|---|---|
| `IMG_20260702_0015.jpg` | xAI (default) | `IMG_20260702_0015_ai.jpg` |
| `topaz_IMG_20260702_0015.jpg` | Topaz (fallback) | `IMG_20260702_0015_ai.jpg` |

The `topaz_` prefix is stripped from the output filename automatically. Pillow dust removal runs first regardless of which enhancer is used.

## Notes

- xAI is the default enhancer — it produces vivid, modern-looking results similar to an iPhone photo. The prompt is specifically tuned to preserve faces at the pixel level while aggressively enhancing everything else.
- Pillow dust removal runs before every API call, giving the AI a cleaner input image.
- Topaz is available as a fallback via the `topaz_` filename prefix if needed.
- Only files that complete the full pipeline (original + AI enhancement + upload) are deleted locally.
- The script is safe to re-run — it checks Drive for existing sequence numbers and avoids overwriting files.
- Credential and token files are excluded from this repository via `.gitignore`.
