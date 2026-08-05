"""Filename sequencing and prefix routing.

Regression cover for a bug where a fresh scan dropped in as topaz_IMG_..._0001.jpg
uploaded as 0001 and collided with a file already on Drive, because prefixed files
skipped the sequence-conflict check entirely.
"""
import sys

from harness import Report, originals, sandbox


def main():
    report = Report('Filename sequencing and prefix routing')

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
    ]

    for label, filenames, last_seq, expected in cases:
        with sandbox(filenames, last_seq=last_seq) as result:
            got = originals(result)
            report.check(label, got == expected, f'expected {expected}, got {got}')

    return report.finish()


if __name__ == '__main__':
    sys.exit(main())
