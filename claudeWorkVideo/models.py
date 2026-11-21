"""The public configuration contract. Defaults and descriptions also power /options."""

from collections import Counter
import math

from duration import main_scene_count, resolve_duration
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Fit = Literal["cover", "contain"]
Motion = Literal["none", "zoom_in", "zoom_out", "pan_left", "pan_right"]
Tone = Literal["auto", "professional", "calm", "enthusiastic", "urgent"]
FileField = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Output(Settings):
    fps: Literal[20, 24, 25, 30, 60] = Field(
        30, description="Frames per second. Higher values increase rendering work."
    )
    width: int = Field(
        1920,
        ge=480,
        le=1920,
        description="Even output width. Version 1 supports a 16:9 landscape canvas.",
    )
    height: int = Field(
        1080,
        ge=270,
        le=1080,
        description="Even output height; must match the 16:9 width.",
    )
    crf: int = Field(
        20,
        ge=16,
        le=28,
        description="H.264 quality: lower values give higher quality and larger files.",
    )
    preset: Literal["ultrafast", "veryfast", "fast", "medium"] = Field(
        "veryfast", description="Encoder effort. Slower presets take longer to render."
    )

    @model_validator(mode="after")
    def dimensions(self):
        if self.width % 2 or self.height % 2 or self.width * 9 != self.height * 16:
            raise ValueError(
                "Use even 16:9 dimensions, such as 960x540, 1280x720 or 1920x1080."
            )
        return self


class Voice(Settings):
    enabled: bool = Field(
        True,
        description="Generate narration. Set false for a silent or music-only video.",
    )
    name: str = Field(
        "en-US-BrianNeural",
        min_length=5,
        max_length=100,
        pattern=r"^[A-Za-z0-9:_-]+Neural$",
        description="An Edge TTS ShortName; GET /voices lists currently available voices. Narration is English.",
    )
    tone: Tone = Field(
        "auto",
        description="Auto uses the story's segment delivery; other values select a fixed tone preset.",
    )
    rate_percent: int = Field(
        0,
        ge=-50,
        le=50,
        description="Base speaking-rate adjustment. Positive is faster; tone also contributes.",
    )
    pitch_hz: int = Field(
        0,
        ge=-50,
        le=50,
        description="Base pitch adjustment in Hz. Positive raises pitch.",
    )
    volume_percent: int = Field(
        0,
        ge=-80,
        le=50,
        description="Base volume adjustment relative to the voice's normal level.",
    )
    prosody_strength: float = Field(
        1,
        ge=0,
        le=2,
        description="Strength of tone presets. Zero disables their rate/pitch/volume offsets.",
    )
    scene_tones: dict[int, Tone] = Field(
        default_factory=dict,
        description='Optional one-based scene tone overrides, for example {"2":"calm"}.',
    )


class Media(Settings):
    file_field: FileField = Field(
        description="Name of the actual File field in the same Postman form-data request."
    )
    fit: Fit = Field(
        "cover",
        description="Cover fills the frame and may crop. Contain preserves the whole asset with margins.",
    )
    motion: Motion = Field(
        "none",
        description="Still-image motion only. Contain keeps the complete image stationary.",
    )
    loop: bool = Field(
        True,
        description="Repeat video/GIF playback to fill its assigned scene; false holds its last frame.",
    )
    start_seconds: float = Field(
        0,
        ge=0,
        le=3600,
        description="Video-only trim start, relative to the source clip, not the finished video.",
    )
    end_seconds: float | None = Field(
        None,
        gt=0,
        le=3600,
        description="Video-only trim end. Omit to use the source clip's end.",
    )

    @model_validator(mode="after")
    def compatible(self):
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds.")
        if self.fit == "contain" and self.motion != "none":
            raise ValueError(
                "Photo motion requires fit=cover. Use motion=none with contain."
            )
        return self


class SceneMedia(Media):
    scene: int = Field(
        ge=1,
        le=40,
        description="One-based scene number. Must exist in the generated story.",
    )


class Closing(Settings):
    narration: str | None = Field(
        None,
        max_length=200,
        description="Optional spoken closing message. The bundled card defaults to 'Connect to Savvy Asia.' when voice is enabled. Use an empty string for silence.",
    )
    enabled: bool = Field(
        True,
        description="Append the closing slide. Set false to disable it.",
    )
    file_field: FileField | None = Field(
        None,
        description="Optional upload field for static JPG, PNG or WebP artwork. Omit to use the bundled Savvy Asia card.",
    )
    duration_seconds: float = Field(
        3,
        ge=1,
        le=30,
        description="Closing time included in the target. The card can grow to fit speech within the total duration window.",
    )
    display: Literal["original"] = Field(
        "original",
        description="Show the complete artwork without headers, footers, motion or message overlays.",
    )
    background: str = Field(
        "#FFFFFF",
        pattern=r"^#[0-9a-fA-F]{6}$",
        description="Plain margin colour when the artwork and video have different proportions.",
    )


class Music(Settings):
    enabled: bool = Field(
        False,
        description="Enable background music. Without a file, use the existing generated instrumental bed.",
    )
    file_field: FileField | None = Field(
        None,
        description="Optional uploaded instrumental audio file. Source-video sound is always discarded.",
    )


class Brand(Settings):
    company_name: str = Field(
        "Savvy Asia",
        min_length=1,
        max_length=36,
        description="Company label on main-content scenes; never added over closing artwork.",
    )
    tagline: str = Field(
        "",
        max_length=60,
        description="Optional custom tagline. Omit (or leave blank) to automatically derive the tagline from the AI story title.",
    )
    logo_file_field: FileField | None = Field(
        None,
        description="Optional File field containing a company logo image (PNG/JPEG/WebP).",
    )


NonemptyText = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]


class SlideTextInstructions(Settings):
    writer: Literal["Gemma"] = "Gemma"
    tone: Literal["professional", "strictly professional"] = "strictly professional"
    detail: str = ""
    top_right_header: str = ""
    logo_clearance: str = ""


class StoryboardScene(Settings):
    scene: int = Field(ge=1, le=40)
    target_seconds: float = Field(gt=0, le=600, allow_inf_nan=False)
    purpose: NonemptyText
    key_points: list[NonemptyText] = Field(min_length=1, max_length=20)
    narration: NonemptyText
    slide_text_guidance: str = Field("", max_length=5000)
    top_right_header_guidance: str = Field("", max_length=5000)


class StoryboardClosing(Settings):
    duration_seconds: float = Field(ge=0, le=30, allow_inf_nan=False)
    narration: str = Field(max_length=200)


class Storyboard(Settings):
    title: Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
    duration_seconds: float = Field(ge=15, le=600, allow_inf_nan=False)
    audience: NonemptyText
    tone: Literal["professional"] = "professional"
    main_content_seconds: float = Field(ge=9, le=600, allow_inf_nan=False)
    scene_count: int = Field(ge=1, le=40)
    slide_text_instructions: SlideTextInstructions = Field(default_factory=SlideTextInstructions)
    scenes: list[StoryboardScene] = Field(min_length=1, max_length=40)
    closing: StoryboardClosing

    @model_validator(mode="after")
    def consistent_timeline(self):
        if self.scene_count != len(self.scenes):
            raise ValueError("scene_count must match the number of scenes.")
        if [scene.scene for scene in self.scenes] != list(range(1, self.scene_count + 1)):
            raise ValueError("Scene numbers must run from 1 to scene_count in order.")
        if not math.isclose(sum(scene.target_seconds for scene in self.scenes), self.main_content_seconds, abs_tol=0.01):
            raise ValueError("Scene target_seconds must add up to main_content_seconds.")
        if not math.isclose(self.main_content_seconds + self.closing.duration_seconds, self.duration_seconds, abs_tol=0.01):
            raise ValueError("Main content and closing time must add up to duration_seconds.")
        if self.closing.duration_seconds == 0 and self.closing.narration.strip():
            raise ValueError("A disabled closing slide must have an empty narration.")
        return self


class VideoConfig(Settings):
    storyboard: Storyboard | None = None
    prompt: str = Field(
        "",
        max_length=20000,
        description="Content brief, audience, facts and intended result. The model does not browse or fact-check.",
    )
    reference_notes: str = Field(
        "",
        max_length=20000,
        description="Optional verified facts or source notes for the local model.",
    )
    duration_seconds: float | None = Field(
        None,
        ge=15,
        le=600,
        description="Explicit target including closing time. Overrides prompt duration; otherwise parse the prompt, then default to 60s.",
    )
    duration_policy: Literal["condense_then_extend"] = Field(
        "condense_then_extend",
        description="Fit measured narration and the closing slide within three seconds of the target. Shorten long narration as needed.",
    )
    output: Output = Field(
        default_factory=Output,
        description="Output dimensions, FPS and encoding settings.",
    )
    voice: Voice = Field(
        default_factory=Voice,
        description="Online Edge narration and delivery settings; no paid model API.",
    )
    background: Media | list[Media] | None = Field(
        None,
        description="Optional shared background, or up to four sequential assets. Explicit scene media takes priority.",
    )
    scene_media: list[SceneMedia] = Field(
        default_factory=list,
        max_length=80,
        description="Per-scene media overrides; up to four sequential assets per scene.",
    )
    closing_slide: Closing | None = Field(
        None, description="Bundled Savvy Asia closing slide when omitted."
    )
    music: Music = Field(
        default_factory=Music, description="Optional instrumental background music."
    )
    brand: Brand = Field(default_factory=Brand, description="Main-scene branding.")
    narration_gap_seconds: float = Field(
        0.18,
        ge=0,
        le=1,
        description="Inter-scene voiceover gap, plus at most a frame of rounding.",
    )
    transition_seconds: float = Field(
        0.20,
        ge=0.10,
        le=0.50,
        description="Visual crossfade length, included in the planned timeline.",
    )
    text_backdrop_strength: float = Field(
        0.86,
        ge=0,
        le=1,
        description="Strength of the light reading gradient behind black text. Zero removes the gradient.",
    )
    background_wash: float = Field(
        0.12,
        ge=0,
        le=1,
        description="Overall light wash over background footage. Zero leaves the source colours unchanged.",
    )

    @model_validator(mode="after")
    def assignments(self):
        if not self.prompt.strip() and self.storyboard is None:
            raise ValueError("Supply a prompt or a storyboard.")
        if self.storyboard:
            if self.duration_seconds is not None and not math.isclose(self.duration_seconds, self.storyboard.duration_seconds, abs_tol=0.001):
                raise ValueError("duration_seconds conflicts with storyboard.duration_seconds.")
            closing = self.effective_closing()
            ending = closing.duration_seconds if closing.enabled else 0
            if not math.isclose(ending, self.storyboard.closing.duration_seconds, abs_tol=0.001):
                raise ValueError("closing_slide duration conflicts with storyboard.closing.duration_seconds.")
            if closing.narration != self.storyboard.closing.narration:
                raise ValueError("closing_slide narration conflicts with storyboard.closing.narration.")
            if self.voice.tone not in ("auto", "professional") or any(tone not in ("auto", "professional") for tone in self.voice.scene_tones.values()):
                raise ValueError("Storyboard videos require a professional voice tone.")
            if closing.narration and closing.narration.strip() and not self.voice.enabled:
                raise ValueError("Enable voice.enabled for the storyboard closing narration.")
        self.planned_scene_count()
        backgrounds = (
            self.background
            if isinstance(self.background, list)
            else ([self.background] if self.background else [])
        )
        if self.background is not None and not 1 <= len(backgrounds) <= 4:
            raise ValueError(
                "Provide one to four background assets, or omit background."
            )
        if any(
            count > 4
            for count in Counter(item.scene for item in self.scene_media).values()
        ):
            raise ValueError("Use at most four media assets per scene.")
        if any(not 1 <= number <= 40 for number in self.voice.scene_tones):
            raise ValueError(
                "scene_tones keys must be one-based scene numbers from 1 to 40."
            )
        if (
            self.closing_slide
            and self.closing_slide.enabled
            and (self.closing_slide.narration or "").strip()
            and not self.voice.enabled
        ):
            raise ValueError(
                "Enable voice.enabled when supplying closing_slide.narration."
            )
        if (
            self.closing_slide
            and self.closing_slide.enabled
            and self.duration_seconds is not None
        ):
            if self.duration_seconds - self.closing_slide.duration_seconds < 9:
                raise ValueError("Leave at least nine seconds for main content.")
        return self

    def media_specs(self):
        background = (
            self.background
            if isinstance(self.background, list)
            else ([self.background] if self.background else [])
        )
        return [*background, *self.scene_media]

    def resolved_duration(self):
        if self.storyboard:
            return self.storyboard.duration_seconds
        return resolve_duration(self.prompt, self.duration_seconds)[0]

    def planned_scene_count(self):
        closing = self.effective_closing()
        fps = self.output.fps
        main_frames = round(self.resolved_duration() * fps) - (
            round(closing.duration_seconds * fps) if closing.enabled else 0
        )
        if not self.storyboard:
            return main_scene_count(main_frames, fps)
        if any(scene.target_seconds < 1 + self.transition_seconds + 0.08 for scene in self.storyboard.scenes):
            raise ValueError("Each scene needs time for at least one second of speech and its transition.")
        return len(self.storyboard.scenes)

    def effective_closing(self):
        if self.storyboard is None:
            return self.closing_slide if self.closing_slide is not None else Closing()
        closing = self.storyboard.closing
        values = {
            "enabled": closing.duration_seconds > 0,
            "duration_seconds": closing.duration_seconds or 3,
            "narration": closing.narration,
        }
        if self.closing_slide:
            values.update(self.closing_slide.model_dump(exclude_unset=True))
        return Closing(**values)

    def referenced_files(self):
        fields = {item.file_field for item in self.media_specs()}
        closing = self.effective_closing()
        if closing.enabled and closing.file_field:
            fields.add(closing.file_field)
        if self.music.enabled and self.music.file_field:
            fields.add(self.music.file_field)
        if self.brand.logo_file_field:
            fields.add(self.brand.logo_file_field)
        return fields
