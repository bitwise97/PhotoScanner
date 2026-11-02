# Photo Scanner Automation

A Python script that automates the end-to-end workflow for digitizing old family photos — from flatbed scanner output to AI-enhanced uploads on Google Drive. It straightens sideways scans, colorizes black & white photographs, restores faded colour ones, and can protect faces from being redrawn by the AI when that matters.

## What It Does

Any `.jpg` or `.JPG` dropped into the watch folder is treated as a photo to process, whatever the scanner named it. For each one:

1. **Names it consistently** — a file already named `IMG_YYYYMMDD_NNNN.jpg` keeps its date and number unless that would collide with something already on Drive; anything else is dated today and given the next free number
2. **Straightens the scan** — detects orientation from the geometry of any faces in the photo and rotates it upright, in place
3. **Detects black & white** photographs by measuring colour saturation
4. **Enhances the photo** with xAI: restoring faded colour, correcting casts, lifting shadows, removing dust and scratches, and repairing print damage. Monochrome scans are colorized instead
5. **Uploads both** the original scan and the enhanced version to Google Drive, plus a grayscale version of the enhancement for B&W photos
6. **Cleans up** local files, but only for photos that completed the whole pipeline

## Processing Modes

How a photo is processed is chosen by its filename prefix. Rename a scan and drop it back in the watch folder to re-run it a different way — every mode writes the same output filename.

| Input filename | Processed by |
|---|---|
| `IMG_20260702_0015.jpg` | xAI, faces unprotected (default) |
| `preserve_IMG_20260702_0015.jpg` | xAI, with the face-preserving pipeline |
| `preserveX2_IMG_20260702_0015.jpg` | the same, with aggressive face detection |

The same three routes are available as `--mode` values on `--reprocess`, for a photo that is already on Drive. The default is the same on both paths — no prefix and no `--mode` both mean unprotected xAI — so there is one rule to remember rather than one per entry point.

Leaving faces unprotected is the default because it gives the best results on most photos — it reconstructs detail and repairs damage freely. On some photos it reworks faces until people are no longer recognisable, and `preserve_` is the remedy for those.

The prefix says only *how* to process the photo. It goes in front of whatever the file is already called — `preserve_IMG_9445.JPG` works as well as `preserve_IMG_20260702_0015.jpg` — and prefixed files are renumbered on sequence conflict exactly like unprefixed ones, so a prefix works equally well on a fresh scan or on a re-run of one you weren't happy with.

### The face-preserving pipeline

`preserve_` runs a four-stage pipeline instead of a single API call:

1. **Detect** — InsightFace locates every face across several detector scales and builds a pixel-precise mask
2. **Enhance** — the raw scan goes to xAI, whatever it does to the faces
3. **Composite** — faces are rebuilt from the scan and laid back over the result. Colour comes entirely from xAI so each face matches its surroundings, while luminance comes entirely from your scan, so no facial detail is ever invented
4. **Verify** — the composite is checked byte-for-byte against an independently recomputed face layer. If it doesn't match exactly, the photo is treated as a failure: nothing is uploaded and the local file is kept

`FACE_XAI_LUMA_BLEND` controls the split, and defaults to `0.0` — facial detail entirely from your scan. It ran at `0.30` for a while, on the theory that a little of xAI's luminance would lift the scan's own grain and softness, but on badly faded scans that share was visible as enhancement bleeding into faces, which is the thing the mask exists to prevent. Raise it toward `0.10`–`0.15` if faces read as too rough against a reconstructed background.

Two related constants shape the mask itself: `FACE_HULL_DILATE_RATIO` (`0.04`) grows the outline outward from the detected landmarks, and `FACE_FEATHER_RATIO` (`0.01`) sets the width of the soft transition ring just outside it. Shrinking them trades overspill around the face for leakage at its edges — too tight and a hairline or jaw falls outside the mask and gets enhanced, which is subtle because it shows at the edges of a face rather than across it.

### Detection resolution

Faces are detected on a copy of the scan capped at `FACE_DET_MAX_EDGE` (`1600`) on the long edge, and the boxes and landmarks are scaled back afterwards. More resolution made detection *worse*, not better.

The detector never looks at more than the largest entry in `FACE_DET_SIZES`, which is also 1600 — it resizes internally. Handing it a 4717×5400 scan meant that resize threw away 3.4× the pixels, and on a grainy print it aliased grain into structure that read as facial texture. On one 1970s photograph of three people it masked a man's belly, his forearm and a woman's knees alongside the three real faces. Downscaling first with `INTER_AREA`, which averages the grain rather than sampling through it, gives the detector the same size image without that artefact.

Measured across 12 real scans and 52 detections: false positives fell from 5 to 2, and the gap between the lowest real face and the highest false positive widened from 0.06 to 0.16, with all 12 real faces still found. On the photograph above, 7 detections became 4. An infant's face that scored 0.51 at full resolution scored 0.54 downscaled — the marginal real faces gained headroom rather than losing it.

Boosting contrast beforehand was tried and is worse. Grey-world white balance took the same photograph from 5 false positives to 9, and a percentile contrast stretch to 7; both pushed the highest false positive *above* the lowest real face, so no threshold could separate them. Lifting contrast on a faded, grainy scan amplifies exactly the grain that creates the problem. CLAHE fares better and is still what `preserveX2` uses, but on its own it narrowed the margin too.

### When a face is missed entirely

A face the detector never finds gets no protection at all, and passes verification silently, because Stage 4 only checks pixels inside the mask. On badly faded scans this does happen — a boy turned three-quarters away in a near-monochrome red wash was invisible to every threshold and scale down to `0.10`.

`preserveX2_` adds a detection pass over a CLAHE-normalised copy of the scan at a lower threshold, which found that face at score 0.21. Normalisation is used only to *locate* faces; the mask and everything composited back still come from the untouched scan.

It's opt-in because it isn't free. Measured across nine real scans it found one genuine face and about ten false ones, nearly all on patterned fabric — a polka-dot dress, folds of clothing, wall texture. Score can't separate them: the real face scored 0.21 while the false positives scored 0.22–0.27, *above* it. Each false positive freezes a small patch out of enhancement, which is a far cheaper error than an AI-invented face, but not one worth paying on every photo.

So use `preserve_` by default, and reach for `preserveX2_` on a photo where you can see a face was missed.

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
- An **xAI API key** — required, this is the only enhancer, and checked at startup
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

### Reprocessing a photo already on Drive

To re-enhance a photo that's already uploaded, pass its original scan name:

```bash
python photo_scanner.py --reprocess IMG_20260422_0060.jpg --mode preserve
```

This downloads the original from Drive, enhances it again, and replaces `IMG_20260422_0060_ai.jpg` — and `_bw_ai.jpg` if the photo is black & white — **in place**. Because the content of the existing file is replaced rather than a new file created, each one keeps its file ID, its name and its creation date, so nothing is renamed, nothing is renumbered, share links keep working, and a reprocessed 1970s photo doesn't jump to the end of an album sorted by date added.

A few deliberate choices:

- **The original is never written to.** It's downloaded read-only and used as the source. It is the thing that makes a reprocess repeatable, so nothing in this path can modify it.
- **Auto-orientation is skipped.** The photo on Drive was straightened when it was first processed, so re-running orientation detection would only give it a second chance to rotate an upright photo wrongly.
- **Nothing reaches Drive until the enhancement has succeeded locally.** A failed enhancement, or a failed face verification, leaves the album exactly as it was.
- **The previous enhancement stays recoverable for 30 days** in Drive's own version history ("Manage versions" on the file), after which Drive purges it automatically. The script never deletes those revisions — letting them expire on their own is what makes a reprocess undoable at no lasting storage cost.
- **Only the original filename is accepted.** Passing `IMG_x_ai.jpg` is rejected with a note telling you which name to use; the derivatives are found by convention. Filenames with a letter suffix are supported — `IMG_20260422_0060b.jpg` is valid.
- **Ambiguity stops the run.** Drive allows two files with the same name in one folder, and overwriting the wrong one isn't undoable by re-running, so a duplicated name aborts before anything is written.
- Work happens in a temp directory, never in the watch folder, so the launchd job can't pick up the download mid-flight and process the same photo a second time.

`--mode` accepts `xai` (default), `preserve` and `preservex2`. The route in use is printed before the API call. A failed reprocess exits non-zero.

## Notes and Behaviour

**Enhancement model.** Uses `grok-imagine-image-quality-latest`. The standard `grok-imagine-image` model is markedly weaker — on the same scan with the same prompt it produced roughly a fifth of the fine detail. `XAI_MODEL` near the top of the script documents all three options.

**Zero Data Retention.** Images are requested inline as base64 rather than as a URL, so xAI never stores the generated image. This is required for ZDR accounts and harmless otherwise.

**Print borders** are preserved exactly. A scan with a white or cream paper border is detected, the border is cropped off before the photo goes to xAI, and the enhanced photo is pasted back inside the scan's own border afterwards — including rounded corners, and the paper wedge left by a print scanned slightly askew. xAI never sees the border, because when it did, it outpainted into it: told in the prompt to leave the margin alone, it invented the top of a figure in a painting that the border had cut off. A border is only recognised when all four sides have one, so an overcast sky or a pale band along one edge isn't mistaken for paper.

**No pre-processing.** Every route sends the raw scan. Median-filtering it first to remove dust cost about 74% of its fine detail to remove specks xAI strips anyway, so that step was removed along with the Topaz route that used it.

**Black & white detection** switches the prompt from restoration to colorization. Detection triggers below an average saturation of 10, so a heavily sepia-toned print may be treated as a colour photo.

**Auto-orientation** reads the direction of the eye-to-mouth axis on detected faces. It deliberately declines when the evidence is weak or the faces disagree, leaving the photo untouched rather than guessing — a wrong rotation is worse than a missed one. Photos without people cannot be oriented this way and need rotating by hand.

**Failure is safe.** If enhancement or verification fails, nothing is uploaded and the local file is kept. Only photos that complete the entire pipeline are deleted locally.

**Re-running a photo** produces a new sequence number rather than replacing the earlier version, so both sit on Drive and you delete the one you don't want.

**Renames happen as a batch**, before any photo is processed, and are staged through temporary names. Renaming files directly to their final names is unsafe: `os.rename` overwrites silently, so shifting a batch up by fewer positions than it has files makes each rename destroy a scan that has not been processed yet, and the damage cascades. Six unique scans once arrived on Drive as six copies of the first one, with the other five originals gone before they were ever uploaded.

**Credential and token files** are excluded from this repository via `.gitignore`.

### Known quirks

- `SCANNER_OUTPUT` is hardcoded and must be edited before first use.
- Every JPEG in the watch folder is processed, uploaded and then **deleted locally**. Nothing else should be kept in that folder — unload the launchctl job first if you need to store ordinary JPEGs there.

## Tests

```bash
python tests/run_all.py
```

65 cases, running in about two seconds, needing no API keys, no network, and no Google Drive access. Google Drive, the xAI enhancement API and auto-orientation are stubbed out, and scans are generated into a temp directory — but the genuine `main()` runs, so the tests exercise the real filename sequencing, prefix routing, key-validation and reprocessing code rather than a copy of it. Exits non-zero if anything fails.

Most cases cover a bug that reached production:

- Prefixed files skipping the sequence-conflict check and colliding with files already on Drive
- Counter-style names never being renamed, so they uploaded under the scanner's own filename
- An unrecognised filename aborting the entire run rather than skipping one photo
- API-key validation requiring a second service's key while never checking the xAI one
- xAI outpainting into a print's paper border, invented content and all
- The rename cascade that destroyed scans — checked by hashing the bytes each upload actually sends, since the filenames were all correct and only the images behind them were duplicated

The `--reprocess` cases are the exception: that path is new, so they fix its invariants rather than recording past failures. They assert the negatives — that the original is never written to, that a duplicated name aborts before any write, and that a failed enhancement reaches Drive with nothing at all — and each was checked by deliberately reintroducing the bug it describes and confirming the right case fails.
