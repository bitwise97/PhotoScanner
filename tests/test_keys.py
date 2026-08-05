"""API-key validation.

Regression cover for a bug where TOPAZ_API_KEY was required at startup while
XAI_API_KEY was never checked — so an xAI-only setup could not run at all, and a
missing xAI key failed as an opaque 401 mid-run after files had been renamed.
"""
import sys

from harness import Report, first_error, originals, sandbox


def main():
    report = Report('API-key validation')

    with sandbox(['IMG_20260804_0001.jpg'], xai_key='stub', topaz_key=None) as r:
        report.check('xAI key alone is enough for an unprefixed photo',
                     originals(r) == ['IMG_20260804_0001.jpg'] and r['remaining'] == [],
                     f"uploaded {originals(r)}, left {r['remaining']}")

    with sandbox(['IMG_20260804_0001.jpg'], xai_key=None, topaz_key='stub') as r:
        report.check('missing xAI key stops at startup, leaving the scan untouched',
                     originals(r) == [] and r['remaining'] == ['IMG_20260804_0001.jpg']
                     and 'XAI_API_KEY' in (first_error(r) or ''),
                     f"uploaded {originals(r)}, left {r['remaining']}, error {first_error(r)}")

    with sandbox(['topaz_IMG_20260804_0001.jpg'], xai_key='stub', topaz_key=None) as r:
        report.check('topaz_ without a Topaz key fails that photo and keeps it',
                     originals(r) == [] and r['remaining'] == ['IMG_20260804_0001.jpg']
                     and 'TOPAZ_API_KEY' in (first_error(r) or ''),
                     f"uploaded {originals(r)}, left {r['remaining']}, error {first_error(r)}")

    with sandbox(['topaz_IMG_20260804_0001.jpg', 'IMG_20260804_0002.jpg'],
                 xai_key='stub', topaz_key=None) as r:
        report.check('one unusable topaz_ photo does not stop the rest of the batch',
                     originals(r) == ['IMG_20260804_0002.jpg'],
                     f"uploaded {originals(r)}")

    with sandbox(['topaz_IMG_20260804_0001.jpg'], xai_key='stub', topaz_key='stub') as r:
        report.check('both keys present runs the Topaz route',
                     originals(r) == ['IMG_20260804_0001.jpg'],
                     f"uploaded {originals(r)}")

    return report.finish()


if __name__ == '__main__':
    sys.exit(main())
