"""Execute the trusted video stages in a fresh process for one job.

The bundled stages are the existing, tested notebook pipeline. They never come
from an HTTP request. Every process has its own namespace, media and audio.
"""

import argparse
import ast
import asyncio
import copy
import inspect
import json
import os
import sys
import traceback
import types
from pathlib import Path

from duration import DURATION_TOLERANCE_SECONDS
from models import VideoConfig
from store import JobStore, save_json

PACKAGE = Path(__file__).resolve().parent
STAGES = json.loads((PACKAGE / "pipeline_stages.json").read_text())
LABELS = {
    1: "environment",
    2: "settings",
    3: "story_planning",
    4: "narration_and_timing",
    5: "preparing_media",
    6: "typography",
    7: "motion_primitives",
    8: "scene_layouts",
    9: "mixing_audio",
    10: "rendering_scenes",
    11: "transitions",
    12: "final_render",
    13: "quality_validation",
}


async def execute(source, namespace, label):
    code = compile(source, label, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    result = eval(code, namespace)
    if inspect.isawaitable(result):
        await result


def configure(namespace, config, assets):
    def spec(item):
        value = dict(
            source=assets[item.file_field]["path"],
            fit=item.fit,
            motion=item.motion,
            loop=item.loop,
        )
        # Avoid sending irrelevant PNG/GIF trim arguments to the existing renderer.
        if assets[item.file_field]["metadata"]["kind"] == "video":
            value["start"] = item.start_seconds
            if item.end_seconds is not None:
                value["end"] = item.end_seconds
        return value

    backgrounds = (
        config.background
        if isinstance(config.background, list)
        else ([config.background] if config.background else [])
    )
    scene_media = {}
    for item in config.scene_media:
        scene_media.setdefault(item.scene, []).append(spec(item))
    closing = config.effective_closing()
    closing_narration = closing.narration
    if closing_narration is None:
        closing_narration = (
            "Connect to Savvy Asia." if config.voice.enabled and not closing.file_field else ""
        )
    namespace.update(
        VIDEO_PROMPT=config.prompt or config.storyboard.title,
        STORYBOARD=config.storyboard.model_dump(mode="json") if config.storyboard else None,
        REFERENCE_NOTES=config.reference_notes,
        TARGET_SECONDS_OVERRIDE=config.resolved_duration(),
        PREVIEW_MODE=False,
        WIDTH=config.output.width,
        HEIGHT=config.output.height,
        FPS=config.output.fps,
        CRF=config.output.crf,
        PRESET=config.output.preset,
        DURATION_TOLERANCE_SECONDS=DURATION_TOLERANCE_SECONDS,
        TRANSITION_SECONDS=config.transition_seconds,
        NARRATION_GAP_SECONDS=config.narration_gap_seconds,
        TEXT_BACKDROP_STRENGTH=config.text_backdrop_strength,
        BACKGROUND_WASH=config.background_wash,
        BACKGROUND_VIDEO=[spec(item) for item in backgrounds],
        SCENE_MEDIA=scene_media,
        DOWNLOAD_IMAGES=False,
        MEDIA_LAYOUT="background",
        STRICT_MEDIA=True,
        SHOW_MEDIA_PREVIEW=False,
        SHOW_NARRATION_PREVIEW=False,
        VOICE=config.voice.name,
        NARRATION_ENABLED=config.voice.enabled,
        NARRATION_TONE="professional" if config.storyboard else config.voice.tone,
        SCENE_TONES={} if config.storyboard else config.voice.scene_tones,
        BASE_RATE_PERCENT=config.voice.rate_percent,
        BASE_PITCH_HZ=config.voice.pitch_hz,
        BASE_VOLUME_PERCENT=config.voice.volume_percent,
        PROSODY_STRENGTH=config.voice.prosody_strength,
        MUSIC_ENABLED=config.music.enabled,
        MUSIC_FILE=(
            assets[config.music.file_field]["path"]
            if config.music.enabled and config.music.file_field
            else None
        ),
        SFX_ENABLED=False,
        COMPANY_NAME=config.brand.company_name,
        # Default tagline from config if present; will be derived from Story Title in Stage 3 if empty.
        BRAND_TAGLINE=config.brand.tagline,
        BRAND_LOGO_PATH=(
            assets[config.brand.logo_file_field]["path"]
            if config.brand.logo_file_field
            else str(PACKAGE / "default_logo_transparent.png")
        ),
        # burn-in captions are ON by default
        CAPTIONS_ENABLED=True,
        END_CARD=(
            dict(
                duration=closing.duration_seconds,
                source=(
                    assets[closing.file_field]["path"]
                    if closing.file_field
                    else str(PACKAGE / "default_closing_slide.png")
                ),
                message="",
                narration=closing_narration.strip(),
                requested_duration=closing.duration_seconds,
                background=closing.background,
                text_color="#000000",
            )
            if closing.enabled
            else None
        ),
        display=lambda *args, **kwargs: None,
    )


def synthetic_story(namespace):
    """An internal offline render fixture, never selectable through the public API."""
    brief = dict(
        duration_seconds=15,
        audience="teams",
        tone="professional",
        goal="Clear handovers",
        must_cover=[],
        assumptions=[],
        duration_was_explicit=True,
    )
    rows = [
        (
            "hook",
            "Keep work moving",
            "A clear handover helps the next person continue the work confidently.",
        ),
        (
            "documents",
            "Share the essentials",
            "Gather supporting documents and identify the latest version for your colleague.",
        ),
        (
            "cta",
            "Agree the next action",
            "Confirm receipt and record who will complete the next task.",
        ),
    ]
    story = dict(
        title="Offline render check",
        brief=brief,
        scenes=[
            dict(
                kind=kind,
                heading=heading,
                subtitle="A practical guide for your team",
                items=["Keep the next action clear"],
                callout="Keep the work moving",
                visual_query="office teamwork",
                top_right_header="Clear team handovers",
                segments=[dict(text=text, delivery="neutral")],
            )
            for kind, heading, text in rows
        ],
    )
    path = namespace["PROJECT_DIR"] / "self_test_story.json"
    save_json(path, story)
    namespace["CUSTOM_STORY_JSON"] = str(path)


async def run_job(job_path, self_test=False):
    job_path = Path(job_path).resolve()
    root = job_path.parent.parent
    store = JobStore(root)
    job_id = job_path.name
    config = VideoConfig.model_validate_json((job_path / "config.json").read_text())
    assets = json.loads((job_path / "uploads.json").read_text())
    os.environ["VIDEO_PROJECT_DIR"] = str(job_path)
    os.environ["VIDEO_SHARED_ROOT"] = str(root / "shared")
    os.environ["OLLAMA_MODELS"] = str(root / "shared" / "models")
    module = types.ModuleType("video_job_pipeline")
    sys.modules[module.__name__] = module
    namespace = module.__dict__
    for number in range(1, 14):
        stage = LABELS[number]
        store.update(job_id, stage=stage, completed_stages=number - 1)
        print(f"STAGE {number}/13: {stage}", flush=True)
        if number == 4 and self_test:
            tree = ast.parse(STAGES["4"])
            boundary = next(
                i
                for i, node in enumerate(tree.body)
                if isinstance(node, ast.If)
                and "TRANSITION_FRAMES" in ast.unparse(node.test)
            )
            await execute(
                ast.Module(body=tree.body[:boundary], type_ignores=[]),
                namespace,
                "speech_helpers",
            )
            import numpy as np

            async def speech(scene, index=None):
                if index is None:
                    # Closing narration has its own identifiable test tone.
                    seconds = float(
                        os.environ.get("VIDEO_API_SELF_TEST_CLOSING_SECONDS", "1.6")
                    )
                    count = round(seconds * namespace["SAMPLE_RATE"])
                    return (
                        0.1
                        * np.sin(
                            2
                            * np.pi
                            * 890
                            * np.arange(count)
                            / namespace["SAMPLE_RATE"]
                        )
                    ).astype(np.float32)
                count = round(
                    max(0.5, namespace["SPEECH_BUDGETS"][index] * 0.90)
                    * namespace["SAMPLE_RATE"]
                )
                audio = (
                    0.10
                    * np.sin(
                        2
                        * np.pi
                        * (170 + 100 * index)
                        * np.arange(count)
                        / namespace["SAMPLE_RATE"]
                    )
                ).astype(np.float32)
                scene["_segment_spans"] = [
                    dict(
                        text=" ".join(s["text"] for s in scene["segments"]),
                        delivery="neutral",
                        settings={},
                        start_sample=0,
                        end_sample=count,
                    )
                ]
                return audio

            namespace["synthesize_scene"] = speech
            namespace["unload_local_model"] = lambda: None
            await execute(
                ast.Module(body=tree.body[boundary:], type_ignores=[]),
                namespace,
                "speech_timing",
            )
        else:
            await execute(STAGES[str(number)], namespace, f"pipeline_stage_{number}")
        if number == 2:
            configure(namespace, config, assets)
            print(
                f"Render output: {namespace['WIDTH']}x{namespace['HEIGHT']} at "
                f"{namespace['FPS']} FPS | CPU frame rendering and libx264 encoding.",
                flush=True,
            )
            if self_test:
                print(
                    "INTERNAL SELF-TEST: synthetic story and tones; Gemma/Edge services are not tested.",
                    flush=True,
                )
                synthetic_story(namespace)
        if number == 3:
            # Derive the tagline from the AI story title if the user didn't specify one.
            if not namespace.get("BRAND_TAGLINE"):
                story_title = namespace.get("STORY", {}).get("title")
                if story_title:
                    namespace["BRAND_TAGLINE"] = story_title

            count = len(namespace["SCENES"])
            if any(item.scene > count for item in config.scene_media) or any(
                i > count for i in config.voice.scene_tones
            ):
                raise ValueError(
                    f"The generated story has {count} scenes; a media/tone assignment exceeds that count."
                )
    report = dict(namespace["checks"], self_test=self_test)
    save_json(job_path / "output" / "quality_report.json", report)
    result = dict(
        requested_seconds=namespace["TARGET_SECONDS"],
        actual_seconds=report["duration_seconds"],
        extension_seconds=report["duration_extension_seconds"],
        duration_delta_seconds=report["duration_delta_seconds"],
        duration_within_tolerance=report["duration_within_tolerance"],
        closing_seconds=report["closing_seconds"],
        closing_narration=report.get("closing_narration", ""),
        scene_count=len(namespace["SCENES"]),
        self_test=self_test,
        video_url=f"/jobs/{job_id}/video",
        report_url=f"/jobs/{job_id}/report",
    )
    store.update(
        job_id,
        status="completed",
        stage="completed",
        completed_stages=13,
        result=result,
    )
    save_json(job_path / "result.json", result)


async def prepare_model(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["VIDEO_PROJECT_DIR"] = str(root / "model_setup")
    os.environ["VIDEO_SHARED_ROOT"] = str(root / "shared")
    os.environ["OLLAMA_MODELS"] = str(root / "shared" / "models")
    namespace = {"__name__": "model_setup"}
    await execute(STAGES["1"], namespace, "environment")
    await execute(STAGES["2"], namespace, "defaults")
    tree = ast.parse(STAGES["3"])
    prefix = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Tuple)
            and any(
                isinstance(e, ast.Name) and e.id == "TARGET_SECONDS" for e in t.elts
            )
            for t in node.targets
        ):
            break
        prefix.append(node)
    await execute(ast.Module(body=prefix, type_ignores=[]), namespace, "model_helpers")
    namespace["load_local_model"]()
    namespace["unload_local_model"]()
    print("Gemma2:2b is downloaded and ready. Inference remains local.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Internal offline render fixture; not exposed in HTTP requests.",
    )
    args = parser.parse_args()
    try:
        if args.prepare:
            asyncio.run(prepare_model(args.prepare))
        elif args.job:
            asyncio.run(run_job(args.job, self_test=args.self_test))
        else:
            parser.error("Supply --job or --prepare.")
    except Exception as exc:
        traceback.print_exc()
        if args.job:
            JobStore(args.job.resolve().parent.parent).update(
                args.job.name,
                status="failed",
                stage="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        sys.exit(1)
