"""Render one memory's files into the finished archive file.

No overlay: the original is copied and only metadata is added. Photo + overlay:
Pillow, JPEG quality 95, 4:4:4. Video + overlay: ffmpeg, H.264 CRF 18, audio
copied. Capture time is written in UTC (+00:00 offset on photos), plus GPS.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from .model import IMAGE, VIDEO, Memory

log = logging.getLogger(__name__)


@dataclass
class EncodeOptions:
    crf: int = 18
    preset: str = "medium"
    jpeg_quality: int = 95
    write_gps: bool = True
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    exiftool: str = "exiftool"


class MediaError(Exception):
    pass


# --------------------------------------------------------------------------- sniffing

_FTYP_IMAGE_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1", b"avif"}


def sniff(path: Path) -> str | None:
    """Identify a file by its first bytes: jpg, png, webp, gif, heic, avif, mp4, mov or zip."""
    with open(path, "rb") as fh:
        head = fh.read(64)
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"GIF8",):
        return "gif"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _FTYP_IMAGE_BRANDS:
            return "avif" if brand == b"avif" else "heic"
        return "mov" if brand == b"qt  " else "mp4"
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free"):
        return "mov"
    return None


VIDEO_TYPES = {"mp4", "mov"}
IMAGE_TYPES = {"jpg", "png", "webp", "gif", "heic", "avif"}


def kind_of(file_type: str | None) -> str | None:
    if file_type in VIDEO_TYPES:
        return VIDEO
    if file_type in IMAGE_TYPES:
        return IMAGE
    return None


# --------------------------------------------------------------------------- tools


def _run(cmd: list[str], what: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise MediaError(f"{what}: {cmd[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{what}: timed out") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
        raise MediaError(f"{what} failed: " + " | ".join(tail))
    return proc


def probe_video(path: Path, opts: EncodeOptions) -> dict:
    proc = _run(
        [opts.ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        "ffprobe",
        timeout=120,
    )
    info = json.loads(proc.stdout or "{}")
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("no video stream")
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    if not rotation:
        rotation = int(float((video.get("tags") or {}).get("rotate", 0) or 0))
    width, height = int(video["width"]), int(video["height"])
    if abs(rotation) % 180 == 90:
        width, height = height, width
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "pix_fmt": video.get("pix_fmt") or "",
        "color_range": video.get("color_range") or "",
        "color_primaries": video.get("color_primaries") or "",
        "color_transfer": video.get("color_transfer") or "",
        "color_space": video.get("color_space") or "",
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
        "creation_time": ((info.get("format") or {}).get("tags") or {}).get("creation_time"),
    }


# --------------------------------------------------------------------------- compositing


def _load_overlay_rgba(path: Path):
    from PIL import Image

    with Image.open(path) as im:
        im.seek(0)  # animated WebP/PNG overlays: use the first frame
        return im.convert("RGBA")


def composite_image(main: Path, overlay: Path, out: Path, opts: EncodeOptions) -> None:
    from PIL import Image, ImageOps

    with Image.open(main) as im:
        icc = im.info.get("icc_profile")
        exif = im.getexif()
        base = ImageOps.exif_transpose(im).convert("RGBA")
    layer = _load_overlay_rgba(overlay)
    if layer.size != base.size:
        layer = layer.resize(base.size, Image.Resampling.LANCZOS)
    merged = Image.alpha_composite(base, layer).convert("RGB")
    if exif:
        exif[0x0112] = 1  # orientation is baked in now
    merged.save(
        out,
        "JPEG",
        quality=opts.jpeg_quality,
        subsampling=0,
        optimize=True,
        icc_profile=icc,
        exif=exif.tobytes() if exif else b"",
    )


_SWS_MATRIX = {"bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601", "fcc": "fcc", "smpte240m": "smpte240m", "bt2020nc": "bt2020"}


def video_filter(info: dict, shortest: bool) -> str:
    """Overlay input 1 on input 0.

    8-bit video is composited in RGB so overlay colours stay exact on full-range footage.
    """
    w, h = info["width"], info["height"]
    full = info["color_range"] == "pc" or info["pix_fmt"].startswith("yuvj")
    rng = "pc" if full else "tv"
    matrix = _SWS_MATRIX.get(info["color_space"], "bt709")
    end = ":shortest=1" if shortest else ""
    even = "" if (w % 2 == 0 and h % 2 == 0) else "trunc(iw/2)*2:trunc(ih/2)*2:"
    overlay_in = f"[1:v]scale={w}:{h}:flags=lanczos,format=rgba[ov];"
    if "10" in info["pix_fmt"] or "12" in info["pix_fmt"]:
        return overlay_in + f"[0:v][ov]overlay=0:0:format=yuv420p10{end},scale={even}out_range={rng},format=yuv420p10le[v]"
    return (
        f"[0:v]scale=in_range={rng}:in_color_matrix={matrix},format=gbrp[base];"
        + overlay_in
        + f"[base][ov]overlay=0:0:format=gbrp{end}[comp];"
        f"[comp]scale={even}out_range={rng}:out_color_matrix={matrix},format=yuv420p[v]"
    )


def composite_video(main: Path, overlay: Path, overlay_type: str | None, out: Path, opts: EncodeOptions) -> dict:
    info = probe_video(main, opts)
    inputs = ["-i", str(main)]
    if overlay_type in VIDEO_TYPES:
        inputs += ["-stream_loop", "-1", "-i", str(overlay)]
    else:
        # ffmpeg's WebP decoder chokes on some of Snapchat's overlays; hand it a clean PNG
        png = out.parent / "overlay-clean.png"
        _load_overlay_rgba(overlay).save(png, "PNG")
        inputs += ["-i", str(png)]
    # a still overlay repeats its only frame until the video ends; a looping one must be cut
    graph = video_filter(info, shortest=overlay_type in VIDEO_TYPES)

    if "10" in info["pix_fmt"] or "12" in info["pix_fmt"]:
        codec = ["-c:v", "libx265", "-preset", opts.preset, "-crf", str(opts.crf + 2), "-tag:v", "hvc1"]
    else:
        codec = ["-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf), "-profile:v", "high"]
    full = info["color_range"] == "pc" or info["pix_fmt"].startswith("yuvj")
    color = ["-color_range", "pc" if full else "tv"]
    for flag, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"), ("-colorspace", "color_space")):
        if info[key] and info[key] != "unknown":
            color += [flag, info[key]]
    if info["color_space"] in ("", "unknown"):
        color += ["-colorspace", "bt709"]  # untagged source; video_filter() assumed BT.709

    cmd = [opts.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", *codec, *color]
    tail = ["-map_metadata", "0", "-map_chapters", "-1", "-movflags", "+faststart", "-f", "mp4", str(out)]
    try:
        _run(cmd + ["-c:a", "copy"] + tail, "ffmpeg overlay")
    except MediaError:
        log.debug("audio copy failed for %s; re-encoding audio", main.name)
        _run(cmd + ["-c:a", "aac", "-b:a", "192k"] + tail, "ffmpeg overlay")

    result = probe_video(out, opts)
    if info["duration"] and abs(result["duration"] - info["duration"]) > max(1.0, 0.1 * info["duration"]):
        raise MediaError(f"merged video is {result['duration']:.1f}s but the original is {info['duration']:.1f}s")
    result["source_creation_time"] = info.get("creation_time")
    return result


def video_has_alpha(path: Path, opts: EncodeOptions) -> bool:
    proc = _run([opts.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
                 "-of", "default=nw=1:nk=1", str(path)], "ffprobe", timeout=60)
    fmt = proc.stdout.strip()
    return fmt.startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya"))


# --------------------------------------------------------------------------- metadata


def _exif_time(mem: Memory) -> str:
    return mem.taken.astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S")


def write_image_metadata(path: Path, mem: Memory, opts: EncodeOptions) -> None:
    t = _exif_time(mem)
    args = [
        f"-DateTimeOriginal={t}",
        f"-CreateDate={t}",
        f"-ModifyDate={t}",
        "-OffsetTimeOriginal=+00:00",
        "-OffsetTimeDigitized=+00:00",
        "-OffsetTime=+00:00",
    ]
    args += _gps_image_args(mem) if opts.write_gps else []
    _run([opts.exiftool, "-q", "-q", "-m", "-n", "-overwrite_original", "-P", *args, str(path)], "exiftool")


def _gps_image_args(mem: Memory) -> list[str]:
    if mem.lat is None or mem.lon is None:
        return []
    return [
        f"-GPSLatitude={abs(mem.lat):.7f}",
        f"-GPSLatitudeRef={'N' if mem.lat >= 0 else 'S'}",
        f"-GPSLongitude={abs(mem.lon):.7f}",
        f"-GPSLongitudeRef={'E' if mem.lon >= 0 else 'W'}",
    ]


def write_video_metadata(path: Path, mem: Memory, opts: EncodeOptions, set_dates: bool) -> None:
    args = []
    if set_dates:
        t = _exif_time(mem) + "+00:00"
        for tag in ("CreateDate", "ModifyDate", "TrackCreateDate", "TrackModifyDate", "MediaCreateDate", "MediaModifyDate"):
            args.append(f"-QuickTime:{tag}={t}")
    if opts.write_gps and mem.lat is not None and mem.lon is not None:
        coords = f"{mem.lat:.7f}, {mem.lon:.7f}, 0"
        args += [f"-Keys:GPSCoordinates={coords}", f"-UserData:GPSCoordinates={coords}"]
    if args:
        _run([opts.exiftool, "-q", "-q", "-m", "-overwrite_original", "-P", "-api", "QuickTimeUTC=1", *args, str(path)], "exiftool")


def video_dates_need_fixing(creation_time: str | None, mem: Memory) -> bool:
    """Keep Snapchat's own video timestamps unless they are missing or clearly wrong."""
    if not creation_time:
        return True
    from .model import parse_snap_date

    dt = parse_snap_date(creation_time)
    return dt is None or dt.year < 2000 or abs(dt - mem.taken) > timedelta(days=1)


# --------------------------------------------------------------------------- verification


def verify_output(path: Path, kind: str, file_type: str, opts: EncodeOptions) -> None:
    if path.stat().st_size == 0:
        raise MediaError("output is empty")
    if kind == VIDEO:
        info = probe_video(path, opts)
        if info["width"] <= 0 or info["height"] <= 0:
            raise MediaError("output has no picture")
        return
    if file_type in ("heic", "avif"):
        return  # Pillow cannot decode these without plugins; exiftool already parsed it
    from PIL import Image

    with Image.open(path) as im:
        im.load()


# --------------------------------------------------------------------------- the whole job


@dataclass
class Rendered:
    path: Path
    ext: str
    kind: str
    overlay: str  # "merged", "none", or why the overlay could not be merged


def render(mem: Memory, main: Path, overlay: Path | None, workdir: Path, opts: EncodeOptions) -> Rendered:
    """Write the finished file for a memory into workdir.

    If the overlay can't be merged, the original is used and Rendered.overlay says why.
    """
    main_type = sniff(main)
    kind = kind_of(main_type)
    if kind is None:
        raise MediaError(f"unrecognised media file ({main_type or 'unknown type'})")
    if kind != mem.kind:
        log.debug("%s: file is a %s, not a %s", mem.label(), kind, mem.kind)
        mem.kind = kind

    status = "none"
    overlay_type = sniff(overlay) if overlay else None
    if overlay is not None:
        if kind_of(overlay_type) is None:
            status = f"skipped: overlay is not a readable image ({overlay_type or 'unknown type'})"
        elif overlay_type in VIDEO_TYPES and (kind == IMAGE or not video_has_alpha(overlay, opts)):
            status = "skipped: overlay is a video without transparency"
        if status != "none":
            log.warning("%s: %s; keeping the original", mem.label(), status)
            overlay = None

    out: Path | None = None
    if overlay is not None:
        try:
            if kind == IMAGE:
                out, ext = workdir / "out.jpg", ".jpg"
                composite_image(main, overlay, out, opts)
                write_image_metadata(out, mem, opts)
                verify_output(out, kind, "jpg", opts)
            else:
                out, ext = workdir / "out.mp4", ".mp4"
                result = composite_video(main, overlay, overlay_type, out, opts)
                # ffmpeg carried Snapchat's timestamps over; replace them only if they are bad
                write_video_metadata(out, mem, opts, set_dates=video_dates_need_fixing(result.get("source_creation_time"), mem))
                verify_output(out, kind, "mp4", opts)
            status = "merged"
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the original
            status = f"failed: {exc}"
            log.warning("%s: overlay merge failed (%s); keeping the original", mem.label(), exc)
            out = None

    if out is None:
        if kind == IMAGE:
            ext = f".{main_type}"
            out = workdir / f"plain{ext}"
            shutil.copyfile(main, out)
            write_image_metadata(out, mem, opts)
        else:
            ext = ".mov" if main_type == "mov" else ".mp4"
            out = workdir / f"plain{ext}"
            shutil.copyfile(main, out)
            info = probe_video(out, opts)
            write_video_metadata(out, mem, opts, set_dates=video_dates_need_fixing(info.get("creation_time"), mem))
        verify_output(out, kind, main_type or "", opts)
    return Rendered(out, ext, kind, status)
