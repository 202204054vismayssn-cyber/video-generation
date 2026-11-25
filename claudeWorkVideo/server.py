"""Authenticated multipart API, a durable queue and one isolated render worker."""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import edge_tts
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from duration import DURATION_TOLERANCE_SECONDS, main_scene_count, resolve_duration
from media import inspect_media, validate_assignments
from models import VideoConfig
from store import JobStore, save_json

PACKAGE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("VIDEO_API_ROOT", str(PACKAGE / "data"))).resolve()
TOKEN = os.environ.get("VIDEO_API_TOKEN", "")
MAX_UPLOAD_BYTES = 90 * 1024 * 1024
MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_PENDING_JOBS = 10
JOB_TIMEOUT_SECONDS = 7200
STORE = JobStore(ROOT)
SUBMIT_LOCK = asyncio.Lock()
BEARER = HTTPBearer(auto_error=False)
VOICE_CACHE = {"time": 0, "voices": []}
VOICE_LOCK = asyncio.Lock()


def authenticate(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(BEARER)],
):
    if (
        not credentials
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials.encode(), TOKEN.encode())
    ):
        raise HTTPException(
            401,
            "A valid Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def job_record(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404, "Job not found.")
    value = STORE.get(job_id)
    if value is None:
        raise HTTPException(404, "Job not found.")
    return value


class RenderWorker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="video-render-worker", daemon=True
        )
        self.process = None

    @staticmethod
    def terminate(process):
        if process and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)

    def run(self):
        while not self.stop_event.is_set():
            job_id = STORE.claim()
            if job_id is None:
                self.stop_event.wait(0.5)
                continue
            folder = ROOT / "jobs" / job_id
            try:
                command = [
                    sys.executable,
                    "-u",
                    str(PACKAGE / "runner.py"),
                    "--job",
                    str(folder),
                ]
                # Only the test harness can select synthetic inference; HTTP config cannot.
                if os.environ.get("VIDEO_API_SELF_TEST") == "1":
                    command.append("--self-test")
                with (folder / "job.log").open("a", encoding="utf-8") as log:
                    self.process = subprocess.Popen(
                        command,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=PACKAGE,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
                    while self.process.poll() is None:
                        if self.stop_event.wait(0.5):
                            self.terminate(self.process)
                            STORE.update(
                                job_id,
                                status="failed",
                                stage="interrupted",
                                error="The API was stopped during rendering. Submit the request again.",
                            )
                            break
                        if time.monotonic() > deadline:
                            self.terminate(self.process)
                            STORE.update(
                                job_id,
                                status="failed",
                                stage="timeout",
                                error="Rendering exceeded the two-hour job limit. Reduce duration, FPS or resolution.",
                            )
                            break
                    record = STORE.get(job_id)
                    if record["status"] == "running":
                        STORE.update(
                            job_id,
                            status="failed",
                            stage="failed",
                            error="The render process stopped unexpectedly. GET this job's /log for details.",
                        )
            except Exception as exc:
                logging.exception("Render worker error for %s", job_id)
                STORE.update(
                    job_id,
                    status="failed",
                    stage="failed",
                    error=f"Worker error: {type(exc).__name__}: {exc}",
                )
            finally:
                self.process = None

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=25)


@asynccontextmanager
async def lifespan(app):
    if len(TOKEN) < 24:
        raise RuntimeError(
            "Set VIDEO_API_TOKEN to a random value of at least 24 characters. The notebook generates it for you."
        )
    lock = (ROOT / "api.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("Another API process already owns this job folder.")
    STORE.recover()
    worker = RenderWorker()
    app.state.worker = worker
    worker.thread.start()
    try:
        yield
    finally:
        await asyncio.to_thread(worker.stop)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


app = FastAPI(
    title="Savvy Video API",
    version="1.3.3",
    lifespan=lifespan,
    description="Upload config JSON text and actual media in one multipart request. Generation uses local Gemma2:2b, online Edge TTS and FFmpeg. Inspect /options and /schema before creating jobs.",
)
AUTH = [Depends(authenticate)]

# The tunnel-hosted studio uses the same origin. Allow a locally served studio too.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?",
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/studio", StaticFiles(directory=PACKAGE / "studio", html=True), name="studio")


@app.get("/health", tags=["Service"])
def health():
    return {
        "status": "ok",
        "version": app.version,
        "self_test": os.environ.get("VIDEO_API_SELF_TEST") == "1",
    }


@app.get("/schema", dependencies=AUTH, tags=["Configuration"])
def schema():
    """Complete nested configuration schema, including defaults, descriptions and limits."""
    return VideoConfig.model_json_schema()


@app.get("/options", dependencies=AUTH, tags=["Configuration"])
def options():
    example = json.loads((PACKAGE / "example_config.json").read_text())
    storyboard_example = json.loads((PACKAGE / "example_storyboard_config.json").read_text())
    defaults = VideoConfig(
        prompt="Replace with your video topic and instructions."
    ).model_dump(mode="json")
    return dict(
        defaults=defaults,
        descriptions=VideoConfig.model_json_schema(),
        example_with_uploads=example,
        example_storyboard_with_uploads=storyboard_example,
        motions={
            "none": "Stationary still image.",
            "zoom_in": "Slowly move closer to a still image.",
            "zoom_out": "Slowly move away from a still image.",
            "pan_left": "Gentle pan toward the left.",
            "pan_right": "Gentle pan toward the right.",
        },
        fit={
            "cover": "Fill the frame, cropping edges if necessary.",
            "contain": "Keep the entire asset, with plain margins if necessary.",
        },
        upload_limits=dict(
            total_bytes=MAX_UPLOAD_BYTES, per_file_bytes=MAX_FILE_BYTES, max_files=16
        ),
        behaviour=dict(
            background="Shared backgrounds fill the frame. Scene overrides take priority.",
            video_audio="Always discarded; only narration and optional instrumental music are mixed.",
            photo_motion="Only static images with cover. GIF/video retains its own animation.",
            closing="The bundled Savvy Asia card is used by default. Set closing_slide.enabled to false to omit it.",
            duration=f"The final runtime, including the closing slide, must be within ±{DURATION_TOLERANCE_SECONDS} seconds of the target.",
            voices="English scripts. Tone is approximated with rate, pitch and volume; Edge TTS is online.",
            progress="completed_stages counts finished stages, not an estimate of elapsed render time.",
        ),
    )


@app.get("/voices", dependencies=AUTH, tags=["Configuration"])
async def voices(locale: str = ""):
    """Current Edge catalog, cached for one hour; optionally filter by locale, such as en-US."""
    async with VOICE_LOCK:
        if time.time() - VOICE_CACHE["time"] > 3600:
            try:
                VOICE_CACHE.update(
                    voices=await asyncio.wait_for(edge_tts.list_voices(), timeout=30),
                    time=time.time(),
                )
            except Exception as exc:
                raise HTTPException(
                    503,
                    "Could not reach the Edge voice catalog. Check internet access and retry.",
                ) from exc
    return [
        voice
        for voice in VOICE_CACHE["voices"]
        if not locale or voice.get("Locale", "").casefold() == locale.casefold()
    ]


@app.post(
    "/jobs",
    status_code=202,
    dependencies=AUTH,
    tags=["Jobs"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["config"],
                        "properties": {
                            "config": {
                                "type": "string",
                                "description": "JSON TEXT matching GET /schema. Optional files use the exact names in file_field.",
                                "example": '{"prompt":"Create a 60-second explainer about clear team handovers."}',
                            },
                            "company_logo": {"type": "string", "format": "binary"},
                            "background_video": {"type": "string", "format": "binary"},
                            "scene_2_media": {"type": "string", "format": "binary"},
                            "closing_slide": {"type": "string", "format": "binary"},
                        },
                    }
                }
            },
        }
    },
)
async def create_job(request: Request):
    """Submit config + named files. Additional File fields are supported; asset IDs are not used."""
    if "multipart/form-data" not in request.headers.get("content-type", "").lower():
        raise HTTPException(
            415,
            "Send multipart/form-data with config as JSON text and media as files.",
        )
    try:
        length = int(request.headers["content-length"])
    except (KeyError, ValueError):
        raise HTTPException(
            411,
            "The multipart request must include a Content-Length header.",
        )
    if not 0 < length <= MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, "The complete multipart upload must be at most 90 MiB."
        )
    job_id = uuid.uuid4().hex
    folder = ROOT / "jobs" / job_id
    try:
        async with request.form(
            max_files=16, max_fields=1, max_part_size=256 * 1024
        ) as form:
            pairs = list(form.multi_items())
            if len({key for key, _ in pairs}) != len(pairs):
                raise HTTPException(422, "Each form-data field name must be unique.")
            raw_config = form.get("config")
            if not isinstance(raw_config, str):
                raise HTTPException(
                    422, "Add a config field of type Text containing valid JSON."
                )
            try:
                config = VideoConfig.model_validate_json(raw_config)
                target = config.resolved_duration()
                closing = config.effective_closing()
                scene_count = config.planned_scene_count()
                if any(item.scene > scene_count for item in config.scene_media) or any(
                    i > scene_count for i in config.voice.scene_tones
                ):
                    raise ValueError(
                        f"This target produces {scene_count} main scenes. A scene assignment is out of range."
                    )
            except ValidationError as exc:
                raise HTTPException(
                    422, detail=exc.errors(include_context=False, include_url=False)
                ) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            files = {key: value for key, value in pairs if key != "config"}
            if any(not isinstance(value, UploadFile) for value in files.values()):
                raise HTTPException(
                    422, "All fields except config must be File fields."
                )
            expected = config.referenced_files()
            if set(files) != expected:
                raise HTTPException(
                    422,
                    f"File fields do not match config. Missing: {sorted(expected-set(files))}; unused: {sorted(set(files)-expected)}.",
                )
            (folder / "inputs").mkdir(parents=True)
            assets, total = {}, 0
            for key, uploaded in files.items():
                suffix = Path(uploaded.filename or "").suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{2,5}", suffix):
                    raise HTTPException(
                        422, f"{key}: supply a filename with a supported extension."
                    )
                path = folder / "inputs" / (key + suffix)
                count = 0
                with path.open("wb") as output:
                    while chunk := await uploaded.read(1024 * 1024):
                        count += len(chunk)
                        total += len(chunk)
                        if count > MAX_FILE_BYTES or total > MAX_UPLOAD_BYTES:
                            raise HTTPException(
                                413, "Upload exceeds the per-file or total size limit."
                            )
                        output.write(chunk)
                try:
                    metadata = await asyncio.to_thread(inspect_media, path)
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    raise HTTPException(422, f"{key}: {exc}") from exc
                assets[key] = dict(
                    path=str(path),
                    original_filename=uploaded.filename,
                    bytes=count,
                    metadata=metadata,
                )
            try:
                validate_assignments(config, assets)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            stored_config = config.model_dump(mode="json")
            if config.storyboard and config.closing_slide is not None:
                # Keep duration and narration inherited from the storyboard when the worker reloads this job.
                stored_config["closing_slide"] = closing.model_dump(mode="json")
            save_json(folder / "config.json", stored_config)
            save_json(folder / "uploads.json", assets)
            async with SUBMIT_LOCK:
                if STORE.pending() >= MAX_PENDING_JOBS:
                    raise HTTPException(
                        429,
                        "The queue already contains ten jobs. Wait for a job to finish.",
                    )
                STORE.create(job_id)
        return dict(
            job_id=job_id,
            status="queued",
            requested_seconds=target,
            status_url=f"/jobs/{job_id}",
            video_url=f"/jobs/{job_id}/video",
            report_url=f"/jobs/{job_id}/report",
        )
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@app.get("/jobs", dependencies=AUTH, tags=["Jobs"])
def jobs():
    return STORE.recent()


@app.get("/jobs/{job_id}", dependencies=AUTH, tags=["Jobs"])
def status(job_id: str):
    return job_record(job_id)


@app.get("/jobs/{job_id}/video", dependencies=AUTH, tags=["Jobs"])
def video(job_id: str):
    record = job_record(job_id)
    if record["status"] != "completed":
        raise HTTPException(409, f"Video is not ready; status is {record['status']}.")
    path = ROOT / "jobs" / job_id / "output" / "generated_video.mp4"
    if not path.is_file():
        raise HTTPException(410, "Output was removed or the runtime storage was reset.")
    return FileResponse(path, media_type="video/mp4", filename=f"video_{job_id}.mp4")


@app.get("/jobs/{job_id}/report", dependencies=AUTH, tags=["Jobs"])
def report(job_id: str):
    record = job_record(job_id)
    folder = ROOT / "jobs" / job_id / "output"
    values = {"job": record}
    for name in [
        "quality_report",
        "duration_fit_report",
        "scene_repair_log",
        "storyboard_display_log",
        "supplied_storyboard",
        "story",
        "narration_edits",
        "narration_cues",
        "closing_narration",
        "timeline",
    ]:
        path = folder / (name + ".json")
        if path.exists():
            values[name] = json.loads(path.read_text())
    return values


@app.get(
    "/jobs/{job_id}/log",
    dependencies=AUTH,
    response_class=PlainTextResponse,
    tags=["Jobs"],
)
def log(job_id: str):
    job_record(job_id)
    path = ROOT / "jobs" / job_id / "job.log"
    if not path.exists():
        return "The job has not started yet."
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 100_000))
        return stream.read().decode("utf-8", errors="replace")
