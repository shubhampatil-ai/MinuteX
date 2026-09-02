"""email_message.py — building the RFC 2822 message MinuteX sends.

Pure functions, no network, no AWS. That is the point: the rules worth getting
right here — who a recipient actually is, what an attachment is allowed to be,
how a MoM becomes an email body — are all decidable from their inputs, so they
are testable without stubbing Gmail.

THREE THINGS THIS MODULE IS OPINIONATED ABOUT:

  RECIPIENTS ARE RESOLVED, NEVER GUESSED. The product rule is explicit: if a
  participant has no email address, MinuteX must say so and stop, not silently
  drop them from the send. resolve_recipients() therefore returns BOTH the
  addresses it resolved and the people it could not resolve, and the caller
  refuses the send when the second list is non-empty. Returning only the good
  ones would make "sent to 2 of 3 people" indistinguishable from "sent to
  everyone", which is exactly the failure the requirement calls out.

  ATTACHMENTS ARE VALIDATED BEFORE THEY ARE ENCODED. The client renders the
  PDF/DOCX (that is where the renderers live — lib/mom-pdf.ts, lib/mom-docx.ts
  — and moving them server-side would mean a second implementation that drifts
  from the one the user previews). So attachment bytes arrive base64 from the
  client, and the backend treats them as untrusted input: declared type must be
  one of a small allow-list, the filename is sanitised to a leaf name, and the
  total size is bounded before anything is built.

  THE BODY IS BOTH TEXT AND HTML. A meeting summary read in a mail client
  should look like a document, and read in a plain-text client should still be
  readable. multipart/alternative costs almost nothing here and avoids the
  usual "why is my email a wall of markdown asterisks".
"""

import base64
import re
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

# Deliberately the SAME pattern lambda_function.py uses for contact emails
# (_EMAIL_RE). Sending is not the place to be more permissive than storing:
# an address this rejects could never have been saved on a contact anyway.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Bounds. Gmail's own hard limit on a message is 25 MB after base64 expansion;
# these sit well under it because the real constraint is the Lambda's own
# request size (API Gateway caps a payload at 10 MB) and because a MoM PDF is
# tens of kilobytes. Generous enough never to be hit by a real document,
# small enough that this endpoint cannot be used as a file relay.
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024      # per file
MAX_TOTAL_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_RECIPIENTS = 25
MAX_SUBJECT_CHARS = 400
MAX_BODY_CHARS = 200_000

# What the client is allowed to attach. An allow-list, not a deny-list: the
# only artefacts MinuteX produces are these four, and anything else arriving
# here means either a bug or an attempt to use the mail route as a general
# file sender.
ATTACHMENT_TYPES = {
    "pdf": ("application", "pdf", ".pdf"),
    "docx": ("application",
             "vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".docx"),
    "md": ("text", "markdown", ".md"),
    "txt": ("text", "plain", ".txt"),
}


class EmailError(ValueError):
    """A message that cannot be built. Always carries user-facing wording —
    these surface directly, so they must read as instructions, not diagnostics.
    """


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------
def normalize_email(value) -> str:
    """Lower-cased bare address, or "" if it isn't one.

    parseaddr first so "Rahul Sharma <r@x.com>" is accepted — contacts imported
    from a phone address book sometimes carry that form.
    """
    _, addr = parseaddr(str(value or "").strip())
    addr = addr.strip().lower()
    return addr if EMAIL_RE.match(addr) else ""


def format_recipient(name: str, email: str) -> str:
    """"Rahul Sharma <rahul@x.com>", or the bare address when there's no name.

    formataddr handles the quoting rules for names containing commas or
    quotes, which is exactly the kind of thing a contact name can contain.
    """
    name = str(name or "").strip()
    return formataddr((name, email)) if name else email


def resolve_recipients(requested) -> tuple:
    """Split requested recipients into (resolved, unresolved).

    `requested` is a list of {name?, email?, contact_id?} — whatever the caller
    could assemble about each person. This function does not know about
    Contacts or DynamoDB; the caller resolves contact_id -> row and passes the
    email it found (or none). That keeps the decision rule — "no address means
    refuse, not skip" — in one testable place.

    Returns:
        resolved   [{"name", "email", "contact_id"}]  deduped, order preserved
        unresolved [{"name", "contact_id"}]           people with no address

    Dedupe is by address, keeping the FIRST occurrence: the same person picked
    twice (once as a participant, once from contacts) must receive one email,
    and the first entry is the one whose name the user saw.
    """
    resolved, unresolved, seen = [], [], set()
    for entry in requested or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        email = normalize_email(entry.get("email"))
        contact_id = str(entry.get("contact_id") or "").strip()
        if not email:
            unresolved.append({"name": name or "This participant",
                               "contact_id": contact_id})
            continue
        if email in seen:
            continue
        seen.add(email)
        resolved.append({"name": name, "email": email, "contact_id": contact_id})
    return resolved, unresolved


def describe_unresolved(unresolved) -> str:
    """The user-facing sentence for people with no email address.

    Named rather than inlined because both the send route and the recipient
    preview route must say the same thing — the app shows this before the user
    presses Send, and the backend repeats it if the state changed in between.
    """
    names = [u.get("name") or "This participant" for u in unresolved]
    if not names:
        return ""
    if len(names) == 1:
        return f"Email address unavailable for {names[0]}."
    listed = ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"Email addresses unavailable for {listed}."


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def safe_filename(name: str, fallback: str, extension: str) -> str:
    """A leaf filename that cannot traverse or hide its type.

    The name reaches us from the client and ends up in a Content-Disposition
    header. Directory separators are stripped (not escaped) so nothing can
    imply a path, control characters and quotes go because they would break
    the header, and the correct extension is enforced rather than trusted —
    a ".pdf" label on DOCX bytes is a mislabelled file in someone's inbox.
    """
    name = str(name or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f"\r\n]+', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if not name.lower().endswith(extension):
        name = name[: 120 - len(extension)] + extension
    return name[:140]


def decode_attachment(entry) -> dict:
    """Validate and decode ONE client-supplied attachment.

    Returns {"filename", "maintype", "subtype", "data": bytes}.
    Raises EmailError with user-facing wording on anything wrong.
    """
    if not isinstance(entry, dict):
        raise EmailError("That attachment could not be read.")

    kind = str(entry.get("type") or "").strip().lower()
    spec = ATTACHMENT_TYPES.get(kind)
    if not spec:
        raise EmailError("MinuteX can only attach PDF, DOCX, Markdown or text "
                         "files.")
    maintype, subtype, extension = spec

    raw = entry.get("content_base64") or entry.get("content") or ""
    if not isinstance(raw, str) or not raw.strip():
        raise EmailError("That attachment arrived empty.")
    try:
        # validate=False: clients legitimately send newline-wrapped base64.
        data = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError):
        raise EmailError("That attachment could not be read.")
    if not data:
        raise EmailError("That attachment arrived empty.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        raise EmailError(f"Attachments must be smaller than {mb} MB.")

    return {
        "filename": safe_filename(entry.get("filename"),
                                  f"minutex{extension}", extension),
        "maintype": maintype,
        "subtype": subtype,
        "data": data,
    }


def decode_attachments(entries) -> list:
    """Validate the whole attachment set, including the aggregate size."""
    entries = entries or []
    if len(entries) > MAX_ATTACHMENTS:
        raise EmailError(f"Attach at most {MAX_ATTACHMENTS} files.")
    out, total = [], 0
    for entry in entries:
        att = decode_attachment(entry)
        total += len(att["data"])
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            mb = MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)
            raise EmailError(f"Those attachments total more than {mb} MB.")
        out.append(att)
    return out


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------
_HTML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def _escape(text: str) -> str:
    for a, b in _HTML_ESCAPES:
        text = text.replace(a, b)
    return text


def text_to_html(body: str) -> str:
    """A plain-text body as simple, safe HTML.

    Not a Markdown renderer on purpose. The bodies MinuteX sends are short
    prose the user can edit before sending, and running arbitrary user text
    through a Markdown-to-HTML converter would be a way to inject markup into
    someone else's inbox. Escape everything, honour paragraph and line breaks,
    and stop there.
    """
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p for p in body.split("\n\n")]
    html_parts = []
    for para in paragraphs:
        if not para.strip():
            continue
        html_parts.append("<p>" + _escape(para).replace("\n", "<br>") + "</p>")
    if not html_parts:
        html_parts.append("<p></p>")
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,'
        'Arial,sans-serif;font-size:14px;line-height:1.55;color:#12131A">'
        + "".join(html_parts)
        + "</div>"
    )


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------
def build_message(sender: str, sender_name: str, to, subject: str, body: str,
                  cc=None, attachments=None, html_body: str = "") -> str:
    """Assemble the message and return it base64url-encoded, ready for Gmail's
    users.messages.send `raw` field.

    Structure: multipart/mixed [ multipart/alternative [text, html], *files ]
    when there are attachments, and just the alternative part when there are
    not — a message with no attachments should not claim to be mixed.

    Header() is used for the subject so a non-ASCII meeting title (which is
    ordinary — meeting titles come from real speech) is RFC 2047 encoded
    rather than mangled or rejected.
    """
    to = list(to or [])
    cc = list(cc or [])
    if not to:
        raise EmailError("Choose at least one recipient.")
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise EmailError(f"Send to at most {MAX_RECIPIENTS} people at a time.")

    subject = str(subject or "").strip()
    if not subject:
        raise EmailError("Add a subject.")
    if len(subject) > MAX_SUBJECT_CHARS:
        subject = subject[:MAX_SUBJECT_CHARS]
    # A newline in a header is header injection — it would let a crafted
    # subject add its own Bcc. Collapse rather than reject: the user typed a
    # subject, not an attack, and the meeting title is often the source.
    subject = re.sub(r"[\r\n]+", " ", subject)

    body = str(body or "")
    if len(body) > MAX_BODY_CHARS:
        raise EmailError("That message is too long to send.")

    attachments = decode_attachments(attachments)

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(body, "plain", "utf-8"))
    alternative.attach(MIMEText(html_body or text_to_html(body), "html", "utf-8"))

    if attachments:
        root = MIMEMultipart("mixed")
        root.attach(alternative)
        for att in attachments:
            part = MIMEApplication(att["data"], _subtype=att["subtype"])
            # Set the full type explicitly: MIMEApplication forces
            # maintype "application", which is wrong for text/markdown.
            part.set_type(f"{att['maintype']}/{att['subtype']}")
            part.add_header("Content-Disposition", "attachment",
                            filename=att["filename"])
            root.attach(part)
    else:
        root = alternative

    root["To"] = ", ".join(to)
    if cc:
        root["Cc"] = ", ".join(cc)
    root["From"] = format_recipient(sender_name, sender)
    root["Subject"] = str(Header(subject, "utf-8"))

    return base64.urlsafe_b64encode(root.as_bytes()).decode("ascii")
