"""spoken_dates.py — turning a spoken deadline into a calendar day, server-side.

THE PROBLEM. prompts.py deliberately forbids the model from resolving a spoken
deadline to a calendar date: it must emit `due_date` EXACTLY as spoken ("Friday",
"next Tuesday", "15th March"). That is the right call — a model resolving "next
Tuesday" without knowing the meeting's date would be inventing information, and
the transcript is the record.

But it leaves every date-dependent feature blind. `_is_overdue` parses ISO and
returns False for anything else, so a task due "Friday" is NEVER overdue, never
appears in `due_before` filters, and would never fire a notification. The value
is real data that no consumer can compare.

So resolution happens HERE, where we know the meeting's date and can anchor a
relative phrase to it. This is a PORT of app/lib/spoken-dates.ts — the client
already solved this problem and its behaviour is test-covered. The two must stay
in agreement, so the parsing order, the regexes and the deliberate omissions
below mirror that file on purpose. Change one, change both.

THE RULE THIS FILE FOLLOWS. Resolve only what is unambiguous; return "" for
everything else. A wrong date is worse than no date, because a due date is
trusted at a glance and drives notifications — mailing someone that work is due
Thursday when nobody said Thursday is a bug that erodes trust in the whole
product. So:

  * "next Tuesday", "15th March", "tomorrow", "end of month" -> resolved.
  * "end of Q3", "before the holidays", "soon"               -> "" (unplaceable).

The original spoken text is NEVER destroyed: callers store the resolved day
alongside it (`due_date` / `due_date_normalized`), so the record keeps what the
speaker actually said and the machinery gets something it can compare.
"""

import re
from datetime import date, datetime, timedelta, timezone

# How the phrase was read, mirroring SpokenResolution.confidence in the TS.
#   exact    — an explicit calendar date was spoken ("15th March")
#   relative — anchored to the meeting date ("next Tuesday", "tomorrow")
#   none     — a real deadline, but not a day ("end of Q3", "soon")
EXACT = "exact"
RELATIVE = "relative"
NONE = "none"

UNPLACEABLE = ("", NONE)

# How far into the past a spoken month+day with NO year may land before it is
# read as next year's date instead. See _month_day_key: "the 15th" said on the
# 17th is a date just missed; "5th January" said in December is next January.
ROLLOVER_SLACK_DAYS = 90

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Monday-first, matching the client's startOfWeek() and calendar grid.
WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_HEDGED = re.compile(
    r"\b(sometime|around|maybe|possibly|hopefully|or so|ish)\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)(?:\s+(\d{4}))?\b")
_MONTH_DAY = re.compile(
    r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?\b")
_IN_N = re.compile(r"\bin\s+(\d{1,3})\s+(day|days|week|weeks)\b")
_END_WEEK = re.compile(r"\bend of (?:the )?week\b")
_END_MONTH = re.compile(r"\bend of (?:the )?month\b")
_WEEKDAY = re.compile(
    r"\b(next|this|coming)?\s*(monday|mon|tuesday|tues|tue|wednesday|wed|"
    r"thursday|thurs|thu|friday|fri|saturday|sat|sunday|sun)\b")


def _day_key(y, m, d):
    """YYYY-MM-DD, or "" when that is not a real calendar day.

    Guards "31st February", which a speaker can absolutely say and which must
    not silently roll over into March.
    """
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def _month_day_key(anchor, month, day, spoken_year):
    """YYYY-MM-DD for a spoken month+day, rolling into next year when needed.

    When the speaker states the year ("5th January 2027") that year is used
    verbatim — they said it, so there is nothing to infer.

    With NO year spoken, the year is the anchor's, EXCEPT when that lands the
    date well before the meeting. A deadline agreed in a meeting is essentially
    always in that meeting's future, so a December meeting saying "5th January"
    means the January that is three weeks away, not the one eleven months gone.
    Defaulting to the anchor's year there produced a date in the past, stamped
    EXACT (the highest confidence), which made the task overdue the moment it
    was created and could fire an overdue notification for work not yet begun.

    The rollover is deliberately NOT applied to a date merely a few days past:
    a meeting that ends by agreeing a deadline of "the 15th" when it is the
    17th is far more likely to be discussing something just missed than
    something 11.5 months out. ROLLOVER_SLACK_DAYS marks that boundary — near
    past means this year, deep past means next.
    """
    if spoken_year:
        return _day_key(int(spoken_year), month, day)
    key = _day_key(anchor.year, month, day)
    if not key:
        # Feb 29 in a non-leap anchor year is still a real date next year.
        return _day_key(anchor.year + 1, month, day)
    if (anchor - date.fromisoformat(key)).days > ROLLOVER_SLACK_DAYS:
        return _day_key(anchor.year + 1, month, day) or key
    return key


def _start_of_week(anchor):
    """Monday of the week containing `anchor`."""
    return anchor - timedelta(days=anchor.weekday())


def resolve_spoken_date(when, anchor):
    """(day_key, confidence) for a spoken deadline, anchored to a meeting date.

    `anchor` is a datetime.date — the meeting's own day. Using "now" instead
    would silently re-point an old meeting's deadline every time it is read.
    """
    raw = str(when or "").strip().lower()
    if not raw or anchor is None:
        return UNPLACEABLE

    # Anything hedged is not a date, however date-like the rest of it looks.
    # "sometime around the 15th" is a hope, not a deadline.
    if _HEDGED.search(raw):
        return UNPLACEABLE

    # --- explicit calendar dates -------------------------------------------
    iso = _ISO.search(raw)
    if iso:
        key = _day_key(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        return (key, EXACT) if key else UNPLACEABLE

    dm = _DAY_MONTH.search(raw)
    if dm and dm.group(2) in MONTHS:
        key = _month_day_key(anchor, MONTHS[dm.group(2)], int(dm.group(1)),
                             dm.group(3))
        if key:
            return (key, EXACT)

    md = _MONTH_DAY.search(raw)
    if md and md.group(1) in MONTHS:
        key = _month_day_key(anchor, MONTHS[md.group(1)], int(md.group(2)),
                             md.group(3))
        if key:
            return (key, EXACT)

    # Numeric d/m — DELIBERATELY NOT SUPPORTED. "3/4" is March 4th to an
    # American and April 3rd to everyone else, and there is no way to tell
    # which the speaker meant. A 50% chance of being a month wrong is exactly
    # the kind of confident error this module exists to avoid.

    # --- relative, anchored to the meeting ---------------------------------
    if re.search(r"\btoday\b", raw):
        return (anchor.isoformat(), RELATIVE)
    # Checked BEFORE plain "tomorrow" — the longer phrase CONTAINS the shorter
    # one, so testing "tomorrow" first swallows it and silently loses a day.
    if re.search(r"\bday after tomorrow\b", raw):
        return ((anchor + timedelta(days=2)).isoformat(), RELATIVE)
    if re.search(r"\btomorrow\b", raw):
        return ((anchor + timedelta(days=1)).isoformat(), RELATIVE)

    in_n = _IN_N.search(raw)
    if in_n:
        n = int(in_n.group(1))
        days = n * 7 if in_n.group(2).startswith("week") else n
        return ((anchor + timedelta(days=days)).isoformat(), RELATIVE)

    if _END_WEEK.search(raw):
        return ((_start_of_week(anchor) + timedelta(days=4)).isoformat(),
                RELATIVE)
    if _END_MONTH.search(raw):
        # Day 1 of next month, minus a day — handles February and leap years
        # without a table.
        first_next = date(anchor.year + (anchor.month == 12),
                          anchor.month % 12 + 1, 1)
        return ((first_next - timedelta(days=1)).isoformat(), RELATIVE)

    wd = _WEEKDAY.search(raw)
    if wd and wd.group(2) in WEEKDAYS:
        target = WEEKDAYS[wd.group(2)]
        anchor_idx = anchor.weekday()
        delta = target - anchor_idx
        # A bare or "this" weekday that has already passed this week means the
        # NEXT one — "Friday" said on a Saturday is not yesterday.
        if delta <= 0:
            delta += 7
        # "next X" means the following week when X still lies ahead this week;
        # when it has already passed, "next X" is that same upcoming one.
        if wd.group(1) == "next" and target > anchor_idx:
            delta += 7
        return ((anchor + timedelta(days=delta)).isoformat(), RELATIVE)

    # --- deliberately unplaceable ------------------------------------------
    #
    # Quarters, seasons, holidays and vague horizons ("soon", "shortly", "in a
    # few weeks", "before the holidays", "end of Q3"). Each is a real deadline
    # the user should see — and none of them is a day.
    return UNPLACEABLE


def anchor_date(recorded_at, created_at=""):
    """Meeting day as a date, from whatever the recording row actually holds.

    `recorded_at` is written as a Unix epoch STRING by the upload path, but
    older/other rows carry an ISO timestamp, and both shapes are live in the
    table today (the assistant's meeting lookup already compares `when[:10]`
    against an ISO date). Accept both rather than assuming one and silently
    resolving every date against the wrong anchor. Returns None when nothing
    usable is present — callers then leave the date unresolved rather than
    falling back to "now", which would re-point old deadlines on every read.
    """
    for value in (recorded_at, created_at):
        text = str(value or "").strip()
        if not text:
            continue
        # Epoch seconds (10 digits today, 11 from 2286) — the upload path's
        # own format. Bare digits can only be epoch here; an ISO timestamp
        # always carries separators.
        if text.isdigit():
            try:
                return datetime.fromtimestamp(
                    int(text), tz=timezone.utc).date()
            except (ValueError, OSError, OverflowError):
                continue
        try:
            return datetime.fromisoformat(
                text.replace("Z", "+00:00")).date()
        except ValueError:
            # A bare YYYY-MM-DD prefix on something otherwise unparseable.
            match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
            if match:
                key = _day_key(*(int(g) for g in match.groups()))
                if key:
                    return date.fromisoformat(key)
    return None


def normalize_spoken_date(spoken, anchor):
    """The ISO day for a spoken deadline, or "" when it is not placeable.

    The thin wrapper callers want: they store this next to the original spoken
    text and compare against it, never parsing the spoken value themselves.
    """
    if anchor is None:
        return ""
    key, _confidence = resolve_spoken_date(spoken, anchor)
    return key
