"""Print-border detection and restoration.

Regression cover for xAI outpainting into a print's paper border — on one scan it
invented the top of a figure in a painting that the border had cut off. The border
is now detected, cropped off before enhancement, and pasted back afterwards, so
these check the detector, the paste, and that the enhancer never sees paper.
"""
import os
import sys
import tempfile

import cv2
import numpy as np

from harness import Report, ps


def bordered(w=600, h=400, border=(30, 30, 30, 40), paper=(220, 232, 238)):
    """A synthetic print: saturated photo content inside a cream paper border."""
    top, right, bottom, left = border
    img = np.full((h, w, 3), paper, np.uint8)
    rng = np.random.default_rng(1)
    photo = rng.integers(20, 160, (h - top - bottom, w - left - right, 3)).astype(np.uint8)
    photo[..., 2] = np.clip(photo[..., 2].astype(int) + 90, 0, 255)   # strong colour, not paper-like
    img[top:h - bottom, left:w - right] = photo
    return img, (left, top, w - right, h - bottom)


def main():
    report = Report('Print borders')

    img, truth = bordered()
    box = ps.detect_print_border(img)
    report.check('a paper border on all four sides is detected',
                 box is not None and all(abs(a - b) <= 2 for a, b in zip(box, truth)),
                 f'expected about {truth}, got {box}')

    photo_only = np.random.default_rng(2).integers(20, 200, (400, 600, 3)).astype(np.uint8)
    report.check('a borderless photo is left alone', ps.detect_print_border(photo_only) is None)

    sky = photo_only.copy()
    sky[:60] = (235, 235, 235)                    # a pale band on one side only, like overcast sky
    report.check('a pale band on one side is not mistaken for a border',
                 ps.detect_print_border(sky) is None)

    snow, _ = bordered()
    snow[30:360, 40:570] = (238, 238, 238)        # a mostly white photograph inside the border
    report.check('a mostly white photograph is not cropped into',
                 ps.detect_print_border(snow) is None)

    img, box = bordered()
    x1, y1, x2, y2 = box
    enhanced = np.full((y2 - y1, x2 - x1, 3), (30, 160, 40), np.uint8)   # same size: no rescale
    out = ps.paste_into_print_border(img, box, enhanced)
    inner = (slice(y1 + 20, y2 - 20), slice(x1 + 20, x2 - 20))
    report.check('the enhanced photograph is pasted unchanged inside the border',
                 np.array_equal(out[inner], enhanced[20:-20, 20:-20]))
    report.check('the border itself comes back untouched from the scan',
                 np.array_equal(out[:y1 - 1], img[:y1 - 1]) and np.array_equal(out[y2 + 1:], img[y2 + 1:]))

    # A sky is pale and colourless like paper, so restoring anything merely pale
    # pasted grey scan sky over the enhanced version in blocky steps. Only pixels
    # matching the border's own colour may be restored.
    sky_img, sky_box = bordered()
    sx1, sy1, sx2, sy2 = sky_box
    sky_img[sy1:sy1 + 120, sx1:sx2] = (205, 204, 200)   # pale sky running to the photo's top edge
    sky_enhanced = np.full((sy2 - sy1, sx2 - sx1, 3), (30, 160, 40), np.uint8)
    out = ps.paste_into_print_border(sky_img, sky_box, sky_enhanced)
    sky = out[sy1 + 2:sy1 + 100, sx1 + 20:sx2 - 20]
    report.check('a pale sky at the photo edge is not restored as if it were paper',
                 np.array_equal(sky, np.full(sky.shape, (30, 160, 40), np.uint8)),
                 f'{(sky != np.array([30, 160, 40], np.uint8)).any(axis=2).mean():.1%} of the sky was overwritten')

    tmp = tempfile.mkdtemp(prefix='photo-scanner-border-')
    src, dst = os.path.join(tmp, 'scan.jpg'), os.path.join(tmp, 'out.jpg')
    cv2.imwrite(src, img, [int(cv2.IMWRITE_JPEG_QUALITY), 98])
    seen = {}

    def fake_enhance(s, d):
        received = cv2.imread(s)
        seen['shape'] = received.shape[:2]
        seen['paper'] = ps._paper_mask(received).mean()
        cv2.imwrite(d, cv2.resize(received, (received.shape[1] // 2, received.shape[0] // 2)))
        return True

    ok = ps.enhance_within_print_border(src, dst, fake_enhance)
    result = cv2.imread(dst)
    report.check('the enhancer receives only the photograph, never the paper',
                 ok and seen.get('shape') == (y2 - y1, x2 - x1) and seen['paper'] < 0.05,
                 f"received {seen.get('shape')}, {seen.get('paper', 1):.0%} paper")
    report.check('the finished image keeps the border around the enhanced photo',
                 result is not None and ps.detect_print_border(result) is not None)
    report.check('no temporary files are left behind',
                 sorted(os.listdir(tmp)) == ['out.jpg', 'scan.jpg'], str(sorted(os.listdir(tmp))))

    return report.finish()


if __name__ == '__main__':
    sys.exit(main())
