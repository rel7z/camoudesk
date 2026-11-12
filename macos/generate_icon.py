#!/usr/bin/env python3
"""Generate crisp macOS AppIcon.icns for CamouDesk using Cocoa/AppKit + iconutil."""

import argparse
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zlib


def _render_cocoa_icon(png_path: Path, size: int = 1024) -> bool:
    """Render anti-aliased vector squircle + geometric fox emblem via AppKit."""
    try:
        from AppKit import (
            NSBitmapImageRep,
            NSBezierPath,
            NSColor,
            NSDeviceRGBColorSpace,
            NSGradient,
            NSGraphicsContext,
            NSMakePoint,
            NSMakeRect,
            NSPNGFileType,
        )
    except ImportError:
        return False

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    # Background rounded macOS squircle
    pad = size * 0.08
    rect = NSMakeRect(pad, pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.20
    bg_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)

    top_col = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.13, 0.22, 1.0)
    bot_col = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.04, 0.06, 0.11, 1.0)
    grad = NSGradient.alloc().initWithStartingColor_endingColor_(bot_col, top_col)
    grad.drawInBezierPath_angle_(bg_path, 90.0)

    # Subtle outer border highlight
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.25, 0.33, 0.50, 0.45).setStroke()
    bg_path.setLineWidth_(size * 0.008)
    bg_path.stroke()

    # Fox geometric silhouette (center-aligned, Cocoa origin is bottom-left)
    cx = size * 0.50
    fox_main = NSBezierPath.bezierPath()
    fox_main.moveToPoint_(NSMakePoint(cx, size * 0.21))              # Chin tip
    fox_main.lineToPoint_(NSMakePoint(size * 0.24, size * 0.48))     # Left cheek
    fox_main.lineToPoint_(NSMakePoint(size * 0.28, size * 0.77))     # Left ear tip
    fox_main.lineToPoint_(NSMakePoint(size * 0.41, size * 0.63))     # Left forehead
    fox_main.lineToPoint_(NSMakePoint(size * 0.59, size * 0.63))     # Right forehead
    fox_main.lineToPoint_(NSMakePoint(size * 0.72, size * 0.77))     # Right ear tip
    fox_main.lineToPoint_(NSMakePoint(size * 0.76, size * 0.48))     # Right cheek
    fox_main.closePath()

    fox_top = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.49, 0.14, 1.0)
    fox_bot = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.86, 0.28, 0.08, 1.0)
    fox_grad = NSGradient.alloc().initWithStartingColor_endingColor_(fox_bot, fox_top)
    fox_grad.drawInBezierPath_angle_(fox_main, 90.0)

    # Bright snout shield accent
    snout = NSBezierPath.bezierPath()
    snout.moveToPoint_(NSMakePoint(cx, size * 0.21))
    snout.lineToPoint_(NSMakePoint(size * 0.36, size * 0.41))
    snout.lineToPoint_(NSMakePoint(cx, size * 0.36))
    snout.lineToPoint_(NSMakePoint(size * 0.64, size * 0.41))
    snout.closePath()
    NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.94, 0.88, 0.95).setFill()
    snout.fill()

    # Dark eyes
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.06, 0.08, 0.14, 1.0).setFill()
    for ex in (size * 0.40, size * 0.60):
        eye_r = size * 0.026
        eye = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(ex - eye_r, size * 0.47 - eye_r, eye_r * 2, eye_r * 2)
        )
        eye.fill()

    NSGraphicsContext.restoreGraphicsState()
    png_data = rep.representationUsingType_properties_(NSPNGFileType, None)
    png_data.writeToFile_atomically_(str(png_path), True)
    return True


def _render_fallback_png(png_path: Path, size: int = 256) -> None:
    """Minimal stdlib PNG fallback when AppKit is unavailable."""
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            # Dark navy square with orange diamond center
            dx = abs(x - size // 2)
            dy = abs(y - size // 2)
            if dx + dy < size // 3:
                raw.extend((249, 115, 22, 255))
            else:
                raw.extend((15, 23, 42, 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")
    png_path.write_bytes(png)


def build_icns(output_icns: Path) -> None:
    output_icns.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        master_png = tmp_path / "icon_1024.png"
        if not _render_cocoa_icon(master_png, 1024):
            _render_fallback_png(master_png, 256)

        iconset = tmp_path / "AppIcon.iconset"
        iconset.mkdir()

        specs = [
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
            (1024, "icon_512x512@2x.png"),
        ]
        for px, filename in specs:
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(master_png), "--out", str(iconset / filename)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(output_icns)],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AppIcon.icns")
    parser.add_argument("--out", required=True, help="Output .icns path")
    args = parser.parse_args()
    build_icns(Path(args.out).resolve())


if __name__ == "__main__":
    main()
