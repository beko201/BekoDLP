"""Media time parsing, trim validation and remaining-time formatting."""
import math
import re


def parse_timecode(text):
    """Parse seconds, mm:ss or hh:mm:ss, returning None for blank input.

    Raise ValueError for invalid, negative or non-finite values. Only the seconds
    field may contain a decimal fraction.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError("Time must be entered as text.")
    text = text.strip()
    if not text:
        return None
    if not re.fullmatch(r"[0-9]+(?::[0-9]+){0,2}(?:\.[0-9]+)?", text):
        raise ValueError(f"Invalid time: {text!r}. Use seconds, mm:ss or hh:mm:ss (e.g. 90, 1:30, 1:02:30.5).")
    parts = [float(part) for part in text.split(":")]
    if any(not math.isfinite(part) for part in parts):
        raise ValueError("Time must be a finite number.")
    if any(part >= 60 for part in parts[1:]):
        raise ValueError("Minute and second fields after a colon must be less than 60.")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    if not math.isfinite(seconds):
        raise ValueError("The time value is too large.")
    return seconds


def validate_trim_range(start_text, end_text):
    values = []
    for label, text in (("Start", start_text), ("End", end_text)):
        try:
            values.append(parse_timecode(text))
        except ValueError as error:
            raise ValueError(f"{label} time: {error}") from error
    start, end = values
    if end is not None and end <= (start or 0):
        raise ValueError("The end time must be greater than the start time. A blank start is treated as 0.")
    return start, end


def format_eta(seconds: float) -> str:
    """Format ETA in minutes and seconds, or '--' when unavailable."""
    if seconds is None or seconds <= 0 or seconds == float('inf'):
        return "--"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"
