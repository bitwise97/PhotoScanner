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
#   Stage 3  The exact original face pixels are composited back over the result,
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
#   Stage 3  Composite the exact original face pixels back onto the result.
#   Stage 4  Verify no face pixel changed; fail loudly if any did.
#
# Face pixels are guaranteed against the RAW scan — dust removal is applied
# only outside the mask, so facial texture is never median-filtered.

# Mask geometry, expressed as a fraction of each face's diagonal so the mask
# scales with face size rather than image resolution.
FACE_HULL_DILATE_RATIO = 0.08   # grow the landmark hull to cover hairline and neck junction
FACE_FEATHER_RATIO = 0.06       # width of the soft transition ring OUTSIDE the strict mask

_FACE_ANALYZER = None


def _get_face_analyzer():
    """Lazily build the InsightFace analyzer. Models download once to ~/.insightface."""
    global _FACE_ANALYZER
    if _FACE_ANALYZER is None:
        from insightface.app import FaceAnalysis
        print("  Loading InsightFace model (first run downloads ~300MB)...")
        analyzer = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
        analyzer.prepare(ctx_id=-1, det_size=(640, 640))
        _FACE_ANALYZER = analyzer
    return _FACE_ANALYZER


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

    faces = _get_face_analyzer().get(image_bgr)

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


def strict_composite(original_bgr, enhanced_bgr, strict_mask, alpha):
    """Stage 3 — paste the exact original face pixels onto the enhanced image.

    The alpha ramp smooths the transition in the ring outside the strict mask, then
    the strict area is overwritten with a verbatim integer copy so no blending or
    float rounding can touch a core face pixel.
    """
    a = alpha[..., None]
    blended = original_bgr.astype(np.float32) * a + enhanced_bgr.astype(np.float32) * (1.0 - a)
    result = np.clip(np.rint(blended), 0, 255).astype(np.uint8)

    core = strict_mask > 0
    result[core] = original_bgr[core]
    return result


def verify_face_pixels(original_bgr, final_bgr, strict_mask):
    """Stage 4 — confirm every strict-mask pixel is byte-identical to the original.

    Returns (ok, changed_pixel_count).
    """
    core = strict_mask > 0
    if not core.any():
        return True, 0

    diff = cv2.absdiff(original_bgr, final_bgr).max(axis=2)
    changed = int(np.count_nonzero((diff > 0) & core))
    return changed == 0, changed


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

    # ---- Stage 3: strict composite ----
    print(f"  Stage 3: compositing original face pixels...")
    final = strict_composite(original, enhanced, strict_mask, alpha)

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

        ok, changed = verify_face_pixels(original, reloaded, strict_mask)
        if not ok:
            print(f"  WARNING: Verification FAILED — {changed} face pixel(s) changed. "
                  f"Discarding enhanced result.")
            return False

        print(f"  Stage 4: verified — 0 face pixels changed")
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