"""Validate uploaded content before placing a request in the render queue."""

import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps


def inspect_media(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            count = getattr(image, "n_frames", 1)
            if (
                max(width, height) > 10000
                or width * height * count > 200_000_000
                or count > 600
            ):
                raise ValueError(
                    "Image/animation is too large. Resize or shorten it before uploading."
                )
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise ValueError("Unsupported image content.")
            return dict(
                kind="animation" if count > 1 else "image",
                width=width,
                height=height,
                frames=count,
                format=image.format,
            )
    if suffix not in {
        ".mp4",
        ".mov",
        ".webm",
        ".mkv",
        ".mp3",
        ".wav",
        ".m4a",
        ".aac",
        ".ogg",
        ".flac",
    }:
        raise ValueError(
            "Use JPG, PNG, WebP, GIF, MP4, MOV, WebM, MKV, or an instrumental audio file."
        )
    probe = shutil.which("ffprobe")
    if not probe:
        raise ValueError("FFprobe is missing. Rerun the dependency-installation cell.")
    result = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("FFprobe could not decode this media file.")
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    if not 0 < duration <= 3600:
        raise ValueError("Media duration must be between zero and one hour.")
    video = next(
        (
            stream
            for stream in data["streams"]
            if stream["codec_type"] == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video:
        if video["width"] * video["height"] > 3840 * 2160:
            raise ValueError("Resize input video to 4K or below before uploading.")
        return dict(
            kind="video",
            duration_seconds=duration,
            width=video["width"],
            height=video["height"],
        )
    if any(stream["codec_type"] == "audio" for stream in data["streams"]):
        return dict(kind="audio", duration_seconds=duration)
    raise ValueError("No usable video or audio stream was found.")


def validate_assignments(config, assets):
    expected = config.referenced_files()
    if expected != set(assets):
        missing, unused = sorted(expected - set(assets)), sorted(set(assets) - expected)
        raise ValueError(
            f"File fields do not match config. Missing: {missing}; unused: {unused}."
        )
    for spec in config.media_specs():
        metadata = assets[spec.file_field]["metadata"]
        kind = metadata["kind"]
        if kind == "audio":
            raise ValueError(
                f"{spec.file_field}: audio cannot be used as visual media."
            )
        if kind != "image" and spec.motion != "none":
            raise ValueError(
                f"{spec.file_field}: photo motion applies only to static images."
            )
        if kind != "video" and (
            spec.start_seconds != 0 or spec.end_seconds is not None
        ):
            raise ValueError(f"{spec.file_field}: start/end trim video clips only.")
        if kind == "video":
            duration = metadata["duration_seconds"]
            if spec.start_seconds >= duration or (
                spec.end_seconds is not None and spec.end_seconds > duration + 0.01
            ):
                raise ValueError(
                    f"{spec.file_field}: trim range is outside the source video's duration."
                )
    if config.brand.logo_file_field:
        logo_asset = assets[config.brand.logo_file_field]
        if logo_asset["metadata"]["kind"] != "image" or logo_asset["metadata"]["format"] not in {
            "JPEG",
            "PNG",
            "WEBP",
        }:
            raise ValueError("Company logo must be a static JPG, PNG or WebP image.")
    closing = config.effective_closing()
    if closing.enabled and closing.file_field:
        asset = assets[closing.file_field]
        if asset["metadata"]["kind"] != "image" or asset["metadata"]["format"] not in {
            "JPEG",
            "PNG",
            "WEBP",
        }:
            raise ValueError("Closing artwork must be a static JPG, PNG or WebP image.")
    if (
        config.music.enabled
        and config.music.file_field
        and assets[config.music.file_field]["metadata"]["kind"] != "audio"
    ):
        raise ValueError("The music field must contain an audio file, not a video.")
