# photo_scanner.py
#
# Scans, AI-enhances, and uploads family photos to a Google Drive album folder.
# Automatically detects black & white photos and produces a grayscale-enhanced
# version alongside the color-enhanced version, using the Pillow image library.
#
# Enhancement is handled by the Topaz Gigapixel (High Fidelity V2) API by default,
# which preserves original details without hallucinating new ones.
#
# xAI fallback: To route a specific file through xAI instead of Topaz, prefix the
# filename with 'xAI_' before dropping it in ~/Pictures, e.g.:
#   xAI_IMG_20260702_0015_ai.jpg  →  processed by xAI  →  IMG_20260702_0015_ai.jpg
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
from PIL import Image
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

# xAI enhancement prompt
ENHANCEMENT_PROMPT = """Restore this old scanned photograph to look like it was taken with a modern camera.

Critical rules:
- Do not alter faces in any way. Preserve exact facial features, skin texture, expressions, and imperfections 100% unchanged. Apply zero sharpening or smoothing to any faces.
- Do not invent, add, or reconstruct any details that are not clearly visible in the original. Only clean existing damage and apply global corrections.
- Preserve the exact original composition and framing.

Enhancement instructions:
- Correct and remove any overall color casts (including amber, yellow, blue, or magenta tints) across the entire photo so the colors appear natural and balanced.
- Dramatically brighten the entire image and significantly boost overall color vibrance and saturation so the colors appear vivid, punchy, rich, and lively — exactly as if captured with a modern high-end digital camera under bright, balanced lighting. Maintain a natural, photorealistic look without oversaturation or artificial effects.
- Remove yellowing and fading.
- For all clothing and fabrics: keep the exact original color family and hue with 100% fidelity. Strongly enhance vibrance and saturation to make the colors much richer, deeper, and more brilliant while preserving the authentic hue. Apply the same strong vibrance/saturation boost to backgrounds, objects, and non-facial areas.
- Remove scratches, dust, creases, and haze.
- Improve clarity, sharpness, and overall image quality while keeping the result natural and photographic.
- Balance lighting for even, natural exposure.
- Apply a modern digital color grade that noticeably increases color intensity and depth across the photo while keeping skin tones on all faces completely natural, unaltered in hue, and true to the original.

If any physically printed dates or text appear on the original print, preserve them exactly. Otherwise, add no text or overlays.

Output only the restored photograph."""

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

def is_black_and_white(image_path, saturation_threshold=15):
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
# TOPAZ ENHANCEMENT FUNCTION
# ============================================================

def enhance_with_topaz(input_path, output_path):
    """Send a photo to Topaz Gigapixel (High Fidelity V2) for enhancement and save the result."""
    print(f"  Sending to Topaz for enhancement...")

    topaz_enhance_url = 'https://api.topazlabs.com/image/v1/enhance/async'
    topaz_status_url = 'https://api.topazlabs.com/image/v1/status'
    topaz_download_url = 'https://api.topazlabs.com/image/v1/download'

    headers = {'X-API-KEY': TOPAZ_API_KEY}

    with open(input_path, 'rb') as f:
        response = requests.post(
            topaz_enhance_url,
            headers=headers,
            files={'image': (os.path.basename(input_path), f, 'image/jpeg')},
            data={
                'model': 'High Fidelity V2',
                'output_format': 'jpeg',
                'face_enhancement': 'false',  # Preserve faces as-is
                'strength': '0.5',            # Moderate enhancement, conservative
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

    # Download the enhanced image
    download_response = requests.get(
        f'{topaz_download_url}/{process_id}',
        headers=headers,
        timeout=120
    )
    if download_response.status_code != 200:
        print(f"  ERROR: Failed to download Topaz result: {download_response.status_code}")
        return False

    with open(output_path, 'wb') as f:
        f.write(download_response.content)

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
        os.path.join(SCANNER_OUTPUT, 'xAI_IMG_*.jpg'),
        os.path.join(SCANNER_OUTPUT, 'xAI_IMG_*.JPG'),
    ]
    local_files = sorted(set(f for p in patterns for f in glob.glob(p)))

    # Filter out any _ai files that might be lingering (but keep xAI_ prefixed files)
    local_files = [f for f in local_files if '_ai.' not in os.path.basename(f).lower()
                   or os.path.basename(f).lower().startswith('xai_')]

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

    # Step 3: Extract date prefix from the first scanner file (strip xAI_ prefix if present)
    first_filename = os.path.basename(local_files[0])
    first_filename_normalized = first_filename[4:] if first_filename.lower().startswith('xai_') else first_filename
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
        m = re.match(r'IMG_\d{8}_(\d{4})\.jpg', os.path.basename(f), re.IGNORECASE)
        if m:
            local_sequences.append(int(m.group(1)))

    needs_rename = last_seq > 0 and min(local_sequences) <= last_seq
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

        # Check if this file is flagged for xAI fallback
        use_xai = original_filename.lower().startswith('xai_')

        # Strip xAI_ prefix to get the base filename for processing
        base_filename = original_filename[4:] if use_xai else original_filename

        # Extract the date from the base filename
        file_date_match = re.match(r'IMG_(\d{8})_', base_filename)
        if not file_date_match:
            print(f"  WARNING: Filename doesn't match expected pattern, skipping: {original_filename}")
            continue
        file_date = file_date_match.group(1)

        if use_xai:
            # xAI fallback: strip the xAI_ prefix, output with standard naming
            # Input:  xAI_IMG_20260702_0015_ai.jpg
            # Output: IMG_20260702_0015_ai.jpg
            new_filename = base_filename
            new_path = os.path.join(SCANNER_OUTPUT, new_filename)
            if original_path != new_path:
                os.rename(original_path, new_path)
                print(f"  Renamed: {original_filename} -> {new_filename} (xAI fallback)")
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

        # Route to xAI or Topaz based on filename prefix
        ai_path = os.path.join(SCANNER_OUTPUT, ai_filename)
        if use_xai:
            print(f"  Using xAI (fallback requested via filename prefix)")
            success = enhance_with_xai(new_path, ai_path)
        else:
            success = enhance_with_topaz(new_path, ai_path)

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