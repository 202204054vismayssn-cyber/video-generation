"""Duration parser shared with the original notebook."""

import math
import re

DEFAULT_SECONDS = 60
DURATION_TOLERANCE_SECONDS = 3
MIN_MAIN_SECONDS = 9


def duration_frame_bounds(seconds, fps):
    """Keep whole-frame durations within the requested tolerance, inclusive."""
    minimum = math.ceil((seconds - DURATION_TOLERANCE_SECONDS) * fps - 1e-8)
    maximum = math.floor((seconds + DURATION_TOLERANCE_SECONDS) * fps + 1e-8)
    return minimum, maximum


def main_scene_count(main_frames, fps):
    if main_frames < MIN_MAIN_SECONDS * fps:
        raise ValueError("Leave at least nine seconds for main content.")
    return max(3, min(40, math.ceil(main_frames / (12 * fps))))


def duration_from_prompt(prompt):
    text = prompt.casefold().replace("–", "-").replace("—", "-").replace("‑", "-")
    ranges = []
    pattern = "(?<![\\d.])(\\d+(?:\\.\\d+)?)\\s*-\\s*(\\d+(?:\\.\\d+)?)\\s*(seconds?|secs?|s|minutes?|mins?|m)\\b"

    def mask_range(match):
        factor = 60 if match[3].startswith("m") else 1
        start, end = (float(match[1]) * factor, float(match[2]) * factor)
        if end <= start:
            raise ValueError("Invalid scene time range in prompt.")
        ranges.append((start, end))
        return " " * len(match[0])

    singles = re.sub(pattern, mask_range, text)
    unit = "(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)"
    token = f"(?<![\\w.])(\\d+(?:\\.\\d+)?)\\s*-?\\s*({unit})\\b"
    matches = list(re.finditer(token, singles))
    groups = []
    for match in matches:
        factor = (
            3600 if match[2].startswith("h") else 60 if match[2].startswith("m") else 1
        )
        value = float(match[1]) * factor
        if (
            groups
            and singles[groups[-1]["end"] : match.start()].strip() in ("", "and")
            and (factor < groups[-1]["factor"])
        ):
            groups[-1].update(
                value=groups[-1]["value"] + value, end=match.end(), factor=factor
            )
        else:
            groups.append(
                dict(value=value, start=match.start(), end=match.end(), factor=factor)
            )
    explicit, candidates = [], []
    for group in groups:
        before = singles[max(0, group["start"] - 70) : group["start"]]
        after = singles[group["end"] : group["end"] + 45]
        if (
            re.search("(?:last|final)\\s*$", before)
            or re.search(
                "(?:closing(?:\\s+(?:slide|card))?|end\\s+card)\\s*(?::|=|for|duration)?\\s*$",
                before,
            )
            or re.search("^\\s*(?:for\\s+)?(?:the\\s+)?(?:closing|end\\s+card)", after)
        ):
            continue
        candidates.append(group["value"])
        if re.search(
            "(?:duration|runtime|total|length|create|make|generate)\\b", before
        ) or re.search("^\\s*(?:long\\s+)?(?:video|film|montage|explainer)\\b", after):
            explicit.append(group["value"])
    values = explicit or candidates
    unique = set(values)
    if len(unique) > 1:
        raise ValueError(
            "Conflicting duration values in prompt. Set duration_seconds to the intended total seconds."
        )
    if unique:
        return (unique.pop(), "explicit prompt duration")
    if ranges:
        return (max((end for start, end in ranges)), "last scene timestamp")
    return (float(DEFAULT_SECONDS), "default (no numeric duration found)")


def resolve_duration(prompt, override=None):
    value, origin = (
        (float(override), "duration_seconds")
        if override is not None
        else duration_from_prompt(prompt)
    )
    if not 15 <= value <= 600:
        raise ValueError("Duration must be between 15 and 600 seconds.")
    return (value, origin)
