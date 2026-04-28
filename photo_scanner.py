import os
import re
import glob
import base64
import requests
import time
from PIL import Image #ensure you've installed Pillow (pip install Pillow)
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

# Google Drive folder ID for the current album's "Cropped Photos" folder
FOLDER_ID = '1yrc9WRIVJ7GXqntftRi-PXDYLBUU3n6A' #Album 1
FOLDER_ID = '1hpgIRznuIwjA5qcqLIjJeuHTKuTzaSiz' #Album 2
FOLDER_ID = '1R5UhpYBe2nzZaf5T8qtAhHha76ajhRhO' #Album 3
FOLDER_ID = '1x8LW9k71LU7zO39duKO3gLlj_X8Wwr0k' #Album 4
FOLDER_ID = '1UT_vbwsuVERcfobNdggozWJSgPjgfTb-' #Album 5 
FOLDER_ID = '1TmPn7KetNcAFoB9H3GfMIuPxh_uxT9XO' #Album 6
FOLDER_ID = '1Y9Zg8Xmr2JgA4rGLnNOXeEhpmZ1fgBKr' #Album 7
FOLDER_ID = '1125dMjJMzv2zLmx6BDI63zm6NtMZqWN3' #Album 8

# Local scanner output folder
SCANNER_OUTPUT = '/Users/sreynoso/Pictures'

# xAI API key - note that the following API key has been added as an environment variable (type 'env' to confirm)
XAI_API_KEY = os.environ.get('XAI_API_KEY')

# xAI enhancement prompt
ENHANCEMENT_PROMPT = """Adjust the coloring, lighting, and clarity of the attached photograph so it looks like a modern photo taken with a current iPhone. Remove the heavy tint and haziness to make the colors true-to-life. Only adjust the lighting, color balance, and overall image resolution.

Directive 1 (STRICT FACE PRESERVATION):

Do NOT enhance, regenerate, reconstruct, or AI-improve the face in any way. Strictly transfer the exact facial features, skin texture, eye shape, nose, mouth, eyebrows, and expression from the source image with zero artistic interpretation or beautification. The face must remain 100% identical in identity and detail to the original photograph — even if it is blurry, grainy, or imperfect. Only apply global color correction, lighting fixes, and haze removal to the non-face areas of the image.

Directive 2: Do NOT add any date, timestamp, or text overlay to the enhanced image. The only exception is if the source image already has date information physically printed on the photo — in that case, preserve it exactly as it appears in the original. If the source image has no date printed on it, the enhanced image must also have no date."""

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
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


def get_last_sequence_number(service, date_prefix):
    """Query Google Drive to find the highest sequence number for a given date in the Cropped Photos folder."""
    results = service.files().list(
        q=f"'{FOLDER_ID}' in parents and trashed = false",
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


def upload_to_drive(service, local_path, filename):
    """Upload a file to the current album folder on Google Drive."""
    file_metadata = {
        'name': filename,
        'parents': [FOLDER_ID]
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
# MAIN PIPELINE
# ============================================================

def main():
    # Step 1: Find scanner output files
    pattern = os.path.join(SCANNER_OUTPUT, 'IMG_*.jpg') 
    # Also check for uppercase .JPG
    local_files = sorted(glob.glob(pattern) + glob.glob(pattern.replace('.jpg', '.JPG')))

    # Filter out any _ai files that might be lingering
    local_files = [f for f in local_files if '_ai.' not in f.lower()]

    # Remove duplicates (in case .jpg and .JPG matched the same file)
    local_files = sorted(set(local_files))

    if not local_files:
        print("No scanner output files found in " + SCANNER_OUTPUT)
        print("Place photos on the scanner and scan before running this script.")
        return

    print(f"Found {len(local_files)} scanned photo(s) to process.\n")

    # Step 2: Authenticate with Google Drive
    print("Authenticating with Google Drive...")
    service = authenticate_drive()

    # Step 3: Extract date prefix from the first scanner file
    first_filename = os.path.basename(local_files[0])
    date_match = re.match(r'IMG_(\d{8})_', first_filename)
    if not date_match:
        print(f"ERROR: First file doesn't match expected pattern: {first_filename}")
        return
    date_prefix = date_match.group(1)

    # Step 4: Get the last sequence number for this date from Drive
    last_seq = get_last_sequence_number(service, date_prefix)
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

        # Extract the date from the original scanner filename
        file_date_match = re.match(r'IMG_(\d{8})_', original_filename)
        if not file_date_match:
            print(f"  WARNING: Filename doesn't match expected pattern, skipping: {original_filename}")
            continue
        file_date = file_date_match.group(1)

        if needs_rename:
            # Calculate new sequence number
            new_seq = last_seq + i + 1
            new_filename = f"IMG_{file_date}_{new_seq:04d}.jpg"
        else:
            new_filename = original_filename

        ai_filename = new_filename.replace('.jpg', '_ai.jpg').replace('.JPG', '_ai.jpg')

        # Rename the local file if needed
        new_path = os.path.join(SCANNER_OUTPUT, new_filename)
        if original_path != new_path:
            os.rename(original_path, new_path)
            print(f"  Renamed: {original_filename} -> {new_filename}")

        # Detect if the source image is black and white
        is_bw = is_black_and_white(new_path)
        if is_bw:
            print(f"  Detected as black & white image")

        # Enhance with xAI
        ai_path = os.path.join(SCANNER_OUTPUT, ai_filename)
        success = enhance_with_xai(new_path, ai_path)

        if success:
            # Upload both the original and enhanced version to Drive
            upload_to_drive(service, new_path, new_filename)
            upload_to_drive(service, ai_path, ai_filename)
            completed_files.append(new_path)
            completed_files.append(ai_path)

            # If the source was B&W, create a grayscale version of the enhanced image
            if is_bw:
                bw_ai_filename = new_filename.replace('.jpg', '_bw_ai.jpg').replace('.JPG', '_bw_ai.jpg')
                bw_ai_path = os.path.join(SCANNER_OUTPUT, bw_ai_filename)
                img = Image.open(ai_path).convert('L')
                img.save(bw_ai_path)
                print(f"  Created B&W version: {bw_ai_filename}")
                upload_to_drive(service, bw_ai_path, bw_ai_filename)
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
