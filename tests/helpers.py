"""Builders for fake Snapchat exports used by the tests."""

from __future__ import annotations

import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw


def make_jpeg(path: Path, size=(720, 1280), color=(40, 90, 160)) -> Path:
    Image.new("RGB", size, color).save(path, "JPEG", quality=92)
    return path


def make_overlay(path: Path, size=(1080, 1920), as_webp=False) -> Path:
    """Transparent overlay with a white box in the top-left quarter."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([0, 0, size[0] // 2, size[1] // 4], fill=(255, 255, 255, 255))
    im.save(path, "WEBP" if as_webp else "PNG", lossless=True)
    return path


def make_video(path: Path, seconds=2, size=(720, 1280), rotate: int | None = None) -> Path:
    """Short H.264 clip with audio, full range like Snapchat's own files."""
    w, h = size
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuvj420p", "-color_range", "pc",
        "-c:a", "aac", "-shortest",
    ]
    if rotate is not None:
        cmd += ["-metadata:s:v:0", f"rotate={rotate}"]
    cmd += ["-metadata", "creation_time=2026-02-01T10:00:12Z", str(path)]
    subprocess.run(cmd, check=True)
    if rotate is not None:
        # newer ffmpeg ignores the rotate tag on output; set the display matrix instead
        tmp = path.with_suffix(".rot.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotate), "-i", str(path),
                        "-c", "copy", str(tmp)], check=True)
        tmp.replace(path)
    return path


def snap_row(taken: datetime, kind: str, lat=None, lon=None, link=None) -> dict:
    row = {
        "Date": taken.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Media Type": kind,
        "Location": f"Latitude, Longitude: {lat}, {lon}" if lat is not None else "Latitude, Longitude: 0.0, 0.0",
    }
    if link:
        row["Download Link"] = link
    return row


def write_export_zip(path: Path, files: list[tuple[str, Path, datetime]], rows: list[dict], json_in_zip=True) -> Path:
    """files: (member name, source file, capture time recorded as the member's DOS time)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        if json_in_zip:
            zf.writestr("json/memories_history.json", json.dumps({"Saved Media": rows}))
        for member, src, taken in files:
            info = zipfile.ZipInfo(member, date_time=taken.astimezone(timezone.utc).timetuple()[:6])
            zf.writestr(info, src.read_bytes())
    return path


def exif(path: Path, *tags: str) -> dict:
    out = subprocess.run(["exiftool", "-j", "-n", *[f"-{t}" for t in tags], str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)[0]


def video_stream_md5(path: Path) -> str:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def frame_pixel(path: Path, x: int, y: int, t: float = 0.5) -> tuple:
    png = path.with_suffix(".frame.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                    "-vf", "scale=in_color_matrix=bt709:out_range=pc,format=rgb24", str(png)], check=True)
    with Image.open(png) as im:
        return im.convert("RGB").getpixel((x, y))
