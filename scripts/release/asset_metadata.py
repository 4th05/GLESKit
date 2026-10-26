#!/usr/bin/env python3
"""Lossless metadata audit/scrub for named public GLESKit image assets.

Pixel/chunk data is preserved. Only PNG text/EXIF/time metadata and SVG metadata
elements are removed; no image is generated, recompressed or sent to a service.
"""
import argparse
import base64
from pathlib import Path
import re
import struct
import zlib

BRAND_ASSETS = ("apps/web/src/assets/GESKit_icon.svg", "apps/web/src/assets/GESKit_icon_no_bg.png")
DOCUMENTATION_ASSETS = ("docs/assets/gleskit-architecture.png",)
PUBLIC_ASSETS = BRAND_ASSETS + DOCUMENTATION_ASSETS
PNG = b"\x89PNG\r\n\x1a\n"
PRIVATE_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}
EMBEDDED_PNG = re.compile(rb"data:image/png;base64,([A-Za-z0-9+/=\r\n]+)")
SVG_METADATA = re.compile(rb"<(?:[A-Za-z0-9_]+:)?metadata\b[^>]*>.*?</(?:[A-Za-z0-9_]+:)?metadata\s*>", re.DOTALL | re.IGNORECASE)


def png_chunks(data):
    if not data.startswith(PNG): raise ValueError("Invalid PNG signature")
    offset = len(PNG)
    while offset < len(data):
        if offset + 12 > len(data): raise ValueError("Truncated PNG")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data): raise ValueError("Truncated PNG chunk")
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        expected = struct.unpack(">I", data[offset + 8 + length:end])[0]
        if zlib.crc32(kind + body) & 0xffffffff != expected: raise ValueError("Invalid PNG checksum")
        yield kind, data[offset:end]
        offset = end
        if kind == b"IEND":
            if offset != len(data): raise ValueError("Unexpected bytes after PNG end")
            return
    raise ValueError("Missing PNG end")


def strip_png(data):
    return PNG + b"".join(chunk for kind, chunk in png_chunks(data) if kind not in PRIVATE_CHUNKS)


def sanitize(data):
    if data.startswith(PNG): return strip_png(data)
    def replace(match):
        decoded = base64.b64decode(match[1], validate=False)
        return b"data:image/png;base64," + base64.b64encode(strip_png(decoded))
    return EMBEDDED_PNG.sub(replace, SVG_METADATA.sub(b"", data))


def has_private_metadata(data):
    if data.startswith(PNG):
        return any(kind in PRIVATE_CHUNKS for kind, _ in png_chunks(data))
    if SVG_METADATA.search(data): return True
    return any(has_private_metadata(base64.b64decode(match[1], validate=False)) for match in EMBEDDED_PNG.finditer(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--sanitize", action="store_true", help="Strip metadata from the named public image files in place")
    args = parser.parse_args()
    for name in PUBLIC_ASSETS:
        path = args.root / name
        if path.is_symlink() or not path.resolve().is_relative_to(args.root.resolve()):
            raise SystemExit("Public image asset is not a regular project file")
        data = path.read_bytes()
        if args.sanitize:
            path.write_bytes(sanitize(data))  # Lossless mechanical binary rewrite; pixel data is untouched.
            data = path.read_bytes()
        if has_private_metadata(data): raise SystemExit("Public image metadata requires sanitization: " + name)
    print("Public image metadata: PASS (pixel data preserved)")


if __name__ == "__main__":
    main()
