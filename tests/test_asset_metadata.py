"""Public image exports cannot retain design-account/EXIF/text metadata."""
import base64
from pathlib import Path
import struct
import unittest
import zlib

from scripts.release.asset_metadata import BRAND_ASSETS, DOCUMENTATION_ASSETS, PUBLIC_ASSETS, PNG, has_private_metadata, png_chunks, sanitize
from scripts.release.privacy_gate import inspect

ROOT = Path(__file__).resolve().parents[1]


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


class AssetMetadataTests(unittest.TestCase):
    def fixture(self):
        return PNG + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + \
            chunk(b"iTXt", b"synthetic-design-account-metadata") + chunk(b"eXIf", b"synthetic-exif") + \
            chunk(b"IDAT", zlib.compress(b"\0\xff\x00\x00")) + chunk(b"IEND", b"")

    def test_scrub_preserves_exact_pixel_chunks_without_recompression(self):
        before = self.fixture(); after = sanitize(before)
        self.assertTrue(has_private_metadata(before))
        self.assertFalse(has_private_metadata(after))
        for kind in (b"IHDR", b"IDAT", b"IEND"):
            self.assertEqual([data for name, data in png_chunks(before) if name == kind],
                             [data for name, data in png_chunks(after) if name == kind])

    def test_embedded_pngs_are_audited_and_scrubbed_too(self):
        data = b'<svg><image href="data:image/png;base64,' + base64.b64encode(self.fixture()) + b'"/></svg>'
        self.assertTrue(has_private_metadata(data))
        self.assertFalse(has_private_metadata(sanitize(data)))
        self.assertIn("brand-image-metadata-requires-review", inspect(data, BRAND_ASSETS[0]))
        self.assertNotIn("synthetic-design-account", str(inspect(data, BRAND_ASSETS[0])))

    def test_actual_public_image_assets_have_no_private_metadata(self):
        for name in PUBLIC_ASSETS:
            with self.subTest(name=name):
                self.assertFalse(has_private_metadata((ROOT / name).read_bytes()))

    def test_documentation_images_reject_metadata_and_malformed_exports(self):
        for name in DOCUMENTATION_ASSETS:
            with self.subTest(name=name):
                self.assertIn("brand-image-metadata-requires-review", inspect(self.fixture(), name))
                self.assertIn("brand-image-metadata-cannot-be-audited", inspect(PNG + b"truncated", name))
                self.assertEqual(inspect(sanitize(self.fixture()), name), [])

    def test_malformed_png_fails_closed(self):
        data = PNG + b"truncated"
        with self.assertRaises(ValueError): sanitize(data)
        self.assertIn("brand-image-metadata-cannot-be-audited", inspect(data, BRAND_ASSETS[1]))
