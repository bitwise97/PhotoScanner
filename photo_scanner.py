# photo_scanner.py
#
# Scans, AI-enhances, and uploads family photos to a Google Drive album folder.
# Automatically detects black & white photos and produces a grayscale-enhanced
# version alongside the color-enhanced version, using the Pillow image library.
#
# Enhancement pipeline (default, xAI): a 4-stage face-preserving pipeline guarantees
# human faces are unchanged at the pixel level rather than relying on prompt wording.
#   Stage 1  InsightFace detects every face and builds a pixel-precise mask.
#   Stage 2  Dust removal runs outside the mask only, then xAI enhances the image.
#   Stage 3  The original face pixels are composited back over the result, graded
#            through the same global tone curve the enhancement applied elsewhere,
#            with a soft transition ring just outside the strict face area.
#   Stage 4  The composite is verified byte-for-byte as a lossless PNG; the PNG is
#            then converted to the final JPEG and discarded.
# If verification fails, the photo is treated as a failure: nothing is uploaded and
# the local file is kept so it can be retried or routed through Topaz.
#
# Topaz fallback: To route a specific file through Topaz instead of xAI, prefix the
# filename with 'topaz_' before dropping it in ~/Pictures, e.g.:
#   topaz_IMG_20260702_0015.jpg  →  processed by Topaz  →  IMG_20260702_0015_ai.jpg
# The Topaz path uses whole-image dust removal and is not face-masked.
#
# Usage:
#   python photo_scanner.py [--folder-id <DRIVE_FOLDER_ID>]
#
# Parameters (optional):
#   --folder-id    The Google Drive folder ID for the current album.
#                  (Found in the folder's URL: drive.google.com/drive/folders/<ID>)
#
# Configuration:
#   The script reads settings from ~/.photo-scanner-config.json
#   (in your home directory, e.g. /Users/sreynoso/.photo-scanner-config.json).
#   *Note: To show hidden files in Finder, press Command (⌘) + Shift (⇧) + . (period)
#
#   Config file format:
#   {
#     "folder_id":    "<DRIVE_FOLDER_ID>",
#     "topaz_api_key": "<TOPAZ_API_KEY>",
#     "xai_api_key":  "<XAI_API_KEY>"
#   }
#
#   API keys can also be set as environment variables (TOPAZ_API_KEY, XAI_API_KEY).
#   The config file takes precedence over environment variables.
#
# Examples:
#   python photo_scanner.py --folder-id 1R5UhpYBe2nzZaf5T8qtAhHha76ajhRhO
#   python photo_scanner.py  # reads all settings from ~/.photo-scanner-config.json
#
# Prerequisites:
#   - launchctl must be configured to monitor ~/Pictures and trigger this script
#     automatically when new scanned files are detected.
#   - Pillow (pip install Pillow) must be installed for black & white photo detection
#     and grayscale image processing.
#   - On first run, a browser window will open to authorize Google Drive access.
#     If the token expires (e.g. after 7 days in test mode), the script will
#     re-authorize automatically via browser rather than failing.

import argparse
import json
import os
import re
import glob
import base64
import requests
import time
import cv2
import numpy as np
from PIL import Image, ImageFilter
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ============================================================
# CONFIGURATION - Update these as needed
# ============================================================

# Google Drive OAuth
SCOPES = ['https://www.googleapis.com/auth/drive']
CREDENTIALS_FILE = os.path.expanduser('~/photo-scanner/photo_scanner_automation_credentials.json')
TOKEN_FILE = os.path.expanduser('~/photo-scanner/token.json')

# Local scanner output folder
SCANNER_OUTPUT = '/Users/sreynoso/Pictures'

# API keys — loaded from ~/.photo-scanner-config.json or environment variables.
# Config file takes precedence. See header comment for config file format.
TOPAZ_API_KEY = None
XAI_API_KEY = None

# xAI enhancement prompt.
#
# Face preservation is NOT the job of this prompt — Stages 1/3/4 of
# enhance_photo_pipeline() enforce that mechanically by compositing the original
# face pixels back and verifying them byte-for-byte. Faces are mentioned here only
# to discourage the model from repositioning or rescaling heads, since geometric
# drift is what makes the Stage 3 composite misalign.
#
# The prompt's real job is color fidelity. Asking for "vivid, punchy" colors and
# calling out clothing by name reliably produced invented colors (a gray shirt
# coming back saturated blue), so the saturation instruction is deliberately
# bounded to recovering what fading removed, and hue changes are forbidden outright.
ENHANCEMENT_PROMPT = """Restore this scanned photograph. This is archival family history: the goal is faithful restoration, not creative reinterpretation.

WHAT TO FIX:
- Remove the overall color cast (amber, yellow, blue, or magenta) left by film aging, as a single global white-balance correction applied evenly across the whole image.
- Correct exposure and lift shadows so the scene reads as evenly and naturally lit.
- Remove dust, specks, scratches, creases, and haze.
- Restore the contrast and clarity that fading has cost the image.

COLOR FIDELITY — THIS IS THE PRIORITY:
Every object must keep its original color identity. Correct the cast; do not repaint the subject.
- Do not change the hue of anything. A gray shirt stays gray. A brown floor stays brown. A white wall stays white.
- Neutral objects stay neutral. Never add color to something that is gray, white, or black.
- Recover saturation only to the degree that fading removed it. Do not exceed the saturation an ordinary, well-exposed photograph of this scene would have had.
- Do not make colors vivid, punchy, bold, or rich. Natural and accurate is correct; striking is wrong.
- If you are unsure what color something originally was, leave it exactly as it is.

GEOMETRY:
- Keep the exact framing, aspect ratio, and placement. Do not crop, rotate, zoom, or reposition anything.
- Keep every person and object at its original position, scale, and pose. Heads in particular must not move or change size.
- Do not add, remove, or substitute any object, person, or background element.

TEXT:
- Preserve any physically printed date or text exactly. Add no text, borders, or watermarks.

Output only the restored photograph."""

# Topaz enhancement prompt (max 1024 characters — condensed version of the xAI prompt)
TOPAZ_PROMPT = ("Restore this old scanned photograph. "
                "Remove scratches, dust, creases, and haze. "
                "Correct color casts and remove yellowing and fading. "
                "Boost color vibrance and saturation for a vivid, modern look. "
                "Balance lighting for even, natural exposure. "
                "Do not alter faces in any way — preserve all facial features exactly. "
                "Do not invent details not visible in the original. "
                "Preserve original composition and framing. "
                "Preserve any physically printed dates or text exactly as they appear.")

# ============================================================
# GOOGLE DRIVE FUNCTIONS
# ============================================================

def authenticate_drive():
    """Authenticate with Google Drive and return the service object."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Refresh token has expired or been revoked — re-authorize via browser
                print("Token has expired and could not be refreshed. Opening browser to re-authorize...")
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


def get_last_sequence_number(service, date_prefix, folder_id):
    """Query Google Drive to find the highest sequence number for a given date in the Cropped Photos folder."""
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(name)",
        pageSize=1000
    ).execute()

    files = results.get('files', [])
    max_seq = 0

    for f in files:
        # Only match files with the same date prefix (exclude _ai files)
        match = re.match(rf'IMG_{date_prefix}_(\d{{4}})\.jpg', f['name'], re.IGNORECASE)
        if match:
            seq = int(match.group(1))
            if seq > max_seq:
                max_seq = seq

    return max_seq


def upload_to_drive(service, local_path, filename, folder_id):
    """Upload a file to the current album folder on Google Drive."""
    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }
    media = MediaFileUpload(local_path, mimetype='image/jpeg')
    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, name'
    ).execute()
    print(f"  Uploaded to Drive: {uploaded['name']}")
    return uploaded

def is_black_and_white(image_path, saturation_threshold=10):
    """Detect if an image is black and white by checking average color saturation."""
    img = Image.open(image_path).convert('RGB')
    # Sample pixels to check saturation (resize for speed)
    img_small = img.resize((100, 100))
    pixels = list(img_small.get_flattened_data())

    total_saturation = 0
    for r, g, b in pixels:
        max_c = max(r, g, b)
        min_c = min(r, g, b)
        total_saturation += (max_c - min_c)

    avg_saturation = total_saturation / len(pixels)
    print(f"  Average saturation: {avg_saturation:.1f} (threshold: {saturation_threshold})")
    return avg_saturation < saturation_threshold

# ============================================================
# DUST REMOVAL (PRE-PROCESSING)
# ============================================================

def apply_dust_removal(input_path, output_path):
    """Apply a median filter to remove dust and scratch specks before AI enhancement."""
    img = Image.open(input_path)
    result = img.filter(ImageFilter.MedianFilter(size=3))  # size must be odd; 3=subtle, 5=moderate, 7=aggressive
    result.save(output_path, 'JPEG', quality=95)

# ============================================================
# XAI ENHANCEMENT FUNCTION
# ============================================================

def enhance_with_xai(input_path, output_path):
    """Send a photo to xAI for enhancement and save the result."""
    print(f"  Sending to xAI for enhancement...")

    with open(input_path, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode('utf-8')

    response = requests.post(
        'https://api.x.ai/v1/images/edits',
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {XAI_API_KEY}'
        },
        json={
            'model': 'grok-imagine-image',
            'prompt': ENHANCEMENT_PROMPT,
            'image': {
                'url': f'data:image/jpeg;base64,{image_data}'
            }
        },
        timeout=120
    )

    if response.status_code != 200:
        print(f"  ERROR: xAI returned status {response.status_code}")
        print(f"  Response: {response.text}")
        return False

    result = response.json()

    # The response should contain a URL to the enhanced image
    if 'data' in result and len(result['data']) > 0:
        image_url = result['data'][0].get('url')
        if image_url:
            # Download the enhanced image
            img_response = requests.get(image_url, timeout=60)
            if img_response.status_code == 200:
                with open(output_path, 'wb') as f:
                    f.write(img_response.content)
                print(f"  Enhanced image saved: {os.path.basename(output_path)}")
                return True
            else:
                print(f"  ERROR: Failed to download enhanced image")
                return False

    # If response contains base64 data directly
    if 'data' in result and len(result['data']) > 0:
        b64_data = result['data'][0].get('b64_json')
        if b64_data:
            with open(output_path, 'wb') as f:
                f.write(base64.b64decode(b64_data))
            print(f"  Enhanced image saved: {os.path.basename(output_path)}")
            return True

    print(f"  ERROR: Unexpected response format from xAI")
    print(f"  Response: {result}")
    return False


# ============================================================
# FACE-PRESERVING ENHANCEMENT PIPELINE (4 STAGES)
# ============================================================
#
# The single-call xAI approach relied on prompt instructions to leave faces
# alone. This pipeline enforces that guarantee mechanically instead:
#
#   Stage 1  Detect faces and build a pixel-precise mask.
#   Stage 2  Enhance the image via xAI (faces are protected from dust removal
#            beforehand, and overwritten afterward regardless of what xAI did).
#   Stage 3  Fit the global tone curve the enhancement applied to the rest of the
#            photo, and composite the ORIGINAL face pixels back through it.
#   Stage 4  Verify the faces equal the graded original byte-for-byte under a
#            monotonic curve; fail loudly otherwise.
#
# Face pixels always derive from the RAW scan — dust removal is applied only
# outside the mask, so facial texture is never median-filtered.
#
# Stage 3 originally pasted the face pixels back completely unmodified. That is a
# stronger-sounding guarantee and it verified perfectly, but on a badly faded scan
# it produced orange face-shaped patches on otherwise correctly graded people: the
# background moved by a mean of 92.8 levels while the faces moved by 0. Grading the
# original pixels through the same curve keeps what actually matters — no AI ever
# redraws a face — while letting faces sit in register with the restored photo. A
# per-channel monotonic LUT cannot invent a feature, smooth skin, or sharpen an
# edge; it can only move color and tone.

# Mask geometry, expressed as a fraction of each face's diagonal so the mask
# scales with face size rather than image resolution.
FACE_HULL_DILATE_RATIO = 0.08   # grow the landmark hull to cover hairline and neck junction
FACE_FEATHER_RATIO = 0.06       # width of the soft transition ring OUTSIDE the strict mask

# Detection is tuned hard for recall, because the two error types are not
# symmetric: a false positive only leaves a small patch unenhanced, while a
# missed face is regenerated by xAI with no protection at all — and does so
# silently, since Stage 4 only verifies pixels inside the mask.
#
# On faded 1970s scans a single detector pass is unreliable. Measured on one such
# photo, face counts by det_size were 640->3, 1024->2, 1600->4, 2048->1: unstable
# and non-monotonic, with different scales finding different faces. So detection
# runs at several scales and the results are merged.
FACE_DET_SIZES = (640, 1024, 1600)
FACE_DET_THRESH = 0.3           # below the 0.5 default; false positives are the cheap error
FACE_DEDUPE_IOU = 0.4           # boxes overlapping more than this are the same face

# Stretching contrast in a faded face amplifies whatever is already there — the
# scan's halftone screen and film grain. Because the three channels are graded
# independently the amplification differs per channel, so the texture reads as
# colored speckle. Measured inside the mask on the degraded scan, grading raised
# luminance high-frequency energy 2.73 -> 4.49 and chroma speckle 0.50 -> 0.96.
#
# Smoothing chroma alone removes the color mesh while leaving luminance — which
# carries essentially all perceived detail — completely untouched. Set below 3 to
# disable. FACE_LUMA_TOLERANCE absorbs YCrCb/BGR round-trip rounding when Stage 4
# checks that the smoothing really was chroma-only.
FACE_CHROMA_SMOOTH_PX = 3
FACE_LUMA_TOLERANCE = 2

# Faces are deliberately excluded from median despeckling (set below 3 = off).
#
# Including them was tried, on the theory that the halftone mesh is an artifact of
# the print rather than of the photograph and so is not worth preserving. It failed
# on both counts: FFT analysis of a face crop puts the screen at a period of ~8.3px
# in two orientations (51x and 43x baseline energy), so a 3px median cannot reach it
# — the mesh survived untouched while gradient correlation against the original
# collapsed from 0.9612 to 0.5548. Reaching an 8px period spatially would need a
# ~9px kernel, which on a 165px-wide face destroys the face.
#
# Real facial structure occupies periods of 15-55px, well separated from the screen,
# so a frequency-domain notch is the tool that could remove it selectively.
FACE_DESPECKLE_PX = 0

_FACE_ANALYZERS = {}


def _get_face_analyzer(det_size):
    """Lazily build an InsightFace analyzer per detector scale.

    Models download once to ~/.insightface; the analyzers are cached because
    prepare() is far more expensive than inference.
    """
    if det_size not in _FACE_ANALYZERS:
        from insightface.app import FaceAnalysis
        if not _FACE_ANALYZERS:
            print("  Loading InsightFace model (first run downloads ~300MB)...")
        analyzer = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
        analyzer.prepare(ctx_id=-1, det_thresh=FACE_DET_THRESH, det_size=(det_size, det_size))
        _FACE_ANALYZERS[det_size] = analyzer
    return _FACE_ANALYZERS[det_size]


def _iou(a, b):
    """Intersection-over-union of two (x1, y1, x2, y2) boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / float(area_a + area_b - inter)


def detect_faces_multiscale(image_bgr):
    """Run detection at every scale in FACE_DET_SIZES and merge the results.

    Different scales surface different faces, so the union is taken and then
    deduplicated by IoU, keeping the highest-scoring detection of each face.

    Detection runs on the raw scan deliberately. Normalizing the color cast and
    equalizing contrast first (gray-world white balance + CLAHE) seemed like it
    should help on faded photos, but measured on a badly degraded 1970s scan it
    found no additional real faces and manufactured three false ones — CLAHE
    amplifies fabric texture into face-like contrast. Raw detection found the same
    six faces with zero false positives, including a background face at a higher
    score (0.534) than the normalized pass gave it (0.350).
    """
    found = []
    for det_size in FACE_DET_SIZES:
        for face in _get_face_analyzer(det_size).get(image_bgr):
            found.append(face)

    # Highest confidence first, so the detection kept for each face is the best one.
    found.sort(key=lambda f: float(getattr(f, 'det_score', 0.0)), reverse=True)

    merged = []
    for face in found:
        box = [int(v) for v in face.bbox]
        if all(_iou(box, [int(v) for v in kept.bbox]) <= FACE_DEDUPE_IOU for kept in merged):
            merged.append(face)
    return merged


def _odd(n):
    """Round up to the nearest odd integer >= 1 (OpenCV kernels must be odd)."""
    n = max(1, int(round(n)))
    return n if n % 2 == 1 else n + 1


def generate_face_mask(image_bgr):
    """Stage 1 — detect every face and build the strict and feathered masks.

    Returns (strict_mask, alpha, boxes):
      strict_mask  uint8 {0,255}  core face area; these pixels are copied verbatim
      alpha        float32 [0,1]  1.0 inside strict_mask, ramping to 0 across a ring
                                  just outside it, 0 everywhere else
      boxes        list of (x1, y1, x2, y2) bounding boxes, for logging
    """
    h, w = image_bgr.shape[:2]
    strict_mask = np.zeros((h, w), dtype=np.uint8)
    boxes = []

    faces = detect_faces_multiscale(image_bgr)

    feather_px = 1  # grows with the largest face found; 1 keeps GaussianBlur valid if none are
    for face in faces:
        x1, y1, x2, y2 = [int(v) for v in face.bbox]
        boxes.append((x1, y1, x2, y2))

        # Prefer the dense 106-point contour; fall back to the 5-point kps, then the
        # bounding box, so partial or low-quality faces still get covered.
        points = getattr(face, 'landmark_2d_106', None)
        if points is None:
            points = getattr(face, 'kps', None)
        if points is None:
            points = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float32)
        points = np.asarray(points, dtype=np.float32).reshape(-1, 2)

        face_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.fillConvexPoly(face_mask, cv2.convexHull(points.astype(np.int32)), 255)

        # Landmarks trace the face contour but stop short of the hairline and the
        # neck junction, so grow the hull outward proportionally to face size.
        diag = float(np.hypot(x2 - x1, y2 - y1))
        grow = _odd(diag * FACE_HULL_DILATE_RATIO)
        face_mask = cv2.dilate(face_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))

        strict_mask = cv2.bitwise_or(strict_mask, face_mask)
        feather_px = max(feather_px, _odd(diag * FACE_FEATHER_RATIO))

    if not faces:
        return strict_mask, np.zeros((h, w), dtype=np.float32), boxes

    # Build the alpha ramp in the ring OUTSIDE the strict mask. Blurring a dilated
    # copy keeps the ramp entirely outside the core, and forcing alpha to 1.0 across
    # the strict area guarantees those pixels take no contribution from the enhanced
    # image even before the verbatim copy in Stage 3.
    ring = cv2.dilate(strict_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (feather_px, feather_px)))
    alpha = cv2.GaussianBlur(ring.astype(np.float32) / 255.0, (feather_px, feather_px), 0)
    alpha[strict_mask > 0] = 1.0

    return strict_mask, alpha, boxes


def apply_masked_dust_removal(image_bgr, strict_mask):
    """Median-filter the image everywhere except the strict face area.

    Faces keep their raw scan texture; dust and specks elsewhere are smoothed.
    """
    filtered = cv2.medianBlur(image_bgr, 3)
    core = strict_mask > 0
    filtered[core] = image_bgr[core]
    return filtered


def enhance_via_xai_array(image_bgr, work_path):
    """Stage 2 — round-trip an image array through xAI and return the enhanced array.

    xAI is generative and may return different dimensions, so the result is resized
    back to the input dimensions to keep the mask aligned. Returns None on failure.
    """
    h, w = image_bgr.shape[:2]
    stage_in = work_path + '_stage2_in.jpg'
    stage_out = work_path + '_stage2_out.jpg'

    try:
        cv2.imwrite(stage_in, image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not enhance_with_xai(stage_in, stage_out):
            return None

        enhanced = cv2.imread(stage_out, cv2.IMREAD_COLOR)
        if enhanced is None:
            print(f"  ERROR: Could not read the image xAI returned")
            return None

        if enhanced.shape[:2] != (h, w):
            print(f"  xAI returned {enhanced.shape[1]}x{enhanced.shape[0]}, "
                  f"resizing to {w}x{h} to realign with the mask")
            enhanced = cv2.resize(enhanced, (w, h), interpolation=cv2.INTER_LANCZOS4)

        return enhanced
    finally:
        for temp in (stage_in, stage_out):
            if os.path.exists(temp):
                os.remove(temp)


def compute_tone_transfer(original_bgr, enhanced_bgr, fit_mask):
    """Derive the global per-channel tone curve the enhancement applied.

    Fitted by histogram matching over `fit_mask` — the non-face region — so it
    describes the grade xAI applied to the rest of the photograph. Because the fit
    uses tonal distributions rather than pixel correspondence, it is unaffected by
    the geometric drift a generative model introduces.

    Returns a (256, 3) uint8 LUT, one monotonic curve per BGR channel.
    """
    lut = np.zeros((256, 3), dtype=np.uint8)
    levels = np.arange(256)

    for c in range(3):
        src_cdf = np.cumsum(np.bincount(original_bgr[..., c][fit_mask], minlength=256).astype(np.float64))
        dst_cdf = np.cumsum(np.bincount(enhanced_bgr[..., c][fit_mask], minlength=256).astype(np.float64))
        if src_cdf[-1] == 0 or dst_cdf[-1] == 0:
            lut[:, c] = levels  # nothing to fit; identity
            continue
        src_cdf /= src_cdf[-1]
        dst_cdf /= dst_cdf[-1]
        # For each source level, the enhanced level sitting at the same quantile.
        lut[:, c] = np.clip(np.rint(np.interp(src_cdf, dst_cdf, levels)), 0, 255).astype(np.uint8)

    return lut


def apply_tone_transfer(image_bgr, lut):
    """Map an image through a (256, 3) per-channel LUT."""
    out = np.empty_like(image_bgr)
    for c in range(3):
        out[..., c] = lut[:, c][image_bgr[..., c]]
    return out


def smooth_face_chroma(image_bgr, radius=FACE_CHROMA_SMOOTH_PX):
    """Median-filter the chroma channels, passing luminance through untouched.

    Working in YCrCb lets the Y plane be copied verbatim, so no facial detail can
    be lost: only the color assigned to that detail is smoothed.
    """
    if radius < 3:
        return image_bgr.copy()

    ycrcb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2YCrCb)
    ycrcb[..., 1] = cv2.medianBlur(ycrcb[..., 1], radius)
    ycrcb[..., 2] = cv2.medianBlur(ycrcb[..., 2], radius)
    return cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)


def build_face_layer(original_bgr, lut):
    """The complete, deterministic face transform, derived only from the raw scan:
    despeckle, tone-grade, then smooth chroma.

    Kept in one function so Stage 4 can recompute it independently and compare the
    result byte-for-byte against what actually got written. No step here can
    introduce content that was not already in the scan.
    """
    return smooth_face_chroma(apply_tone_transfer(_face_source(original_bgr), lut))


def _face_source(original_bgr):
    """The raw scan, optionally despeckled — the sole origin of every face pixel."""
    if FACE_DESPECKLE_PX >= 3:
        return cv2.medianBlur(original_bgr, FACE_DESPECKLE_PX)
    return original_bgr


def grade_and_composite(original_bgr, enhanced_bgr, strict_mask, alpha, lut):
    """Stage 3 — composite tone-graded ORIGINAL face pixels onto the enhanced image.

    Face pixels come from the original scan, mapped through the global curve derived
    from the rest of the photo. Nothing is regenerated: the mapping is per-pixel and
    per-channel, so grain, texture, expressions, and every spatial relationship in
    the face survive intact — only color and tone move, bringing faces into register
    with the corrected background instead of leaving them as raw amber patches.

    The alpha ramp smooths the ring outside the strict mask; the strict area is then
    overwritten with the graded original so no blending or rounding can mix in
    enhanced-image content.
    """
    graded = build_face_layer(original_bgr, lut)

    a = alpha[..., None]
    blended = graded.astype(np.float32) * a + enhanced_bgr.astype(np.float32) * (1.0 - a)
    result = np.clip(np.rint(blended), 0, 255).astype(np.uint8)

    core = strict_mask > 0
    result[core] = graded[core]
    return result


def verify_face_grade(original_bgr, final_bgr, strict_mask, lut):
    """Stage 4 — confirm faces are the original pixels under a tone curve and a
    chroma-only smooth, and nothing else.

    Three independent checks:

      1. Every strict-mask pixel must equal the independently recomputed face
         layer, byte for byte. Any AI-generated content — a redrawn eye, a
         smoothed cheek, a sharpened edge — fails here, because the face layer is
         derived solely from the original scan.
      2. Each channel curve must be non-decreasing. A monotonic mapping preserves
         the ordering of tones, so it cannot invert or restructure detail.
      3. The chroma smoothing must leave luminance intact, which is what proves it
         touched only color and not detail.

    Returns (ok, changed_pixel_count, monotonic, max_luma_deviation).
    """
    monotonic = all(bool(np.all(np.diff(lut[:, c].astype(np.int16)) >= 0)) for c in range(3))

    core = strict_mask > 0
    if not core.any():
        return monotonic, 0, monotonic, 0

    graded = apply_tone_transfer(_face_source(original_bgr), lut)
    expected = smooth_face_chroma(graded)

    # Did smoothing disturb luminance anywhere in the face?
    luma_before = cv2.cvtColor(graded, cv2.COLOR_BGR2YCrCb)[..., 0].astype(np.int16)
    luma_after = cv2.cvtColor(expected, cv2.COLOR_BGR2YCrCb)[..., 0].astype(np.int16)
    luma_dev = int(np.abs(luma_before - luma_after)[core].max())

    diff = cv2.absdiff(expected, final_bgr).max(axis=2)
    changed = int(np.count_nonzero((diff > 0) & core))

    ok = changed == 0 and monotonic and luma_dev <= FACE_LUMA_TOLERANCE
    return ok, changed, monotonic, luma_dev


def enhance_photo_pipeline(input_path: str, output_path: str) -> bool:
    """Enhance a photo while guaranteeing human faces are unchanged at the pixel level.

    Runs the four stages against the raw scan, verifies the result on a lossless PNG,
    then writes `output_path` as JPEG and discards the PNG. The input file is never
    modified. Returns True on success, False on any hard error or failed verification.
    """
    original = cv2.imread(input_path, cv2.IMREAD_COLOR)
    if original is None:
        print(f"  ERROR: Could not read {os.path.basename(input_path)}")
        return False

    h, w = original.shape[:2]
    png_path = os.path.splitext(output_path)[0] + '.png'

    # ---- Stage 1: face mask ----
    print(f"  Stage 1: detecting faces...")
    try:
        strict_mask, alpha, boxes = generate_face_mask(original)
    except Exception as e:
        print(f"  ERROR: Face detection failed: {e}")
        return False

    if boxes:
        coverage = 100.0 * np.count_nonzero(strict_mask) / (h * w)
        print(f"  Stage 1: {len(boxes)} face(s) masked, {coverage:.1f}% of image protected")
        for i, (x1, y1, x2, y2) in enumerate(boxes, 1):
            print(f"    face {i}: ({x1},{y1})-({x2},{y2})")
    else:
        print(f"  Stage 1: no faces detected — enhancing the whole image")

    # ---- Stage 2: enhancement ----
    # Faces are excluded from dust removal so their guarantee holds against the raw scan.
    print(f"  Stage 2: dust removal (outside faces) + xAI enhancement...")
    prepared = apply_masked_dust_removal(original, strict_mask)
    enhanced = enhance_via_xai_array(prepared, os.path.splitext(output_path)[0])
    if enhanced is None:
        print(f"  ERROR: Stage 2 enhancement failed")
        return False

    # With no faces there is nothing to protect, so the enhanced result stands as-is.
    if not boxes:
        cv2.imwrite(output_path, enhanced, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        print(f"  Enhanced image saved: {os.path.basename(output_path)}")
        return True

    # ---- Stage 3: graded composite ----
    # The tone curve is fitted only where alpha is 0 — the untouched enhanced
    # region — so neither face pixels nor the feathered ring skew the fit.
    print(f"  Stage 3: grading and compositing original face pixels...")
    lut = compute_tone_transfer(original, enhanced, alpha == 0)
    final = grade_and_composite(original, enhanced, strict_mask, alpha, lut)

    # ---- Stage 4: verification on a lossless PNG ----
    # JPEG would recompress the composite and invalidate a byte-exact check, so the
    # result is verified as PNG and only then written out as the JPEG deliverable.
    print(f"  Stage 4: verifying face pixels...")
    if not cv2.imwrite(png_path, final):
        print(f"  ERROR: Could not write verification PNG")
        return False

    try:
        reloaded = cv2.imread(png_path, cv2.IMREAD_COLOR)
        if reloaded is None:
            print(f"  ERROR: Could not read back verification PNG")
            return False

        ok, changed, monotonic, luma_dev = verify_face_grade(original, reloaded, strict_mask, lut)
        if not ok:
            if not monotonic:
                print(f"  WARNING: Verification FAILED — tone curve is not monotonic, "
                      f"so it could restructure facial detail. Discarding enhanced result.")
            elif luma_dev > FACE_LUMA_TOLERANCE:
                print(f"  WARNING: Verification FAILED — chroma smoothing shifted face "
                      f"luminance by up to {luma_dev} levels. Discarding enhanced result.")
            else:
                print(f"  WARNING: Verification FAILED — {changed} face pixel(s) do not match "
                      f"the graded original. Discarding enhanced result.")
            return False

        print(f"  Stage 4: verified — faces are the original pixels under a monotonic tone "
              f"curve; chroma-only smoothing shifted luminance by at most {luma_dev}")
        cv2.imwrite(output_path, reloaded, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        print(f"  Enhanced image saved: {os.path.basename(output_path)}")
        return True
    finally:
        if os.path.exists(png_path):
            os.remove(png_path)


# ============================================================
# TOPAZ ENHANCEMENT FUNCTION
# ============================================================

def enhance_with_topaz(input_path, output_path):
    """Send a photo to Topaz Gigapixel (High Fidelity V2) for enhancement and save the result."""
    print(f"  Sending to Topaz for enhancement...")

    topaz_enhance_url = 'https://api.topazlabs.com/image/v1/enhance-gen/async'
    topaz_status_url = 'https://api.topazlabs.com/image/v1/status'
    topaz_download_url = 'https://api.topazlabs.com/image/v1/download'

    headers = {'X-API-KEY': TOPAZ_API_KEY}
    start_time = time.time()

    with open(input_path, 'rb') as f:
        response = requests.post(
            topaz_enhance_url,
            headers=headers,
            files={'image': (os.path.basename(input_path), f, 'image/jpeg')},
            data={
                'model': 'Wonder 2',
                'output_format': 'jpeg',
                'prompt': TOPAZ_PROMPT,
                'face_enhancement': 'false',  # Preserve faces as-is; true/false
                'denoise': '1.0',             # Dust/scratch removal strength; range 0.0–1.0 (0=off, 1=maximum)
                'creativity': '6',            # Reconstruction latitude; range 1–6 (1=conservative, 6=most generative)
            },
            timeout=60
        )

    if response.status_code != 200:
        print(f"  ERROR: Topaz returned status {response.status_code}")
        print(f"  Response: {response.text}")
        return False

    process_id = response.json().get('process_id')
    if not process_id:
        print(f"  ERROR: No process_id in Topaz response: {response.json()}")
        return False

    print(f"  Topaz job submitted (process_id: {process_id}). Waiting for completion...")

    # Poll for completion (up to 10 minutes)
    for attempt in range(120):
        time.sleep(5)
        status_response = requests.get(
            f'{topaz_status_url}/{process_id}',
            headers=headers,
            timeout=30
        )
        if status_response.status_code != 200:
            print(f"  ERROR: Failed to check Topaz status: {status_response.status_code}")
            return False

        status = status_response.json().get('status')
        if status == 'Completed':
            break
        elif status == 'Failed':
            print(f"  ERROR: Topaz enhancement job failed.")
            print(f"  Response: {status_response.json()}")
            return False
        elif status == 'Cancelled':
            print(f"  ERROR: Topaz enhancement job was cancelled.")
            return False
        # Still processing — keep polling

    else:
        print(f"  ERROR: Topaz enhancement timed out after 10 minutes.")
        return False

    elapsed = time.time() - start_time
    print(f"  Topaz processing completed in {elapsed:.1f}s")

    # Get the download URL from Topaz
    download_response = requests.get(
        f'{topaz_download_url}/{process_id}',
        headers=headers,
        timeout=30
    )
    if download_response.status_code != 200:
        print(f"  ERROR: Failed to get Topaz download URL: {download_response.status_code}")
        return False

    image_url = download_response.json().get('download_url')
    if not image_url:
        print(f"  ERROR: No download_url in Topaz download response: {download_response.json()}")
        return False

    # Download the actual image from the URL
    image_response = requests.get(image_url, timeout=120)
    if image_response.status_code != 200:
        print(f"  ERROR: Failed to download image from Topaz URL: {image_response.status_code}")
        return False

    with open(output_path, 'wb') as f:
        f.write(image_response.content)

    print(f"  Enhanced image saved: {os.path.basename(output_path)}")
    return True

# ============================================================
# CONFIG HANDLING
# ============================================================

def load_config_file():
    """Load settings from ~/.photo-scanner-config.json.

    Returns the parsed config dict, or an empty dict if the file doesn't exist.
    API keys in the config file take precedence over environment variables.
    """
    global TOPAZ_API_KEY, XAI_API_KEY

    config_path = os.path.expanduser('~/.photo-scanner-config.json')

    # Start with environment variables as the baseline
    TOPAZ_API_KEY = os.environ.get('TOPAZ_API_KEY')
    XAI_API_KEY = os.environ.get('XAI_API_KEY')

    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)

        # Config file values override environment variables
        if config.get('topaz_api_key'):
            TOPAZ_API_KEY = config['topaz_api_key']
        if config.get('xai_api_key'):
            XAI_API_KEY = config['xai_api_key']

        return config
    except Exception as e:
        print(f"WARNING: Could not read config file {config_path}: {e}")
        return {}

# ============================================================
# MAIN PIPELINE
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Scan, enhance, and upload photos to Google Drive.')
    parser.add_argument('--folder-id', required=False, help='Google Drive folder ID for the current album')
    args = parser.parse_args()

    # Load config file (also sets TOPAZ_API_KEY and XAI_API_KEY globals)
    config = load_config_file()

    # Use CLI parameter if provided, otherwise fall back to config file
    folder_id = args.folder_id or config.get('folder_id')

    if not folder_id:
        config_path = os.path.expanduser('~/.photo-scanner-config.json')
        print("ERROR: folder_id not provided via --folder-id parameter or config file")
        print(f"\nUsage:")
        print("  python photo_scanner.py --folder-id <DRIVE_FOLDER_ID>")
        print(f"\nOr add it to the config file at: {config_path}")
        print('  { "folder_id": "<DRIVE_FOLDER_ID>", "topaz_api_key": "...", "xai_api_key": "..." }')
        return

    if not TOPAZ_API_KEY:
        print("ERROR: TOPAZ_API_KEY not found in config file or environment variables.")
        print("Add 'topaz_api_key' to ~/.photo-scanner-config.json or set the TOPAZ_API_KEY environment variable.")
        return

    # Step 1: Find scanner output files — both normal (IMG_*) and xAI fallback (xAI_IMG_*)
    patterns = [
        os.path.join(SCANNER_OUTPUT, 'IMG_*.jpg'),
        os.path.join(SCANNER_OUTPUT, 'IMG_*.JPG'),
        os.path.join(SCANNER_OUTPUT, 'topaz_IMG_*.jpg'),
        os.path.join(SCANNER_OUTPUT, 'topaz_IMG_*.JPG'),
    ]
    local_files = sorted(set(f for p in patterns for f in glob.glob(p)))

    # Filter out any _ai, _dr (dust-removal temp), or _stage2_ (pipeline temp) files
    # that might be lingering from an interrupted run
    local_files = [f for f in local_files if '_ai.' not in os.path.basename(f).lower()
                   and '_dr.' not in os.path.basename(f).lower()
                   and '_stage2_' not in os.path.basename(f).lower()]

    # Remove duplicates
    local_files = sorted(set(local_files))

    if not local_files:
        print("No scanner output files found in " + SCANNER_OUTPUT)
        print("Place photos on the scanner and scan before running this script.")
        return

    print(f"Found {len(local_files)} scanned photo(s) to process.\n")

    # Step 2: Authenticate with Google Drive
    print("Authenticating with Google Drive...")
    service = authenticate_drive()

    # Step 3: Extract date prefix from the first scanner file (strip topaz_ prefix if present)
    first_filename = os.path.basename(local_files[0])
    first_filename_normalized = first_filename[6:] if first_filename.lower().startswith('topaz_') else first_filename
    date_match = re.match(r'IMG_(\d{8})_', first_filename_normalized)
    if not date_match:
        print(f"ERROR: First file doesn't match expected pattern: {first_filename}")
        return
    date_prefix = date_match.group(1)

    # Step 4: Get the last sequence number for this date from Drive
    last_seq = get_last_sequence_number(service, date_prefix, folder_id)
    print(f"Last sequence number on Google Drive for {date_prefix}: {last_seq:04d}")

    # Determine if renaming is needed by checking if any local file
    # would conflict with what's already on Drive
    local_sequences = []
    for f in local_files:
        name = os.path.basename(f)
        if name.lower().startswith('topaz_'):
            name = name[6:]
        m = re.match(r'IMG_\d{8}_(\d{4})\.jpg', name, re.IGNORECASE)
        if m:
            local_sequences.append(int(m.group(1)))

    needs_rename = last_seq > 0 and bool(local_sequences) and min(local_sequences) <= last_seq
    if needs_rename:
        print(f"Conflict detected: renaming files to start from {last_seq + 1:04d}\n")
    else:
        print(f"No conflicts: keeping original filenames\n")

    # Step 5: Process each file
    completed_files = []  # Only files that were fully processed (original + AI)
    failed_files = []     # Files where enhancement failed

    for i, original_path in enumerate(local_files):
        original_filename = os.path.basename(original_path)
        print(f"Processing [{i + 1}/{len(local_files)}]: {original_filename}")

        # Check if this file is flagged for Topaz fallback
        use_topaz = original_filename.lower().startswith('topaz_')

        # Strip topaz_ prefix to get the base filename for processing
        base_filename = original_filename[6:] if use_topaz else original_filename

        # Extract the date from the base filename
        file_date_match = re.match(r'IMG_(\d{8})_', base_filename)
        if not file_date_match:
            print(f"  WARNING: Filename doesn't match expected pattern, skipping: {original_filename}")
            continue
        file_date = file_date_match.group(1)

        if use_topaz:
            # Topaz fallback: strip the topaz_ prefix, output with standard naming
            # Input:  topaz_IMG_20260702_0015.jpg
            # Output: IMG_20260702_0015_ai.jpg
            new_filename = base_filename
            new_path = os.path.join(SCANNER_OUTPUT, new_filename)
            if original_path != new_path:
                os.rename(original_path, new_path)
                print(f"  Renamed: {original_filename} -> {new_filename} (Topaz fallback)")
        else:
            # Normal flow: rename if needed to avoid sequence conflicts
            if needs_rename:
                new_seq = last_seq + i + 1
                new_filename = f"IMG_{file_date}_{new_seq:04d}.jpg"
            else:
                new_filename = original_filename

            new_path = os.path.join(SCANNER_OUTPUT, new_filename)
            if original_path != new_path:
                os.rename(original_path, new_path)
                print(f"  Renamed: {original_filename} -> {new_filename}")

        ai_filename = new_filename.replace('.jpg', '_ai.jpg').replace('.JPG', '_ai.jpg')

        # Detect if the source image is black and white
        is_bw = is_black_and_white(new_path)
        if is_bw:
            print(f"  Detected as black & white image")

        # Route to the face-preserving xAI pipeline (default) or Topaz (topaz_ prefix)
        ai_path = os.path.join(SCANNER_OUTPUT, ai_filename)
        if use_topaz:
            # Topaz path is unchanged: whole-image dust removal, then Topaz.
            dust_removed_path = new_path.replace('.jpg', '_dr.jpg').replace('.JPG', '_dr.jpg')
            print(f"  Applying dust removal (MedianFilter size=3)...")
            apply_dust_removal(new_path, dust_removed_path)
            print(f"  Using Topaz (fallback requested via filename prefix)")
            success = enhance_with_topaz(dust_removed_path, ai_path)
            if os.path.exists(dust_removed_path):
                os.remove(dust_removed_path)
        else:
            # 4-stage pipeline; it applies its own face-masked dust removal internally.
            success = enhance_photo_pipeline(new_path, ai_path)

        if success:
            # Upload both the original and enhanced version to Drive
            upload_to_drive(service, new_path, new_filename, folder_id)
            upload_to_drive(service, ai_path, ai_filename, folder_id)
            completed_files.append(new_path)
            completed_files.append(ai_path)

            # If the source was B&W, create a grayscale version of the enhanced image
            if is_bw:
                bw_ai_filename = new_filename.replace('.jpg', '_bw_ai.jpg').replace('.JPG', '_bw_ai.jpg')
                bw_ai_path = os.path.join(SCANNER_OUTPUT, bw_ai_filename)
                img = Image.open(ai_path).convert('L')
                img.save(bw_ai_path)
                print(f"  Created B&W version: {bw_ai_filename}")
                upload_to_drive(service, bw_ai_path, bw_ai_filename, folder_id)
                completed_files.append(bw_ai_path)
        else:
            print(f"  WARNING: Enhancement failed for {new_filename}, keeping local file. Not uploaded to Drive.")
            failed_files.append(new_filename)

        print()  # Blank line between files

    # Step 6: Clean up - only delete files that were fully processed
    if completed_files:
        print("Cleaning up completed files...")
        for f in completed_files:
            if os.path.exists(f):
                os.remove(f)
                print(f"  Deleted: {os.path.basename(f)}")

    if failed_files:
        print(f"\nWARNING: {len(failed_files)} file(s) were NOT deleted due to enhancement failure:")
        for f in failed_files:
            print(f"  {f}")

    print(f"\nDone! {len(completed_files) // 2} of {len(local_files)} photo(s) fully processed.")


if __name__ == '__main__':
    main()