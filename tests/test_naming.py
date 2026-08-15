"""Filename sequencing and prefix routing, across both naming conventions.

Regression cover for two bugs. A fresh scan dropped in as topaz_IMG_..._0001.jpg
uploaded as 0001 and collided with a file already on Drive, because prefixed files
skipped the sequence-conflict check entirely. Separately, counter-style names
(IMG00001.JPG) were left unrenamed because the rename only fired on a sequence
conflict — a silent failure that surfaced only as wrong filenames on Drive.
"""
import sys
import time

from harness import Report, originals, sandbox


def main():
    report = Report('Filename sequencing and prefix routing')
    today = time.strftime('%Y%m%d')

    cases = [
        ('topaz_ on a fresh scan is renumbered past the conflict',
         ['topaz_IMG_20260730_0001.jpg'], 2, ['IMG_20260730_0003.jpg']),
        ('preserve_ on a fresh scan is renumbered past the conflict',
         ['preserve_IMG_20260730_0001.jpg'], 2, ['IMG_20260730_0003.jpg']),
        ('unprefixed is renumbered past the conflict',
         ['IMG_20260730_0001.jpg'], 2, ['IMG_20260730_0003.jpg']),
        ('no conflict keeps the number and drops the prefix',
         ['topaz_IMG_20260730_0005.jpg'], 0, ['IMG_20260730_0005.jpg']),
        ('mixed batch renumbers in order',
         ['IMG_20260730_0001.jpg', 'topaz_IMG_20260730_0002.jpg'], 4,
         ['IMG_20260730_0005.jpg', 'IMG_20260730_0006.jpg']),

        # Counter-style names carry no date and no Drive sequence, so they are dated
        # on import and always renumbered — even with no conflict, which is the case
        # that silently failed.
        ('counter name is dated today and renumbered past the last on Drive',
         ['IMG00001.JPG'], 6, [f'IMG_{today}_0007.jpg']),
        ('counter name with nothing on Drive starts at 0001',
         ['IMG00001.JPG'], 0, [f'IMG_{today}_0001.jpg']),
        ('the camera counter is ignored, not carried over',
         ['IMG00042.JPG'], 0, [f'IMG_{today}_0001.jpg']),
        ('lowercase extension is accepted and normalised to .jpg',
         ['IMG00001.jpg'], 0, [f'IMG_{today}_0001.jpg']),
        ('a prefix routes a counter name and still renames it',
         ['topaz_IMG00001.JPG'], 2, [f'IMG_{today}_0003.jpg']),
    ]

    for label, filenames, last_seq, expected in cases:
        with sandbox(filenames, last_seq=last_seq) as result:
            got = originals(result)
            report.check(label, got == expected, f'expected {expected}, got {got}')

    return report.finish()


if __name__ == '__main__':
    sys.exit(main())
