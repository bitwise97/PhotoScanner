# Photo Scanner Automation

A Python script that automates the end-to-end workflow for digitizing old family photos — from flatbed scanner output to AI-enhanced uploads on Google Drive. It straightens sideways scans, colorizes black & white photographs, restores faded colour ones, and can protect faces from being redrawn by the AI when that matters.

## What It Does

For every scan dropped into the watch folder:

1. **Renames to avoid collisions** — checks Google Drive for the highest sequence number already used for that date and renumbers if needed
2. **Straightens the scan** — detects orientation from the geometry of any faces in the photo and rotates it upright, in place
3. **Detects black & white** photographs by measuring colour saturation
4. **Enhances the photo** with xAI: restoring faded colour, correcting casts, lifting shadows, removing dust and scratches, and repairing print damage. Monochrome scans are colorized instead
5. **Uploads both** the original scan and the enhanced version to Google Drive, plus a grayscale version of the enhancement for B&W photos
6. **Cleans up** local files, but only for photos that completed the whole pipeline

## Processing Modes

How a photo is processed is chosen by its filename prefix. Rename a scan and drop it back in the watch folder to re-run it a different way — every mode writes the same output filename.

| Input filename | Processed by |
|---|---|
| `IMG_20260702_0015.jpg` | xAI, unrestricted (default) |
| `preserve_IMG_20260702_0015.jpg` | xAI, with the face-preserving pipeline |
| `topaz_IMG_20260702_0015.jpg` | Topaz instead of xAI |

Unrestricted xAI is the default because it gives the best results on most photos — it reconstructs detail and repairs damage freely. On some photos it reworks faces until people are no longer recognisable, and `preserve_` is the remedy for those.

The prefix says only *how* to process the photo. Prefixed files are renumbered on sequence conflict exactly like unprefixed ones, so a prefix works equally well on a fresh scan or on a re-run of one you weren't happy with.

### The face-preserving pipeline

`preserve_` runs a four-stage pipeline instead of a single API call:

1. **Detect** — InsightFace locates every face across several detector scales and builds a pixel-precise mask
2. **Enhance** — the raw scan goes to xAI, whatever it does to the faces
3. **Composite** — faces are rebuilt from the scan and laid back over the result. Colour comes entirely from xAI so each face matches its surroundings; luminance is weighted 70/30 toward the scan, so facial identity stays original while the scan's halftone and grain are lifted
4. **Verify** — the composite is checked byte-for-byte against an independently recomputed face layer. If it doesn't match exactly, the photo is treated as a failure: nothing is uploaded and the local file is kept

The blend is controlled by `FACE_XAI_LUMA_BLEND` (default `0.30`). Set it to `0.0` for facial detail entirely from your scan, higher for more of xAI's reconstruction.

## Output Files Per Photo

| File | Description |
|---|---|
| `IMG_YYYYMMDD_NNNN.jpg` | Original scan, unmodified except for straightening |
| `IMG_YYYYMMDD_NNNN_ai.jpg` | AI-enhanced (or colorized) version |
| `IMG_YYYYMMDD_NNNN_bw_ai.jpg` | Grayscale version of the enhancement (B&W photos only) |

The enhanced file comes back at whatever resolution xAI produces — currently capped around 1200px on the long edge — so it is smaller than the scan. The full-resolution original is uploaded alongside it.

## Requirements

- Python — developed and tested on 3.13
- A Google Cloud project with the **Google Drive API** enabled
- OAuth 2.0 credentials downloaded from the Google Cloud Console
- An **xAI API key** — required, this is the default enhancer, and checked at startup
- A **Topaz API key** — only needed if you use the `topaz_` prefix
- **launchctl** (macOS) configured to watch the scan folder and trigger the script, if you want it to run automatically

On first run, InsightFace downloads its face-detection models — about a 280MB download, occupying roughly 600MB in `~/.insightface` once unpacked. This happens once.

### Install dependencies

```bash
pip install -r requirements.txt
```

## Setup

1. Enable the **Google Drive API** in the [Google Cloud Console](https://console.cloud.google.com/).
2. Create OAuth 2.0 credentials (Desktop app) and save them as `photo_scanner_automation_credentials.json` in the project folder.
3. **Edit `SCANNER_OUTPUT` near the top of `photo_scanner.py`** to point at your scanner's output folder. It is currently hardcoded to a specific home directory.
4. Create `~/.photo-scanner-config.json`:
   ```json
   {
     "folder_id": "your_google_drive_folder_id",
     "topaz_api_key": "your_topaz_api_key",
     "xai_api_key": "your_xai_api_key"
   }
   ```
5. Optionally configure launchctl to watch the scan folder and run `python /path/to/photo_scanner.py` — no parameters needed, since everything is read from the config file.

On first run a browser window opens to authorize Google Drive. The token is saved locally and refreshed automatically; if it expires, the script re-authorizes through the browser rather than failing.

## Usage

Drop scanned photos in the watch folder, then run:

```bash
python photo_scanner.py
```

Or pass the Drive folder explicitly, which takes precedence over the config file:

```bash
python photo_scanner.py --folder-id 1R5UhpYBe2nzZaf5T8qtAhHha76ajhRhO
```

The config file is the recommended approach and the only one that works under launchctl, which does not load your shell profile or environment variables. API keys in the config file take precedence over environment variables.

(`~` is your home directory. To show hidden files in Finder, press `Cmd + Shift + .`)

## Notes and Behaviour

**Enhancement model.** Uses `grok-imagine-image-quality-latest`. The standard `grok-imagine-image` model is markedly weaker — on the same scan with the same prompt it produced roughly a fifth of the fine detail. `XAI_MODEL` near the top of the script documents all three options.

**Zero Data Retention.** Images are requested inline as base64 rather than as a URL, so xAI never stores the generated image. This is required for ZDR accounts and harmless otherwise.

**Dust removal** runs only on the Topaz route. Median-filtering a scan costs about 74% of its fine detail to remove dust that xAI strips anyway, so the xAI routes send the raw scan untouched.

**Black & white detection** switches the prompt from restoration to colorization. Detection triggers below an average saturation of 10, so a heavily sepia-toned print may be treated as a colour photo.

**Auto-orientation** reads the direction of the eye-to-mouth axis on detected faces. It deliberately declines when the evidence is weak or the faces disagree, leaving the photo untouched rather than guessing — a wrong rotation is worse than a missed one. Photos without people cannot be oriented this way and need rotating by hand.

**Failure is safe.** If enhancement or verification fails, nothing is uploaded and the local file is kept. Only photos that complete the entire pipeline are deleted locally.

**Re-running a photo** produces a new sequence number rather than replacing the earlier version, so both sit on Drive and you delete the one you don't want.

**Credential and token files** are excluded from this repository via `.gitignore`.

### Known quirks

- `SCANNER_OUTPUT` is hardcoded and must be edited before first use.

## Tests

```bash
python tests/run_all.py
```

Runs in about a second and needs no API keys, no network, and no Google Drive access. Google Drive, both enhancement APIs and auto-orientation are stubbed out, and scans are generated into a temp directory — but the genuine `main()` runs, so the tests exercise the real filename sequencing, prefix routing and key-validation code rather than a copy of it.

Exits non-zero if anything fails. Both modules cover bugs that reached production: prefixed files skipping the sequence-conflict check and colliding with files already on Drive, and the API-key validation that required the wrong key.
