"""share_schema.py — the Meeting Share data model, tokens and public payload.

A share is a CAPABILITY: possession of the token is the entire authorization.
There is no account behind it, no JWT, no session. That single fact drives
every decision in this module, so it is worth stating what follows from it:

  * THE TOKEN IS A SECRET, and it is the ONLY secret. It must therefore carry
    enough entropy that guessing is not a strategy (SHARE_TOKEN_BYTES = 32,
    256 bits, from secrets.token_urlsafe) and must never be stored in a form
    that a database leak would turn into working links. We store
    sha256(token) and nothing else — see hash_token(). The raw token is
    returned to the owner exactly once, in the create response, and is never
    written to DynamoDB, never logged, and never echoed by the public route.

  * A SHARE IS NOT A RECORDING. The public payload is BUILT, field by field,
    from what the share config enables — it is never the recording row with
    private attributes stripped out. That direction matters: a deny-list has
    to be updated every time the recording row grows a new attribute, and the
    day someone forgets is the day owner_user_id or the raw S3 key ships to
    the public internet. public_payload() below can only emit what it
    explicitly assembles.

  * TOGGLES ARE ENFORCED SERVER-SIDE, not by hiding UI. transcript_enabled
    False means the transcript never enters the payload at all, so there is
    nothing for a curious viewer to find in the HTML source, and audio_enabled
    False means no presigned URL is ever generated (a presign is a bearer
    credential too — minting one "just in case" would hand out audio the owner
    did not share).

WHY A SEPARATE MODULE. lambda_function.py owns routing, auth and DynamoDB;
this module owns the share VALUE model — coercion, tokens, section filtering
and HTML. Keeping them apart is what lets cloud/tests/test_meeting_share.py
exercise the security rules (expiry, revocation, toggle enforcement, private
field exclusion) as pure functions, with no AWS stubs in the way.

MoM REUSE. The public page renders the SAME mom_schema sections the MoM
editor, the DOCX export and the PDF render from — see public_sections(). A
second meeting-note renderer would drift from the owner's edited MoM within
one release, and the shared page would then show something the owner never
approved. Filtering happens on section `role`, not on title, so an owner who
renames "Attendees" to "Participants" does not accidentally unshare it.
"""

import hashlib
import html
import re
import secrets
from datetime import datetime, timezone

import mom_schema

# 32 bytes -> 43 URL-safe characters, 256 bits of entropy. Sized so that an
# attacker with the whole internet's bandwidth is not meaningfully closer to a
# hit than an attacker with none; the recording key is NEVER a substitute for
# this (see the module docstring).
SHARE_TOKEN_BYTES = 32

# Presigned audio lifetime for a PUBLIC viewer.
#
# THE SUBTLETY THAT MAKES A FIXED VALUE WRONG. S3 checks a presigned URL's
# expiry at the time of the HTTP REQUEST, not continuously: a download already
# in flight survives the expiry, but any NEW request after it fails. An HTML5
# <audio> element does not make one long request — it makes a new ranged
# request every time the viewer seeks, and often when they pause and resume.
# So a flat 15-minute URL does not mean "15 minutes of listening"; it means
# the first seek after minute 15 fails, and a viewer who opens the page and
# presses play 20 minutes later gets nothing at all. An hour-long meeting is
# exactly where that bites.
#
# Hence: scaled to the recording, not to the clock. x3 leaves room for pausing
# and scrubbing around rather than assuming a single linear listen.
SHARE_AUDIO_MIN_EXPIRY = 3600        # 1h floor — covers open-now-play-later
SHARE_AUDIO_MAX_EXPIRY = 43200       # 12h ceiling — S3 console's own maximum
SHARE_AUDIO_DURATION_FACTOR = 3

# The gateway (/share/{token}/audio) re-validates and re-signs on EVERY
# request, so the URL it hands out only has to outlive one ranged read. This
# is the value used there, and it is why revoking a share stops playback that
# is already under way.
SHARE_AUDIO_URL_EXPIRY = 900


def audio_url_expiry(duration_seconds=None):
    """How long a public audio presign should live, given the recording.

    Used for the DIRECT presign (the fallback path). The gateway uses the flat
    SHARE_AUDIO_URL_EXPIRY instead, because it re-signs per request.
    """
    try:
        seconds = int(duration_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    scaled = seconds * SHARE_AUDIO_DURATION_FACTOR
    return max(SHARE_AUDIO_MIN_EXPIRY, min(scaled, SHARE_AUDIO_MAX_EXPIRY))

# V1 has exactly one access model. Stored as a field rather than assumed, so
# adding "invite_only" later is a value change and not a migration — the same
# reason every toggle below is its own boolean.
ACCESS_PUBLIC = "public"
ACCESS_TYPES = (ACCESS_PUBLIC,)

# The share toggles, in display order. ONE list drives the DynamoDB attribute
# names, the create/patch API surface, the section filter and the app's
# checklist, so a future "photos" toggle cannot be added to three of the four
# and silently ignored by the fourth.
#
# `roles` maps a toggle to the mom_schema section roles it governs. A section
# whose role is in NO toggle's set is never published — that is the deny-by-
# default rule, and it is what makes adding a new MoM section safe: it stays
# private until someone deliberately maps it here.
TOGGLES = (
    ("summary_enabled", True,
     (mom_schema.ROLE_SUMMARY, mom_schema.ROLE_OVERVIEW)),
    ("highlights_enabled", True,
     (mom_schema.ROLE_HIGHLIGHTS, mom_schema.ROLE_POINTS)),
    ("decisions_enabled", True, (mom_schema.ROLE_DECISIONS,)),
    ("tasks_enabled", True,
     (mom_schema.ROLE_ACTIONS, mom_schema.ROLE_FOLLOWUPS)),
    ("participants_enabled", True, (mom_schema.ROLE_ATTENDEES,)),
    ("transcript_enabled", False, ()),
    ("audio_enabled", False, ()),
)

# The API's short names ({"summary": true}) vs the stored attribute names
# (summary_enabled). The wire stays readable; the stored row stays explicit.
TOGGLE_API_NAMES = {name[: -len("_enabled")]: name for name, _, _ in TOGGLES}

# Meeting Details is structural, not content: it carries the title, date and
# duration that the page header shows. It is filtered out of the body rather
# than published as a section, so the header never duplicates it.
STRUCTURAL_ROLES = (mom_schema.ROLE_DETAILS,)

MAX_TRANSCRIPT_CHARS = 200_000
# Ceilings on what one shared page may carry. A four-hour meeting can hold
# thousands of segments, and the whole page is built in memory and returned
# through API Gateway — these keep a pathological recording from producing a
# response nobody can load. Generous enough that a real meeting never reaches
# them.
MAX_TRANSCRIPT_SEGMENTS = 4_000
MAX_TEXT_CHARS = 12_000
MAX_LIST_ITEMS = 100

# theme.tsx's ColorScale.speakers has six entries and assigns them in
# first-appearance order (lib/transcript-view.tsx buildSpeakerColors). The
# palette itself lives in the CSS below; this is only how many there are, so
# the index arithmetic here and the CSS classes there cannot drift.
SPEAKER_COLOUR_COUNT = 6


def normalize_speaker_label(label):
    """Mirrors lib/sources.ts normalizeSpeakerLabel().

    Duplicated rather than imported from mom_schema for the reason that module
    already gives for its own copy: importing across these modules to save six
    lines is not worth the coupling.
    """
    s = str(label or "").strip()
    m = re.match(r"^speaker[\s_-]*(.+)$", s, re.IGNORECASE)
    return (m.group(1) if m else s).strip()


def default_config():
    """The V1 default: notes shared, transcript and audio withheld."""
    return {name: default for name, default, _ in TOGGLES}


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    return default


def coerce_config(raw, base=None):
    """Any -> a full, valid toggle map.

    `base` is the currently stored config for a PATCH: an absent key keeps its
    stored value rather than snapping back to the default, so PATCHing one
    toggle cannot silently re-enable another. Accepts both the API short names
    and the stored names, because the create body speaks the former and a
    round-tripped row speaks the latter.
    """
    current = dict(default_config())
    if isinstance(base, dict):
        for name, _, _ in TOGGLES:
            if name in base:
                current[name] = _as_bool(base.get(name), current[name])
    if not isinstance(raw, dict):
        return current

    for short, stored in TOGGLE_API_NAMES.items():
        if short in raw:
            current[stored] = _as_bool(raw.get(short), current[stored])
        if stored in raw:
            current[stored] = _as_bool(raw.get(stored), current[stored])
    return current


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def new_token():
    """A fresh, high-entropy, URL-safe share token. Never persisted as-is."""
    return secrets.token_urlsafe(SHARE_TOKEN_BYTES)


def hash_token(token):
    """sha256 hex of the token — what the database actually stores.

    Plain SHA-256, NOT a password KDF, and that is deliberate. A KDF's cost
    factor defends a LOW-entropy secret against offline guessing; this token
    carries 256 bits, so there is nothing to guess and the per-request cost of
    bcrypt/PBKDF2 would buy no security while making the public page slower.
    The same reasoning the codebase applies to API keys, and the opposite of
    _hash_password's, which guards user-chosen passwords.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def token_looks_valid(token):
    """Cheap shape check before any database read.

    Rejects the obvious junk (empty, wrong alphabet, absurd length) so a flood
    of malformed URLs costs no DynamoDB traffic — the same "cheap checks
    first" ordering the ElevenLabs webhook uses.
    """
    if not isinstance(token, str):
        return False
    if not (20 <= len(token) <= 128):
        return False
    return re.fullmatch(r"[A-Za-z0-9_-]+", token) is not None


def redact_token(token):
    """A loggable stand-in. Raw share tokens must never reach CloudWatch.

    Returns the hash prefix, which is enough to correlate a support report
    with a stored row and useless for constructing a working link.
    """
    if not token:
        return "<none>"
    return f"sha256:{hash_token(token)[:12]}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def _parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def is_expired(share, now=None):
    """True when expires_at has passed. An absent/blank value never expires.

    An UNPARSEABLE expiry counts as EXPIRED. That is the safe direction: a
    corrupted timestamp must close the link, not open it forever.
    """
    raw = share.get("expires_at")
    if not raw:
        return False
    at = _parse_iso(raw)
    if at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at <= now


def is_revoked(share):
    return bool(share.get("revoked_at"))


def is_active(share, now=None):
    return not is_revoked(share) and not is_expired(share, now=now)


def public_share_url(base_url, token):
    """The link handed to the owner. Built once, here, so the app never
    concatenates its own and cannot drift from the route the Lambda serves."""
    return f"{(base_url or '').rstrip('/')}/share/{token}"


def owner_view(share, base_url=None, token=None):
    """One share row as the OWNER's API sees it.

    token_hash is never included: it is the stored form of the secret, and the
    owner's list screen has no use for it. `url` appears only on create, where
    the raw token still exists in memory — the list route cannot reconstruct a
    link, by design, because that would mean the token was recoverable from
    the database.
    """
    view = {
        "share_id": share.get("share_id", ""),
        "recording_key": share.get("recording_key", ""),
        "access_type": share.get("access_type", ACCESS_PUBLIC),
        "expires_at": share.get("expires_at") or None,
        "revoked_at": share.get("revoked_at") or None,
        "created_at": share.get("created_at", ""),
        "updated_at": share.get("updated_at", ""),
        "view_count": int(share.get("view_count") or 0),
        "last_viewed_at": share.get("last_viewed_at") or None,
        "active": is_active(share),
    }
    for name, _, _ in TOGGLES:
        view[name] = bool(share.get(name))
    if token:
        view["url"] = public_share_url(base_url, token)
    return view


# ---------------------------------------------------------------------------
# The public payload — assembled, never filtered down from the recording row.
# ---------------------------------------------------------------------------
def allowed_roles(share):
    """The mom_schema roles this share publishes."""
    roles = set()
    for name, _, section_roles in TOGGLES:
        if share.get(name):
            roles.update(section_roles)
    return roles


def _role_allowed(role, roles):
    """Overview sections carry a compound role ("overview:s-abc") so each AI
    section keeps a stable identity; match on the prefix before the colon."""
    base = (role or "").split(":", 1)[0]
    return base in roles


def public_sections(mom, share):
    """The MoM sections this share may show, in document order.

    Deny-by-default twice over: a section must be `visible` (the owner did not
    hide it in the editor) AND its role must be enabled by a toggle. A section
    with an unrecognised or empty role is dropped — a new MoM section is
    private until it is deliberately mapped in TOGGLES.
    """
    roles = allowed_roles(share)
    out = []
    for section in (mom or {}).get("sections") or []:
        if not isinstance(section, dict):
            continue
        if not section.get("visible", True):
            continue
        role = section.get("role") or ""
        if role in STRUCTURAL_ROLES:
            continue
        if not _role_allowed(role, roles):
            continue
        if mom_schema_section_is_empty(section):
            continue
        out.append(_public_section(section))
    return out


def mom_schema_section_is_empty(section):
    """True when a section would render as a heading with nothing under it.

    Mirrors the app's isSectionEmpty (lib/mom-model.ts) so the public page and
    the in-app preview agree about which sections are worth showing.
    """
    kind = section.get("kind")
    if kind == mom_schema.KIND_TEXT:
        return not (section.get("text") or "").strip()
    if kind == mom_schema.KIND_LIST:
        return not [i for i in section.get("items") or []
                    if i.get("visible", True) and (i.get("text") or "").strip()]
    if kind == mom_schema.KIND_FIELDS:
        return not [f for f in section.get("fields") or []
                    if f.get("visible", True)
                    and ((f.get("label") or "").strip()
                         or (f.get("value") or "").strip())]
    if kind == mom_schema.KIND_TABLE:
        return not [r for r in section.get("rows") or []
                    if r.get("visible", True)]
    return False


def _public_section(section):
    """One section reduced to what a READER needs.

    Drops ids, `source` provenance and the visible flags — all of them are
    editor bookkeeping, and `source` in particular would tell a viewer which
    lines the owner personally wrote versus which the AI produced. That is
    information about the owner's editing, not about the meeting.
    """
    kind = section.get("kind")
    out = {"kind": kind, "title": section.get("title") or ""}

    if kind == mom_schema.KIND_TEXT:
        out["text"] = section.get("text") or ""
    elif kind == mom_schema.KIND_LIST:
        out["items"] = [(i.get("text") or "").strip()
                        for i in section.get("items") or []
                        if i.get("visible", True) and (i.get("text") or "").strip()]
    elif kind == mom_schema.KIND_FIELDS:
        out["fields"] = [
            {"label": f.get("label") or "", "value": f.get("value") or ""}
            for f in section.get("fields") or []
            if f.get("visible", True)
            and ((f.get("label") or "").strip() or (f.get("value") or "").strip())
        ]
    elif kind == mom_schema.KIND_TABLE:
        columns = [c for c in section.get("columns") or [] if c.get("visible", True)]
        out["columns"] = [c.get("label") or "" for c in columns]
        rows = []
        for row in section.get("rows") or []:
            if not row.get("visible", True):
                continue
            cells = row.get("cells") or {}
            values = [(cells.get(c.get("id")) or "") for c in columns]
            if any((v or "").strip() for v in values):
                rows.append(values)
        out["rows"] = rows
    return out


def _overview_sections(item, share):
    """The AI's own Overview sections, filtered by the share's toggles.

    THIS IS WHAT THE APP'S OVERVIEW TAB RENDERS (lib/meeting-overview.tsx), so
    it is what the shared page shows first — a recipient should see the same
    meeting the owner does, not a differently-shaped rendering of it.

    The model picks these titles per meeting ("Panel Feedback", "Objections"),
    which is exactly why lib/meeting-overview.tsx switches on NOTHING but
    `kind`. The one thing this function adds is the share filter, and it can
    only do that by title, since an AI section carries no role. The mapping is
    deliberately conservative: a title that matches nothing recognisable is
    governed by `summary_enabled`, the toggle a user reads as "the AI's
    written notes". Turning that off therefore hides everything unrecognised
    rather than leaking it.
    """
    sections = ((item.get("overview") or {}).get("sections") or [])
    out = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        title = (sec.get("title") or "").strip()
        content = (sec.get("content") or "").strip()
        items = [str(i).strip() for i in (sec.get("items") or []) if str(i).strip()]
        if not title or not (content or items):
            continue
        if not share.get(_toggle_for_title(title)):
            continue
        out.append({
            "title": title,
            "kind": "list" if (sec.get("kind") == "list" or
                               (items and not content)) else "text",
            "content": content[:MAX_TEXT_CHARS],
            "items": items[:MAX_LIST_ITEMS],
        })
    return out


# Title keyword -> governing toggle, for AI-chosen Overview headings. Ordered:
# the first keyword found in the lowercased title wins.
_TITLE_TOGGLES = (
    (("decision", "agreed", "approval"), "decisions_enabled"),
    (("action", "task", "next step", "follow-up", "follow up", "to-do",
      "todo", "owner"), "tasks_enabled"),
    (("attendee", "participant", "speaker", "who "), "participants_enabled"),
    (("highlight", "key point", "discussion", "topic", "takeaway",
      "risk", "question", "objection", "feedback"), "highlights_enabled"),
)


def _toggle_for_title(title):
    low = (title or "").lower()
    for keywords, toggle in _TITLE_TOGGLES:
        if any(k in low for k in keywords):
            return toggle
    # Unrecognised -> the "AI's written notes" toggle. Deny-leaning: turning
    # Summary off hides anything this function could not classify.
    return "summary_enabled"


def _speaker_display(label, names):
    """Mirrors lib/sources.ts speakerName(), so the web page calls a speaker
    exactly what the app calls them."""
    key = normalize_speaker_label(label)
    given = (names or {}).get(key)
    if isinstance(given, str) and given.strip():
        return given.strip()
    return f"Speaker {key}" if key.isdigit() else (key or "Speaker")


def public_speaker_blocks(item, share):
    """Consecutive same-speaker segments folded into blocks.

    Mirrors lib/transcript-view.tsx buildSpeakerBlocks + buildSpeakerColors, so
    the shared transcript reads exactly like the app's: a timecode gutter, a
    coloured speaker dot and name, then the text. Colours are assigned in
    FIRST-APPEARANCE order, which is what keeps one speaker one colour.

    Timestamps are carried as numbers and formatted at render time. Nothing
    here is seekable — the shared page has no player wired to the transcript —
    so the timecode is presentation only.
    """
    if not share.get("transcript_enabled"):
        return []
    segs = item.get("timestamps") or []
    if not isinstance(segs, list):
        return []

    names = item.get("speaker_names") or {}
    blocks = []
    colour_of = {}
    for seg in segs[:MAX_TRANSCRIPT_SEGMENTS]:
        if not isinstance(seg, dict):
            continue
        speaker = str(seg.get("speaker") or "")
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        if speaker not in colour_of:
            colour_of[speaker] = len(colour_of) % SPEAKER_COLOUR_COUNT
        if blocks and blocks[-1]["speaker_raw"] == speaker:
            blocks[-1]["text"] = f"{blocks[-1]['text']} {text}".strip()
            continue
        blocks.append({
            "speaker_raw": speaker,
            "speaker": _speaker_display(speaker, names),
            "colour": colour_of[speaker],
            "start": _num_or_zero(seg.get("start")),
            "text": text,
        })

    # speaker_raw was only needed for grouping; it is a diarization internal
    # and there is no reason for it to reach the page.
    for b in blocks:
        b.pop("speaker_raw", None)
    return blocks


def _num_or_zero(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def public_payload(item, mom, share, audio_url=None):
    """Everything the public page may know about this meeting.

    ASSEMBLED, not filtered: only the keys written below can ever leave this
    function, so no future attribute on the recording row (owner_user_id,
    device_id, the S3 key, CRM records, chat history, folder membership) can
    leak by being forgotten. See the module docstring.

    CONTENT CASCADE — the same one src/app/recording/[key]/index.tsx uses, in
    the same order, so the shared page shows what the owner sees:
        overview.sections  ->  the MoM structure  ->  legacy summary/highlights
    """
    overview = _overview_sections(item, share)
    sections = public_sections(mom, share)

    payload = {
        "title": (item.get("title") or "Meeting").strip() or "Meeting",
        "recorded_at": item.get("created_at") or "",
        "duration": _int_or_none(item.get("duration")),
        "language": item.get("language") or "",
        "overview": overview,
        # Only used when there is no overview — otherwise the same content
        # would render twice under two sets of headings, the exact failure
        # mom_schema.build_sections guards against server-side.
        "sections": [] if overview else sections,
        "transcript": None,
        "speaker_blocks": [],
        "audio_url": None,
        "expires_at": share.get("expires_at") or None,
    }

    # Both of these are gated on the SERVER, not in the template. A disabled
    # toggle means the value never enters the payload, so it cannot appear in
    # the page source either.
    if share.get("transcript_enabled"):
        transcript = (item.get("transcript") or "").strip()
        if transcript:
            payload["transcript"] = transcript[:MAX_TRANSCRIPT_CHARS]
        payload["speaker_blocks"] = public_speaker_blocks(item, share)

    if share.get("audio_enabled") and audio_url:
        payload["audio_url"] = audio_url

    return payload


def _int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTML — one self-contained, responsive, theme-aware page.
# ---------------------------------------------------------------------------
def esc(value):
    """Escape for text content AND attribute values (quote=True).

    Every single interpolation below goes through this. Meeting content is
    user- and AI-authored text that has never been sanitised for markup, so
    treating it as trusted here would make the share page an XSS delivery
    mechanism aimed at people who are not even MinuteX users.
    """
    return html.escape(str(value if value is not None else ""), quote=True)


def _fmt_date(iso):
    at = _parse_iso(iso)
    if at is None:
        return ""
    return at.strftime("%d %B %Y, %I:%M %p").replace(" 0", " ").lstrip("0")


def _fmt_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes} min"
    return f"{seconds} sec"


def _paragraphs(text):
    blocks = [b.strip() for b in re.split(r"\n{2,}", text or "") if b.strip()]
    return "".join(
        f"<p>{esc(b).replace(chr(10), '<br/>')}</p>" for b in blocks
    )

def _fmt_ts(seconds):
    """Seconds -> "MM:SS", zero-padded on BOTH halves.

    Mirrors lib/transcript-view.tsx fmtTs(): the timecodes sit in a
    fixed-width tabular column and an unpadded minute makes it ragged.
    """
    total = int(seconds or 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def _section_rule(title, ai=False):
    """The app's SectionRule (lib/ui.tsx): a T.h3 heading ABOVE the card, not
    inside it, with the AI sparkle on the right for model-authored sections
    (lib/meeting-overview.tsx does exactly this)."""
    spark = '<span class="spark" aria-hidden="true">✦</span>' if ai else ""
    return f'<div class="rule"><h2>{esc(title)}</h2>{spark}</div>'


def _bullets(items):
    """A list Card: bullet rows separated by hairlines, blue 6px dot.
    Mirrors meeting-overview.tsx's itemRow/bullet styles."""
    rows = "".join(
        f'<div class="item"><span class="dot"></span>'
        f'<span class="itemtxt">{esc(t)}</span></div>'
        for t in items
    )
    return rows


def _overview_html(sections):
    """The Overview tab, rendered the way lib/meeting-overview.tsx renders it:
    for each AI section, a SectionRule heading then a Card. Prose sections use
    T.bodyLead; list sections use bullet rows. A section carrying both renders
    the prose as a lead-in with the bullets beneath — nothing is dropped."""
    out = []
    for sec in sections or []:
        title = sec.get("title") or ""
        content = sec.get("content") or ""
        items = sec.get("items") or []
        if sec.get("kind") == "list" and items and not content:
            body = _bullets(items)
            out.append(_section_rule(title, ai=True)
                       + f'<div class="card tight">{body}</div>')
            continue
        body = _paragraphs(content)
        if items:
            body += f'<div class="mt">{_bullets(items)}</div>'
        if not body:
            continue
        out.append(_section_rule(title, ai=True) + f'<div class="card">{body}</div>')
    return "".join(out)


def _section_html(section):
    """One MoM section as a SectionRule + Card.

    The FALLBACK path only — used when a recording predates the dynamic
    Overview. Styled identically to the overview cards so the two are
    indistinguishable to a reader.
    """
    kind = section.get("kind")
    title = section.get("title") or ""
    body = ""
    tight = False

    if kind == mom_schema.KIND_TEXT:
        body = _paragraphs(section.get("text") or "")
    elif kind == mom_schema.KIND_LIST:
        items = [i for i in (section.get("items") or []) if i]
        if items:
            body = _bullets(items)
            tight = True
    elif kind == mom_schema.KIND_FIELDS:
        rows = "".join(
            f'<div class="field"><span class="flabel">{esc(f["label"])}</span>'
            f'<span class="fvalue">{esc(f["value"]).replace(chr(10), "<br/>")}'
            f'</span></div>'
            for f in section.get("fields") or []
        )
        body = rows
    elif kind == mom_schema.KIND_TABLE:
        columns = section.get("columns") or []
        head = "".join(f"<th scope=\"col\">{esc(c)}</th>" for c in columns)
        rows = "".join(
            "<tr>" + "".join(
                f'<td data-label="{esc(columns[i] if i < len(columns) else "")}">'
                f'{esc(v).replace(chr(10), "<br/>")}</td>'
                for i, v in enumerate(row)
            ) + "</tr>"
            for row in section.get("rows") or []
        )
        if rows:
            body = (f'<div class="tablewrap"><table class="grid">'
                    f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody>"
                    f"</table></div>")

    if not body:
        return ""
    klass = "card tight" if tight else "card"
    return _section_rule(title) + f'<div class="{klass}">{body}</div>'


def _transcript_html(payload):
    """The Transcript tab, reproducing lib/transcript-view.tsx's speaker
    blocks: a 44px right-aligned tabular timecode gutter, then a coloured
    speaker dot + name, then the text.

    The timecodes are NOT links here. In the app they seek the shared player;
    on a public page there is no wired player, and a timecode that looked
    tappable but did nothing would be worse than one that plainly does not.
    """
    blocks = payload.get("speaker_blocks") or []
    if blocks:
        rows = "".join(
            f'<div class="sblock">'
            f'<div class="stime">{esc(_fmt_ts(b.get("start")))}</div>'
            f'<div class="sbody">'
            f'<div class="sname c{int(b.get("colour", 0)) % 6}">'
            f'<span class="sdot"></span>{esc(b.get("speaker") or "Speaker")}'
            f"</div>"
            f'<p class="stext">{esc(b.get("text") or "")}</p>'
            f"</div></div>"
            for b in blocks
        )
        return rows
    # No diarization: the flat transcript is still the source of truth and
    # must still be readable (the app's `plain` style).
    text = payload.get("transcript") or ""
    return f'<div class="plain">{_paragraphs(text)}</div>' if text else ""


def _player_html(payload):
    """The hero audio player, matching lib/audio-player.tsx's "hero" variant:
    a Card holding a 52px circular blue play button and the timecodes.

    The controls are the browser's own <audio> element — a hand-built
    transport would be a new interaction pattern, which is exactly what this
    page must not introduce. It is wrapped in the app's Card so it sits in the
    layout the way the app's player does.
    """
    url = payload.get("audio_url")
    if not url:
        return ""
    return (
        '<div class="card player">'
        f'<audio controls preload="metadata" src="{esc(url)}">'
        "Your browser cannot play this audio.</audio>"
        "</div>"
    )


def render_page(payload, brand="MinuteX"):
    """The complete public meeting page, in the MinuteX app's visual language.

    THIS IS THE APP'S MEETING SCREEN, READ-ONLY ON THE WEB — not a second
    design. Every token below is lifted from app/lib/theme.tsx and every
    structure from src/app/recording/[key]/index.tsx, so a recipient who also
    uses MinuteX recognises it immediately:

        masthead   T.headline title + dateline          (index.tsx)
        player     hero Card, 52px blue play button     (audio-player.tsx)
        tabs       SegmentedTabs — underline, not pills (ui.tsx)
        overview   SectionRule + Card, blue bullets     (meeting-overview.tsx)
        transcript timecode gutter + speaker blocks     (transcript-view.tsx)

    TABS ARE CONDITIONAL, matching the app's own `showTabs`: with only AI
    notes shared there is one view and no tab bar, because a single tab is
    just a label. Adding the transcript produces "Overview | Transcript".

    The tab switch is 14 lines of inline JS. It is the only script on the
    page, it touches nothing but a class name, and both panels are in the DOM
    either way — so with JS disabled the reader still gets all the content,
    just stacked.
    """
    title = esc(payload.get("title") or "Meeting")
    when = _fmt_date(payload.get("recorded_at"))
    duration = _fmt_duration(payload.get("duration"))
    dateline = esc(" · ".join(b for b in (when, duration) if b))

    overview = _overview_html(payload.get("overview"))
    if not overview:
        overview = "".join(_section_html(s) for s in payload.get("sections") or [])

    transcript = _transcript_html(payload)
    player = _player_html(payload)

    if not (overview or transcript or player):
        overview = ('<div class="card"><p class="muted">The owner has not '
                    "shared any content from this meeting.</p></div>")

    # Exactly the app's rule: tabs appear only when there is more than one
    # view to switch between.
    tabs = ""
    if overview and transcript:
        tabs = (
            '<div class="tabs" role="tablist">'
            '<button class="tab on" role="tab" aria-selected="true" '
            'data-panel="overview">Overview</button>'
            '<button class="tab" role="tab" aria-selected="false" '
            'data-panel="transcript">Transcript</button>'
            "</div>"
        )

    # `hidden` is the HTML ATTRIBUTE, never a class. The CSS rule is
    # `.panel[hidden]`, and the tab script toggles the `hidden` PROPERTY — so
    # a class named "hidden" would match neither, and the transcript would
    # render stacked underneath the Overview until the first tab click set the
    # real property. That was a live bug; this is the fix, and the test
    # `test_transcript_never_renders_below_the_overview` pins it.
    #
    # Only meaningful when there is a tab bar to switch with: with no
    # Overview, the transcript IS the only panel and must stay visible.
    panels = ""
    if overview:
        panels += f'<div class="panel" id="p-overview">{overview}</div>'
    if transcript:
        hidden = " hidden" if overview else ""
        # The heading is redundant directly beneath a tab that already says
        # "Transcript" — the app's own transcript screen has no such heading
        # either. It is only added when there is NO tab bar, where the section
        # would otherwise start with no label at all.
        heading = "" if overview else _section_rule("Transcript")
        panels += (f'<div class="panel" id="p-transcript"{hidden}>'
                   f'{heading}{transcript}</div>')

    expiry_note = ""
    if payload.get("expires_at"):
        pretty = _fmt_date(payload["expires_at"])
        if pretty:
            expiry_note = (f'<p class="muted">This link expires on '
                           f"{esc(pretty)}.</p>")

    script = """
<script>
(function(){
  var tabs=document.querySelectorAll('.tab');
  for(var i=0;i<tabs.length;i++){
    tabs[i].addEventListener('click',function(){
      var want=this.getAttribute('data-panel');
      for(var j=0;j<tabs.length;j++){
        var on=tabs[j]===this;
        tabs[j].className=on?'tab on':'tab';
        tabs[j].setAttribute('aria-selected',on?'true':'false');
      }
      var ps=document.querySelectorAll('.panel');
      for(var k=0;k<ps.length;k++){
        ps[k].hidden=(ps[k].id!=='p-'+want);
      }
    });
  }
})();
</script>""" if tabs else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<!-- A shared meeting is private content behind an unguessable URL. Search
     engines must never index it, and no crawler should follow anything on it. -->
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex"/>
<meta name="referrer" content="no-referrer"/>
<meta name="color-scheme" content="light dark"/>
<meta name="theme-color" content="#F6F7FB" media="(prefers-color-scheme: light)"/>
<meta name="theme-color" content="#0B0D14" media="(prefers-color-scheme: dark)"/>
<title>{title} · {esc(brand)}</title>
<!-- The app's two families (app/lib/theme.tsx FONT). Loaded so the shared page
     is typographically the SAME product, not a lookalike. -->
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet"/>
<style>
  /* ===================================================================
     Tokens lifted VERBATIM from app/lib/theme.tsx. Light = LIGHT scale,
     dark = DARK scale, switched on prefers-color-scheme exactly as the
     app switches on Appearance. Changing a colour here without changing
     it there is what makes the two drift, so they are labelled.
     =================================================================== */
  :root {{
    --bg:#F6F7FB; --surface:#FFFFFF; --surface2:#F0F2F7;
    --border:#E6E9F0; --border-strong:#D8DCE6;
    --primary:#3E6BFF; --primary-soft:#EAF0FF;
    --accent:#7C5CFF;
    --text:#12131A; --text-dim:#5B6072; --text-faint:#9297A8;
    --shadow:rgba(26,32,51,0.06); --shadow-md:rgba(26,32,51,0.08);
    --sp0:#3E6BFF; --sp1:#1FA972; --sp2:#7C5CFF;
    --sp3:#F5A623; --sp4:#E5484D; --sp5:#0EA5B7;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#0B0D14; --surface:#151823; --surface2:#1D2130;
      --border:#262B3B; --border-strong:#323852;
      --primary:#5C86FF; --primary-soft:#1A2340;
      --accent:#9B82FF;
      --text:#F3F4F8; --text-dim:#9BA0B4; --text-faint:#666C82;
      --shadow:rgba(0,0,0,0.35); --shadow-md:rgba(0,0,0,0.45);
      --sp0:#5C86FF; --sp1:#3DCB93; --sp2:#9B82FF;
      --sp3:#FFB84D; --sp4:#FF6B6E; --sp5:#41C6DA;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--text);
    font-family:"Plus Jakarta Sans",-apple-system,BlinkMacSystemFont,
                "Segoe UI",Roboto,sans-serif;
    font-size:14.5px; line-height:21px;
    -webkit-text-size-adjust:100%; -webkit-font-smoothing:antialiased;
  }}
  /* index.tsx: container paddingHorizontal 20, paddingBottom 40 */
  .wrap {{ max-width:720px; margin:0 auto; padding:20px 20px 40px; }}

  /* ---- Brand bar. The app has a native header here; on the web the
     product needs naming, so this is the one element with no direct
     counterpart. Deliberately small and quiet. ---- */
  .brand {{
    display:flex; align-items:center; gap:8px;
    font-weight:800; font-size:13px; letter-spacing:-0.2px;
    color:var(--primary); margin-bottom:20px;
  }}
  .brand .mark {{
    width:22px; height:22px; border-radius:7px;
    background:linear-gradient(135deg,var(--primary),var(--accent));
  }}
  .brand .ro {{
    margin-left:auto; font-family:inherit; font-weight:600; font-size:11px;
    letter-spacing:0.6px; text-transform:uppercase; color:var(--text-faint);
  }}

  /* ---- Masthead. index.tsx st.headline / st.dateline ---- */
  h1 {{
    font-weight:800; font-size:21px; line-height:26px; letter-spacing:-0.2px;
    color:var(--text); margin:0;
  }}
  .dateline {{
    font-weight:500; font-size:12.5px; color:var(--text-faint);
    margin:4px 0 0;
  }}

  /* ---- SectionRule (ui.tsx): T.h3 heading ABOVE the card ---- */
  .rule {{
    display:flex; align-items:center; justify-content:space-between;
    margin:24px 0 12px;
  }}
  .rule h2 {{
    font-weight:600; font-size:15px; line-height:20px; color:var(--text);
    margin:0; letter-spacing:0;
  }}
  .spark {{ color:var(--accent); font-size:15px; line-height:1; }}

  /* ---- Card (ui.tsx): surface, R.card 16, hairline, ELEV.sm ---- */
  .card {{
    background:var(--surface); border:1px solid var(--border);
    border-radius:16px; padding:16px;
    box-shadow:0 1px 3px var(--shadow);
  }}
  .card + .card {{ margin-top:12px; }}
  .card.tight {{ padding:8px 16px; }}
  .card p {{
    font-size:15.5px; line-height:24px; color:var(--text); margin:0 0 10px;
  }}
  .card p:last-child {{ margin-bottom:0; }}
  .mt {{ margin-top:8px; }}

  /* ---- Bullet rows (meeting-overview.tsx itemRow / bullet) ---- */
  .item {{
    display:flex; align-items:flex-start; gap:12px; padding:9px 0;
  }}
  .item + .item {{ border-top:1px solid var(--border); }}
  .dot {{
    width:6px; height:6px; border-radius:3px; background:var(--primary);
    margin-top:8px; flex:0 0 auto;
  }}
  .itemtxt {{
    font-weight:500; font-size:14.5px; line-height:21px; color:var(--text);
  }}

  /* ---- SegmentedTabs (ui.tsx): underline indicator, never pills ---- */
  .tabs {{
    display:flex; position:relative; gap:0;
    border-bottom:1.5px solid var(--border); margin-top:24px;
  }}
  .tab {{
    appearance:none; background:none; border:0; cursor:pointer;
    padding:0 16px 11px; font-family:inherit; font-weight:600;
    font-size:14.5px; color:var(--text-faint);
    border-bottom:2.5px solid transparent; margin-bottom:-1.5px;
  }}
  .tab.on {{ font-weight:700; color:var(--primary);
             border-bottom-color:var(--primary); }}
  /* Both selectors on purpose. `[hidden]` is what the markup and the tab
     script actually use; `.hidden` is a belt-and-braces catch so that if a
     future edit reaches for the class form, the panel still hides instead of
     silently stacking under the Overview — which is exactly how that bug
     shipped once. `display:none` also beats the UA's weak `hidden` rule,
     which a `display:flex` on .panel would otherwise override. */
  .panel[hidden], .panel.hidden {{ display:none !important; }}

  /* ---- Hero player (audio-player.tsx heroCard) ---- */
  .card.player {{ padding:14px 16px; }}
  .player audio {{ width:100%; display:block; }}

  /* ---- Speaker blocks (transcript-view.tsx) ---- */
  .sblock {{ display:flex; gap:12px; margin-top:18px; }}
  .stime {{
    width:44px; flex:0 0 auto; text-align:right;
    font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
    font-variant-numeric:tabular-nums; font-size:10.5px; line-height:18px;
    color:var(--text-faint);
  }}
  .sbody {{ flex:1; min-width:0; }}
  .sname {{
    display:flex; align-items:center; gap:6px;
    font-weight:700; font-size:12.5px;
  }}
  .sdot {{ width:8px; height:8px; border-radius:4px;
           background:currentColor; flex:0 0 auto; }}
  .sname.c0 {{ color:var(--sp0); }} .sname.c1 {{ color:var(--sp1); }}
  .sname.c2 {{ color:var(--sp2); }} .sname.c3 {{ color:var(--sp3); }}
  .sname.c4 {{ color:var(--sp4); }} .sname.c5 {{ color:var(--sp5); }}
  .stext {{
    font-weight:400; font-size:14.5px; line-height:21px; color:var(--text);
    margin:5px 0 0;
  }}
  .plain {{ font-size:14.5px; line-height:21px; margin-top:16px; }}

  /* ---- MoM fallback: field rows and tables ---- */
  .field {{ display:flex; gap:12px; padding:7px 0; }}
  .field + .field {{ border-top:1px solid var(--border); }}
  .flabel {{
    width:38%; flex:0 0 auto; font-weight:600; font-size:13px;
    color:var(--text-dim);
  }}
  .fvalue {{ font-size:14.5px; color:var(--text); }}
  .tablewrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
  .grid {{ width:100%; border-collapse:collapse; }}
  .grid th, .grid td {{
    text-align:left; padding:9px 10px; vertical-align:top;
    border-bottom:1px solid var(--border); font-size:14px;
  }}
  .grid th {{
    font-weight:600; font-size:11px; letter-spacing:0.6px;
    text-transform:uppercase; color:var(--text-dim); white-space:nowrap;
  }}
  .grid tbody tr:last-child td {{ border-bottom:none; }}

  .muted {{ font-size:13.5px; line-height:20px; color:var(--text-dim); }}
  footer {{
    margin-top:32px; padding-top:16px; border-top:1px solid var(--border);
    text-align:center;
  }}
  footer p {{
    font-size:11.5px; color:var(--text-faint); margin:0 0 4px;
  }}

  @media (max-width:560px) {{
    .wrap {{ padding:16px 16px 32px; }}
    h1 {{ font-size:20px; line-height:25px; }}
    .tab {{ padding:0 12px 11px; }}
    /* The app's Action Items grid is wide; stack it rather than force a
       horizontal scroll through the whole page. */
    .grid thead {{ display:none; }}
    .grid, .grid tbody, .grid tr, .grid td {{ display:block; width:100%; }}
    .grid tr {{ border-bottom:1px solid var(--border); padding:8px 0; }}
    .grid tr:last-child {{ border-bottom:none; }}
    .grid td {{ border:none; padding:3px 0; }}
    .grid td:before {{
      content:attr(data-label); display:block; font-size:11px;
      letter-spacing:0.6px; text-transform:uppercase; color:var(--text-dim);
    }}
    .grid td:empty {{ display:none; }}
    .field {{ display:block; }}
    .flabel {{ width:100%; display:block; font-size:11px;
               letter-spacing:0.6px; text-transform:uppercase; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="brand">
      <span class="mark" aria-hidden="true"></span>{esc(brand)}
      <span class="ro">Shared · Read-only</span>
    </header>

    <h1>{title}</h1>
    <p class="dateline">{dateline}</p>

    {player}
    {tabs}
    {panels}

    <footer>
      <p>Shared from {esc(brand)} — your AI meeting workspace.</p>
      {expiry_note}
    </footer>
  </div>{script}
</body>
</html>"""


def render_error_page(status, headline, detail, brand="MinuteX"):
    """The page a dead link gets.

    Every failure — unknown token, revoked, expired — renders from here, and
    the caller passes the same generic headline for the first two. A page that
    distinguished "no such share" from "revoked" would confirm to someone
    holding a stale link that the meeting exists, which is exactly the probe
    the authenticated routes already refuse (404, never 403).

    Same tokens as the meeting page, so a dead link still looks like MinuteX
    rather than like a server error.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex, nofollow"/>
<meta name="referrer" content="no-referrer"/>
<meta name="color-scheme" content="light dark"/>
<title>{esc(headline)} · {esc(brand)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;800&display=swap" rel="stylesheet"/>
<style>
  :root {{
    --bg:#F6F7FB; --surface:#FFFFFF; --border:#E6E9F0;
    --primary:#3E6BFF; --accent:#7C5CFF;
    --text:#12131A; --text-dim:#5B6072; --shadow:rgba(26,32,51,0.06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#0B0D14; --surface:#151823; --border:#262B3B;
      --primary:#5C86FF; --accent:#9B82FF;
      --text:#F3F4F8; --text-dim:#9BA0B4; --shadow:rgba(0,0,0,0.35);
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--text);
    display:flex; min-height:100vh; align-items:center;
    justify-content:center; padding:24px;
    font-family:"Plus Jakarta Sans",-apple-system,BlinkMacSystemFont,
                "Segoe UI",Roboto,sans-serif;
  }}
  .box {{
    background:var(--surface); border:1px solid var(--border);
    border-radius:16px; box-shadow:0 1px 3px var(--shadow);
    padding:28px 24px; max-width:420px; width:100%; text-align:center;
  }}
  .brand {{
    display:flex; align-items:center; justify-content:center; gap:8px;
    font-weight:800; font-size:13px; color:var(--primary); margin-bottom:16px;
  }}
  .mark {{
    width:22px; height:22px; border-radius:7px;
    background:linear-gradient(135deg,var(--primary),var(--accent));
  }}
  h1 {{ font-weight:800; font-size:19px; line-height:25px;
        letter-spacing:-0.2px; margin:0 0 8px; }}
  p {{ font-size:14.5px; line-height:21px; color:var(--text-dim); margin:0; }}
</style>
</head>
<body>
  <div class="box">
    <div class="brand"><span class="mark" aria-hidden="true"></span>{esc(brand)}</div>
    <h1>{esc(headline)}</h1>
    <p>{esc(detail)}</p>
  </div>
</body>
</html>"""
