#!/usr/bin/env python3
"""test_meeting_share.py — Meeting Share, owner routes and the public page.

This feature publishes meeting content to the open internet, so the tests
that matter most are the ones asserting what does NOT come out. The file is
organised around that: the happy path is a handful of cases, and the bulk
below pins the ways a share must refuse.

WHY EACH GROUP EXISTS — every one of these is a real failure mode, not a
box-tick:

  OWNERSHIP     A non-owner creating a share for someone else's recording
                would be a total authorization bypass — one POST and a
                stranger's meeting is public. Must 404 (never 403: the
                existing recording routes report a miss and a forbidden
                identically so the API cannot be used to probe for keys, and
                the share routes inherit that rule).

  LIFECYCLE     Revocation and expiry are the ONLY ways an owner can take a
                link back. If either is checked in the wrong place — or
                checked in the app instead of the server — "revoke" means
                "hide the button" and the URL keeps working forever.

  TOGGLES       transcript_enabled/audio_enabled default to FALSE, and a
                disabled toggle must mean the value never enters the payload
                — not that it is rendered and hidden with CSS. These assert
                against the raw HTML, because "not visible" and "not present"
                are very different when anyone can View Source.

  LEAKAGE       The public payload is ASSEMBLED, never filtered down from the
                recording row. The test that proves it is the one that stuffs
                the row with owner_user_id, device ids, S3 keys, CRM records
                and chat history, then asserts none of it reaches the page.
                A deny-list would pass this today and fail the day someone
                adds an attribute; the assembly approach cannot.

  TOKENS        Only sha256(token) may be stored. A test asserts the raw
                token appears nowhere in the persisted row, because a table
                dump that yields working links is the worst outcome here.

OFFLINE by design, same as the rest of this directory: fake_dynamodb tables,
no AWS, no network, no Groq. Run it before every deploy — 38_deploy_meeting_
share.sh does exactly that.

Run:  python -m pytest tests/test_meeting_share.py
"""
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))

# Importing test_ai_workspace installs the boto3/Groq stubs and gives us the
# same `api` module object every other test in this directory binds — see
# conftest.py on why one shared stub identity matters.
from test_ai_workspace import (  # noqa: E402
    HIGHLIGHTS, RECORDING, api, call, parse,
)

import fake_dynamodb as fdb  # noqa: E402
import share_schema  # noqa: E402

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
OWNER = "u-1"
STRANGER = "u-2"


def owner_event(method, route, body=None, key=KEY, share_id=None, auth=True):
    """An API Gateway HTTP API v2.0 event for an owner-side share route."""
    params = {}
    if key is not None:
        params["key"] = key
    if share_id is not None:
        params["share_id"] = share_id
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": "/"},
                           "stage": "$default"},
        "pathParameters": params,
        "headers": {"host": "api.example.com"},
    }
    if auth:
        ev["headers"]["authorization"] = "Bearer test-token"
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def public_event(token):
    """The public route's event. Deliberately carries NO authorization header
    — that is the whole point of the endpoint."""
    return {
        "routeKey": "GET /share/{token}",
        "requestContext": {"http": {"method": "GET", "path": "/"},
                           "stage": "$default"},
        "pathParameters": {"token": token},
        "headers": {"host": "api.example.com"},
    }


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ShareTestCase(unittest.TestCase):
    """Real fake tables for Shares and Recordings.

    The Shares table is a genuine FakeTable rather than a MagicMock because
    almost every test here is a ROUND TRIP: create a link, then open it. A
    stub that only records the last call cannot tell a working token lookup
    from a broken one, and the token-index query is precisely the part worth
    proving.
    """

    def setUp(self):
        self.item = json.loads(json.dumps(RECORDING))
        self.item["meeting_highlights"] = json.loads(json.dumps(HIGHLIGHTS))

        self.shares = fdb.FakeTable(
            "Shares", "share_id",
            indexes={"token-index": ("token_hash", None),
                     "recording-index": ("recording_key", "created_at")})
        self.recordings = fdb.FakeTable("Recordings", "audio_s3_key")
        self.recordings.items[(KEY,)] = self.item

        self.s3 = mock.MagicMock()
        self.s3.generate_presigned_url.return_value = \
            "https://s3.example.com/signed?X-Amz-Signature=abc"

        patches = [
            # test_ai_workspace installs its own minimal Key whose .eq()
            # returns a bare tuple, and importing it above is what binds that
            # class into `api`. fake_dynamodb's query() needs the richer _Cond
            # (it reads .terms), so the two must agree HERE — see conftest.py
            # on why the whole directory shares one stub identity. Patched on
            # `api` only, so no other test file's expectations change.
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_shares", self.shares),
            mock.patch.object(api, "_recordings", self.recordings),
            mock.patch.object(api, "_s3", self.s3),
            mock.patch.object(api, "BUCKET_NAME", "minutex-audio"),
            mock.patch.object(api, "_require_auth", return_value=OWNER),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            mock.patch.object(api, "_tasks_for_recording", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- helpers ---------------------------------------------------------
    def create(self, body=None, expect=201):
        resp = call(api.create_share,
                    owner_event("POST", "/recordings/share/{key+}",
                                body=body if body is not None else {}))
        status, data = parse(resp)
        self.assertEqual(status, expect, data)
        return data

    def open_public(self, token):
        resp = api.public_share(public_event(token))
        return resp["statusCode"], resp["body"], resp["headers"]

    def stored(self, share_id):
        return self.shares.items[(share_id,)]


# ---------------------------------------------------------------------------
# 1 + 10. The owner can create a share, and nothing else changed.
# ---------------------------------------------------------------------------
class CreateShareTests(ShareTestCase):

    def test_owner_can_create_a_share(self):
        data = self.create()
        self.assertTrue(data["share_id"].startswith("shr_"))
        self.assertIn("/share/", data["url"])
        self.assertIsNone(data["expires_at"])
        # The V1 default: notes shared, transcript and audio withheld.
        share = data["share"]
        self.assertTrue(share["summary_enabled"])
        self.assertTrue(share["highlights_enabled"])
        self.assertTrue(share["decisions_enabled"])
        self.assertTrue(share["tasks_enabled"])
        self.assertTrue(share["participants_enabled"])
        self.assertFalse(share["transcript_enabled"])
        self.assertFalse(share["audio_enabled"])
        self.assertEqual(share["access_type"], "public")
        self.assertTrue(share["active"])

    def test_raw_token_is_never_stored(self):
        """The one property a database dump must not be able to defeat."""
        data = self.create()
        token = data["url"].rsplit("/", 1)[-1]
        row = self.stored(data["share_id"])

        self.assertNotIn("token", row)
        self.assertEqual(row["token_hash"], share_schema.hash_token(token))
        # The raw token must appear nowhere in the persisted row, under any
        # attribute name.
        self.assertNotIn(token, json.dumps(row, default=str))

    def test_create_honours_the_requested_config(self):
        data = self.create({"summary": True, "highlights": False,
                            "decisions": False, "tasks": True,
                            "participants": False, "transcript": True,
                            "audio": True})
        share = data["share"]
        self.assertTrue(share["summary_enabled"])
        self.assertFalse(share["highlights_enabled"])
        self.assertFalse(share["decisions_enabled"])
        self.assertTrue(share["tasks_enabled"])
        self.assertFalse(share["participants_enabled"])
        self.assertTrue(share["transcript_enabled"])
        self.assertTrue(share["audio_enabled"])

    def test_expires_in_days_is_accepted(self):
        data = self.create({"expires_at": 7})
        self.assertIsNotNone(data["expires_at"])
        at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        delta = at - datetime.now(timezone.utc)
        self.assertGreater(delta.total_seconds(), 6.5 * 86400)
        self.assertLess(delta.total_seconds(), 7.5 * 86400)

    def test_expiry_in_the_past_is_rejected(self):
        resp = call(api.create_share, owner_event(
            "POST", "/recordings/share/{key+}",
            body={"expires_at": iso(datetime.now(timezone.utc)
                                    - timedelta(days=1))}))
        status, data = parse(resp)
        self.assertEqual(status, 400)
        self.assertIn("future", data["error"])

    def test_share_url_uses_the_configured_base_when_set(self):
        with mock.patch.object(api, "SHARE_BASE_URL", "https://share.minutex.app"):
            data = self.create()
        self.assertTrue(data["url"].startswith("https://share.minutex.app/share/"))


# ---------------------------------------------------------------------------
# 2. A non-owner cannot create a share.
# ---------------------------------------------------------------------------
class OwnershipTests(ShareTestCase):

    def test_non_owner_cannot_create_a_share(self):
        """The total-bypass case: one POST must not make a stranger's meeting
        public. 404, not 403 — see the module docstring."""
        with mock.patch.object(api, "_require_auth", return_value=STRANGER):
            resp = call(api.create_share, owner_event(
                "POST", "/recordings/share/{key+}", body={}))
        status, data = parse(resp)
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "recording not found")
        self.assertEqual(self.shares.calls["put"], 0)

    def test_non_owner_cannot_list_shares(self):
        self.create()
        with mock.patch.object(api, "_require_auth", return_value=STRANGER):
            resp = call(api.list_shares,
                        owner_event("GET", "/recordings/shares/{key+}"))
        self.assertEqual(parse(resp)[0], 404)

    def test_non_owner_cannot_revoke_a_share(self):
        share_id = self.create()["share_id"]
        with mock.patch.object(api, "_require_auth", return_value=STRANGER):
            resp = call(api.revoke_share, owner_event(
                "DELETE", "/shares/{share_id}", key=None, share_id=share_id))
        status, data = parse(resp)
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "share not found")
        # And the link still works, because the revoke was refused.
        self.assertEqual(self.stored(share_id)["revoked_at"], "")

    def test_non_owner_cannot_retoggle_a_share(self):
        """The subtle bypass: not creating a link, but widening one that
        already exists to expose the transcript."""
        share_id = self.create()["share_id"]
        with mock.patch.object(api, "_require_auth", return_value=STRANGER):
            resp = call(api.update_share, owner_event(
                "PATCH", "/shares/{share_id}", body={"transcript": True},
                key=None, share_id=share_id))
        self.assertEqual(parse(resp)[0], 404)
        self.assertFalse(self.stored(share_id)["transcript_enabled"])


# ---------------------------------------------------------------------------
# 3 + 4. The public page: a valid token works with no JWT; junk 404s.
# ---------------------------------------------------------------------------
class PublicPageTests(ShareTestCase):

    def test_valid_token_works_without_a_jwt(self):
        token = self.create()["url"].rsplit("/", 1)[-1]
        # _require_auth is patched to succeed in this class, so to prove the
        # public route never consults it we make ANY call to it explode.
        with mock.patch.object(api, "_require_auth",
                               side_effect=AssertionError(
                                   "public share must not require auth")):
            status, body, headers = self.open_public(token)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(RECORDING["title"], body)

    def test_page_carries_the_privacy_headers(self):
        token = self.create()["url"].rsplit("/", 1)[-1]
        status, body, headers = self.open_public(token)
        self.assertEqual(status, 200)
        # A revoked link must not be served from a cache that outlives the
        # revocation.
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("noindex", headers["X-Robots-Tag"])
        self.assertIn("noindex", body)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_csp_permits_exactly_what_the_page_needs(self):
        """A CSP that blocks the app's own fonts or the tab switcher would
        ship a page that renders in the wrong typeface with dead tabs — and
        no test that only greps the header string would notice. These pin the
        four sources the page actually uses, and that nothing else is opened
        up."""
        token = self.create()["url"].rsplit("/", 1)[-1]
        _, body, headers = self.open_public(token)
        csp = headers["Content-Security-Policy"]

        self.assertIn("default-src 'none'", csp)
        # The app's two families (theme.tsx FONT) come from Google Fonts, and
        # the stylesheet and the font files are two DIFFERENT hosts.
        self.assertIn("https://fonts.googleapis.com", csp)
        self.assertIn("https://fonts.gstatic.com", csp)
        self.assertIn("style-src", csp)
        self.assertIn("font-src", csp)
        # The tab switcher is inline.
        self.assertIn("script-src 'unsafe-inline'", csp)
        # The <audio> element follows the gateway's 302 to S3.
        self.assertIn("media-src https:", csp)
        # And nothing has been opened up wholesale.
        self.assertNotIn("'unsafe-eval'", csp)
        self.assertNotIn("script-src *", csp)
        self.assertNotIn("default-src *", csp)

        # Every host the page actually references must be permitted.
        for host in ("https://fonts.googleapis.com", "https://fonts.gstatic.com"):
            with self.subTest(host=host):
                if host in body:
                    self.assertIn(host, csp)

    def test_page_is_mobile_responsive(self):
        token = self.create()["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)
        self.assertIn("width=device-width", body)
        self.assertIn("@media (max-width:560px)", body)

    def test_page_uses_the_app_design_system(self):
        """The shared page must LOOK like MinuteX, not like a generic
        document. These are the tokens and structures a redesign would break:
        theme.tsx's exact palette, its two font families, the Card, the
        SectionRule heading and the underline SegmentedTabs."""
        token = self.create()["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        # theme.tsx LIGHT + DARK scales, verbatim.
        for token_value in ("#F6F7FB", "#3E6BFF", "#12131A",   # LIGHT
                            "#0B0D14", "#5C86FF", "#F3F4F8"):  # DARK
            with self.subTest(colour=token_value):
                self.assertIn(token_value, body)
        # theme.tsx FONT — the app's families, not a system stack alone.
        self.assertIn("Plus Jakarta Sans", body)
        self.assertIn("JetBrains Mono", body)
        # ui.tsx Card: R.card == 16, hairline border.
        self.assertIn("border-radius:16px", body)
        self.assertIn('class="card"', body)
        # ui.tsx SectionRule: heading ABOVE the card, not inside it.
        self.assertIn('class="rule"', body)
        # Respects the viewer's theme the way the app respects Appearance.
        self.assertIn("prefers-color-scheme: dark", body)

    def test_tabs_appear_only_with_more_than_one_view(self):
        """Mirrors the app's own `showTabs`: one view needs no tab bar."""
        notes_only = self.create()["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(notes_only)
        self.assertNotIn('class="tabs"', body)

        with_transcript = self.create({"transcript": True})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(with_transcript)
        self.assertIn('class="tabs"', body)
        self.assertIn(">Overview<", body)
        self.assertIn(">Transcript<", body)

    def test_unknown_token_returns_404(self):
        status, body, _ = self.open_public("a" * 43)
        self.assertEqual(status, 404)
        self.assertIn("isn&#x27;t available", body)

    def test_malformed_token_costs_no_database_read(self):
        """Cheapest check first: junk must not buy a DynamoDB query."""
        for junk in ("", "short", "has/slash", "has spaces", "x" * 500):
            with self.subTest(junk=junk):
                status, _, _ = self.open_public(junk)
                self.assertEqual(status, 404)
        self.assertEqual(self.shares.calls["query"], 0)

    def test_deleted_recording_is_not_served(self):
        token = self.create()["url"].rsplit("/", 1)[-1]
        del self.recordings.items[(KEY,)]
        status, _, _ = self.open_public(token)
        self.assertEqual(status, 404)

    def test_trashed_recording_is_not_served(self):
        """Moving a meeting to the Trash must take its live links with it."""
        token = self.create()["url"].rsplit("/", 1)[-1]
        self.item["deleted_at"] = iso(datetime.now(timezone.utc))
        self.item["status"] = "trashed"
        with mock.patch.object(api, "_is_trashed", return_value=True):
            status, _, _ = self.open_public(token)
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# 5 + 6. Expiry and revocation — the only two ways to take a link back.
# ---------------------------------------------------------------------------
class LifecycleTests(ShareTestCase):

    def test_expired_token_returns_410(self):
        data = self.create()
        token = data["url"].rsplit("/", 1)[-1]
        # Backdate past the expiry rather than sleeping.
        self.stored(data["share_id"])["expires_at"] = \
            iso(datetime.now(timezone.utc) - timedelta(minutes=1))

        status, body, _ = self.open_public(token)
        self.assertEqual(status, 410)
        self.assertIn("expired", body.lower())
        self.assertNotIn(RECORDING["summary"], body)

    def test_unparseable_expiry_is_treated_as_expired(self):
        """A corrupted timestamp must CLOSE the link, not open it forever."""
        data = self.create()
        token = data["url"].rsplit("/", 1)[-1]
        self.stored(data["share_id"])["expires_at"] = "not-a-timestamp"
        status, _, _ = self.open_public(token)
        self.assertEqual(status, 410)

    def test_revoked_token_cannot_be_accessed(self):
        data = self.create()
        token = data["url"].rsplit("/", 1)[-1]

        resp = call(api.revoke_share, owner_event(
            "DELETE", "/shares/{share_id}", key=None,
            share_id=data["share_id"]))
        status, revoked = parse(resp)
        self.assertEqual(status, 200)
        self.assertTrue(revoked["revoked"])

        status, body, _ = self.open_public(token)
        self.assertEqual(status, 404)
        self.assertNotIn(RECORDING["summary"], body)

    def test_revoked_and_unknown_render_the_same_page(self):
        """A dead link must not confirm that a share ever existed."""
        data = self.create()
        token = data["url"].rsplit("/", 1)[-1]
        call(api.revoke_share, owner_event(
            "DELETE", "/shares/{share_id}", key=None,
            share_id=data["share_id"]))

        _, revoked_body, _ = self.open_public(token)
        _, unknown_body, _ = self.open_public("z" * 43)
        self.assertEqual(revoked_body, unknown_body)

    def test_revoke_is_idempotent(self):
        share_id = self.create()["share_id"]
        ev = owner_event("DELETE", "/shares/{share_id}", key=None,
                         share_id=share_id)
        first = parse(call(api.revoke_share, ev))[1]
        second = parse(call(api.revoke_share, ev))[1]
        self.assertEqual(first["revoked_at"], second["revoked_at"])

    def test_revoked_share_is_kept_for_the_audit_trail(self):
        share_id = self.create()["share_id"]
        call(api.revoke_share, owner_event(
            "DELETE", "/shares/{share_id}", key=None, share_id=share_id))
        row = self.stored(share_id)
        self.assertTrue(row["revoked_at"])
        self.assertFalse(share_schema.is_active(row))

    def test_patch_updates_toggles_and_expiry(self):
        share_id = self.create()["share_id"]
        resp = call(api.update_share, owner_event(
            "PATCH", "/shares/{share_id}",
            body={"transcript": True, "expires_at": 3},
            key=None, share_id=share_id))
        status, data = parse(resp)
        self.assertEqual(status, 200)
        self.assertTrue(data["share"]["transcript_enabled"])
        self.assertIsNotNone(data["share"]["expires_at"])
        # An absent toggle keeps its stored value rather than snapping back
        # to the default — PATCHing one must not silently change another.
        self.assertTrue(data["share"]["summary_enabled"])

    def test_patch_can_narrow_a_live_share(self):
        """Turning a toggle OFF must take the content off the live page."""
        data = self.create({"transcript": True})
        token = data["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)
        self.assertIn("Transcript", body)

        call(api.update_share, owner_event(
            "PATCH", "/shares/{share_id}", body={"transcript": False},
            key=None, share_id=data["share_id"]))
        _, body, _ = self.open_public(token)
        self.assertNotIn(RECORDING["transcript"][:60], body)

    def test_listing_shows_shares_without_urls(self):
        """The tokens are unrecoverable by design, so the list cannot offer a
        link — only a revoke control."""
        self.create()
        resp = call(api.list_shares,
                    owner_event("GET", "/recordings/shares/{key+}"))
        status, data = parse(resp)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["shares"]), 1)
        self.assertNotIn("url", data["shares"][0])
        self.assertNotIn("token_hash", data["shares"][0])


# ---------------------------------------------------------------------------
# 7 + 8 + 9. Toggle enforcement — server-side, not CSS.
# ---------------------------------------------------------------------------
class ToggleEnforcementTests(ShareTestCase):

    def test_disabled_transcript_is_not_exposed(self):
        """Default config. The transcript must be ABSENT from the HTML, not
        merely hidden — anyone can View Source."""
        data = self.create()
        token = data["url"].rsplit("/", 1)[-1]
        status, body, _ = self.open_public(token)

        self.assertEqual(status, 200)
        self.assertFalse(data["share"]["transcript_enabled"])
        self.assertNotIn(RECORDING["transcript"][:80], body)
        self.assertNotIn("<h2>Transcript</h2>", body)

    def test_enabled_transcript_is_rendered(self):
        token = self.create({"transcript": True})["url"].rsplit("/", 1)[-1]
        status, body, _ = self.open_public(token)
        self.assertEqual(status, 200)
        self.assertIn("Transcript", body)

    def test_disabled_audio_is_not_exposed(self):
        """No <audio> element AND no presign: minting a URL "just in case"
        would hand out audio the owner never shared."""
        token = self.create()["url"].rsplit("/", 1)[-1]
        status, body, _ = self.open_public(token)

        self.assertEqual(status, 200)
        self.assertNotIn("<audio", body)
        self.assertNotIn("X-Amz-Signature", body)
        self.s3.generate_presigned_url.assert_not_called()

    def test_enabled_audio_points_at_the_gateway_not_s3(self):
        """The PAGE must never carry a presign. It carries the gateway route,
        which is inert without a live share behind it."""
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        status, body, _ = self.open_public(token)

        self.assertEqual(status, 200)
        self.assertIn("<audio", body)
        self.assertIn(f"/share/{token}/audio", body)
        # No S3 host, no signature, and no presign minted just to render.
        self.assertNotIn("X-Amz-Signature", body)
        self.assertNotIn("s3.example.com", body)
        self.s3.generate_presigned_url.assert_not_called()

    def test_page_asks_for_metadata_so_seeking_works(self):
        """preload="none" leaves the control with an unknown duration and no
        working scrubber until playback has already begun."""
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)
        self.assertIn('preload="metadata"', body)
        self.assertNotIn('preload="none"', body)

    def test_s3_object_permissions_are_never_changed(self):
        """Presigning only. Nothing in this feature may make the bucket or an
        object public."""
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        self.open_public(token)
        for forbidden in ("put_object_acl", "put_bucket_acl",
                          "put_bucket_policy", "put_object"):
            self.assertFalse(getattr(self.s3, forbidden).called,
                             f"{forbidden} must never be called")

    def test_disabling_notes_removes_those_sections(self):
        token = self.create({
            "summary": False, "highlights": False, "decisions": False,
            "tasks": False, "participants": False,
        })["url"].rsplit("/", 1)[-1]
        status, body, _ = self.open_public(token)

        self.assertEqual(status, 200)
        self.assertNotIn(RECORDING["summary"], body)
        self.assertNotIn("<h2>Decisions</h2>", body)
        self.assertNotIn("<h2>Action Items</h2>", body)
        self.assertNotIn("<h2>Attendees</h2>", body)
        # Still a valid page — it just says there is nothing to show.
        self.assertIn("has not shared any content", body)

    def test_a_new_mom_section_is_private_until_mapped(self):
        """Deny-by-default: a section whose role no toggle governs must not
        be published, so growing the MoM cannot leak content."""
        share = dict(share_schema.default_config())
        mom = {"sections": [{
            "kind": "text", "title": "Confidential Notes",
            "role": "some_future_role", "visible": True,
            "text": "internal only",
        }]}
        self.assertEqual(share_schema.public_sections(mom, share), [])

    def test_owner_hidden_sections_stay_hidden(self):
        """visible=false in the MoM editor must hold on the public page too."""
        share = dict(share_schema.default_config())
        mom = {"sections": [{
            "kind": "text", "title": "Summary", "role": "summary",
            "visible": False, "text": "hidden by the owner",
        }]}
        self.assertEqual(share_schema.public_sections(mom, share), [])


# ---------------------------------------------------------------------------
# The app's own content, rendered the app's way.
#
# The shared page must be the MinuteX meeting screen, read-only on the web —
# so it follows the SAME content cascade index.tsx follows (overview ->
# MoM -> legacy) and reproduces the transcript's speaker blocks rather than
# dumping a wall of text.
# ---------------------------------------------------------------------------
class AppParityTests(ShareTestCase):

    OVERVIEW = {"sections": [
        {"id": "section_0", "title": "Panel Feedback", "kind": "text",
         "content": "The panel felt the fittings spend was unjustified.",
         "items": [], "source": "ai", "evidence_segment_ids": []},
        {"id": "section_1", "title": "Decisions", "kind": "list",
         "content": "", "items": ["Drop imported fittings"],
         "source": "ai", "evidence_segment_ids": []},
    ]}

    def test_overview_is_preferred_over_the_mom(self):
        """index.tsx renders rec.overview when present; so must the share."""
        self.item["overview"] = json.loads(json.dumps(self.OVERVIEW))
        token = self.create()["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        # The model's own title, which no fixed template would produce.
        self.assertIn("Panel Feedback", body)
        self.assertIn("fittings spend was unjustified", body)
        # And NOT the MoM's parallel headings, or the meeting would render
        # twice under two sets of titles.
        self.assertNotIn("<h2>Meeting Points</h2>", body)

    def test_overview_sections_respect_the_share_toggles(self):
        """An AI section is still governed by a toggle — turning Decisions
        off must drop the model's Decisions section too."""
        self.item["overview"] = json.loads(json.dumps(self.OVERVIEW))
        token = self.create({"decisions": False})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        self.assertIn("Panel Feedback", body)
        self.assertNotIn("Drop imported fittings", body)

    def test_recognisable_ai_titles_map_to_their_toggle(self):
        """The classifier reads the model's own wording. "Panel Feedback" is
        a highlight, "Next Steps" are tasks — so each is governed by the
        toggle a user would expect, not by a catch-all."""
        for title, toggle in (
            ("Panel Feedback", "highlights_enabled"),
            ("Key Takeaways", "highlights_enabled"),
            ("Decisions Made", "decisions_enabled"),
            ("Next Steps", "tasks_enabled"),
            ("Who Attended", "participants_enabled"),
        ):
            with self.subTest(title=title):
                self.assertEqual(share_schema._toggle_for_title(title), toggle)

    def test_unrecognised_ai_title_follows_the_summary_toggle(self):
        """Deny-leaning: a title this code cannot classify falls to the AI
        notes toggle, so turning Summary off HIDES it rather than leaking it.

        "Budget Context" is deliberately chosen to match no keyword — the
        model invents titles per meeting and most will not be classifiable.
        """
        self.item["overview"] = {"sections": [{
            "id": "section_0", "title": "Budget Context", "kind": "text",
            "content": "Unclassifiable prose the model chose to write.",
            "items": [], "source": "ai", "evidence_segment_ids": [],
        }]}
        self.assertEqual(
            share_schema._toggle_for_title("Budget Context"), "summary_enabled")

        token = self.create({"summary": False})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)
        self.assertNotIn("Budget Context", body)
        self.assertNotIn("Unclassifiable prose", body)

    def test_falls_back_to_mom_when_no_overview(self):
        self.item.pop("overview", None)
        token = self.create()["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)
        self.assertIn("Summary", body)
        self.assertIn(RECORDING["summary"], body)

    def test_transcript_never_renders_below_the_overview(self):
        """REGRESSION. The transcript panel shipped as class="panel hidden"
        while the only CSS rule was `.panel[hidden]` — an attribute selector.
        A CLASS named "hidden" matched nothing, so on first load the whole
        transcript sat stacked underneath the Overview content, and it only
        started behaving once a tab click set the real `hidden` property.

        These assert the ATTRIBUTE form, which is what both the CSS and the
        tab script actually key on.
        """
        self.item["overview"] = json.loads(json.dumps(self.OVERVIEW))
        self.item["timestamps"] = [
            {"speaker": "0", "start": 0, "end": 5, "text": "SECRET_LINE"},
        ]
        token = self.create({"transcript": True})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        # Tabs exist, so the transcript panel must start hidden.
        self.assertIn('class="tabs"', body)
        self.assertIn('id="p-transcript" hidden>', body)
        # The broken class form must never come back.
        self.assertNotIn('class="panel hidden"', body)
        # The Overview panel is the visible one.
        self.assertIn('<div class="panel" id="p-overview">', body)
        # And the CSS actually hides it.
        self.assertIn(".panel[hidden]", body)

    def test_transcript_stays_visible_when_it_is_the_only_panel(self):
        """The mirror case: with nothing else shared there is no tab bar, so
        hiding the transcript would render a page with no content at all."""
        self.item.pop("overview", None)
        self.item["timestamps"] = [
            {"speaker": "0", "start": 0, "end": 5, "text": "ONLY_LINE"},
        ]
        token = self.create({
            "summary": False, "highlights": False, "decisions": False,
            "tasks": False, "participants": False, "transcript": True,
        })["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        self.assertNotIn('class="tabs"', body)
        self.assertIn('<div class="panel" id="p-transcript">', body)
        self.assertNotIn('id="p-transcript" hidden', body)
        self.assertIn("ONLY_LINE", body)

    def test_transcript_heading_is_not_duplicated_under_its_tab(self):
        """A "Transcript" heading directly beneath a "Transcript" tab reads as
        a rendering mistake. The app's own transcript screen has no such
        heading either — but with NO tab bar the section still needs a label."""
        self.item["overview"] = json.loads(json.dumps(self.OVERVIEW))
        self.item["timestamps"] = [
            {"speaker": "0", "start": 0, "end": 5, "text": "A line."},
        ]
        tabbed = self.create({"transcript": True})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(tabbed)
        self.assertIn(">Transcript<", body)            # the tab
        self.assertNotIn("<h2>Transcript</h2>", body)  # not also a heading

        self.item.pop("overview", None)
        untabbed = self.create({
            "summary": False, "highlights": False, "decisions": False,
            "tasks": False, "participants": False, "transcript": True,
        })["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(untabbed)
        self.assertIn("<h2>Transcript</h2>", body)

    def test_every_panel_is_addressable_by_the_tab_script(self):
        """Each tab's data-panel must name a panel that exists, or clicking it
        hides everything and the page goes blank."""
        self.item["overview"] = json.loads(json.dumps(self.OVERVIEW))
        self.item["timestamps"] = [
            {"speaker": "0", "start": 0, "end": 5, "text": "A line."},
        ]
        token = self.create({"transcript": True})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        wanted = set(re.findall(r'data-panel="([a-z]+)"', body))
        present = set(re.findall(r'id="p-([a-z]+)"', body))
        self.assertTrue(wanted)
        self.assertEqual(wanted, present)

    def test_transcript_renders_as_speaker_blocks(self):
        """transcript-view.tsx's shape: a tabular timecode gutter, a coloured
        speaker dot and the resolved NAME — not the raw diarization label."""
        self.item["timestamps"] = [
            {"speaker": "0", "start": 0, "end": 5, "text": "Quote is 4.2 lakh."},
            {"speaker": "0", "start": 5, "end": 9, "text": "Over budget."},
            {"speaker": "1", "start": 9, "end": 14, "text": "Agreed, revise it."},
        ]
        token = self.create({"transcript": True})["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        self.assertIn('class="sblock"', body)
        self.assertIn('class="stime"', body)
        # speaker_names maps 0->Ravi, 1->Priya (sources.ts speakerName).
        self.assertIn("Ravi", body)
        self.assertIn("Priya", body)
        # Consecutive same-speaker segments FOLD into one block.
        self.assertIn("Quote is 4.2 lakh. Over budget.", body)
        # Distinct speakers get distinct colour classes.
        self.assertIn("sname c0", body)
        self.assertIn("sname c1", body)

    def test_speaker_blocks_are_absent_when_transcript_is_off(self):
        self.item["timestamps"] = [
            {"speaker": "0", "start": 0, "end": 5, "text": "Secret line."},
        ]
        token = self.create()["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)
        self.assertNotIn("Secret line.", body)
        self.assertNotIn('class="sblock"', body)

    def test_raw_diarization_label_never_reaches_the_page(self):
        """speaker_raw is a grouping internal; only the display name ships."""
        self.item["timestamps"] = [
            {"speaker": "0", "start": 0, "end": 5, "text": "Hello."},
        ]
        blocks = share_schema.public_speaker_blocks(
            self.item, {"transcript_enabled": True})
        self.assertTrue(blocks)
        for b in blocks:
            self.assertNotIn("speaker_raw", b)


# ---------------------------------------------------------------------------
# The audio gateway — /share/{token}/audio.
#
# WHY THIS EXISTS AT ALL. S3 checks a presigned URL's expiry at REQUEST time,
# not continuously. An <audio> element issues a NEW ranged request on every
# seek, so a single flat presign does not buy "N minutes of listening" — it
# buys "the first seek after N minutes fails". On an hour-long meeting that
# is a guaranteed break. Re-signing per request removes the class of bug.
# ---------------------------------------------------------------------------
class AudioGatewayTests(ShareTestCase):

    def audio_request(self, token):
        resp = api.public_share_audio(public_event(token))
        return resp["statusCode"], resp["headers"], resp.get("body", "")

    def test_gateway_redirects_to_a_fresh_presign(self):
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        status, headers, _ = self.audio_request(token)

        self.assertEqual(status, 302)
        self.assertIn("X-Amz-Signature", headers["Location"])
        self.s3.generate_presigned_url.assert_called_once()
        kwargs = self.s3.generate_presigned_url.call_args.kwargs
        self.assertEqual(kwargs["Params"]["Key"], KEY)
        # Short, because the next seek comes back through here.
        self.assertEqual(kwargs["ExpiresIn"],
                         share_schema.SHARE_AUDIO_URL_EXPIRY)

    def test_every_request_is_signed_afresh(self):
        """This is what makes seeking work on a long recording: request 50,
        made an hour in, is signed then — not at page load."""
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        for _ in range(5):
            self.assertEqual(self.audio_request(token)[0], 302)
        self.assertEqual(self.s3.generate_presigned_url.call_count, 5)

    def test_redirect_is_never_cached(self):
        """A cached redirect would pin one expiring presign in front of every
        later seek — reintroducing the bug the gateway removes."""
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        status, headers, _ = self.audio_request(token)
        self.assertEqual(status, 302)   # 302, never a cacheable 301
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_revoking_stops_playback_already_under_way(self):
        """The headline reason the gateway exists: a revoke must reach a
        viewer who already has the page open and is mid-listen."""
        data = self.create({"audio": True})
        token = data["url"].rsplit("/", 1)[-1]
        self.assertEqual(self.audio_request(token)[0], 302)

        call(api.revoke_share, owner_event(
            "DELETE", "/shares/{share_id}", key=None,
            share_id=data["share_id"]))

        status, _, body = self.audio_request(token)
        self.assertEqual(status, 404)
        self.assertNotIn("X-Amz-Signature", body)

    def test_disabling_audio_stops_playback_already_under_way(self):
        """Same, for narrowing rather than revoking: the toggle is re-read on
        every request, not trusted from page-render time."""
        data = self.create({"audio": True})
        token = data["url"].rsplit("/", 1)[-1]
        self.assertEqual(self.audio_request(token)[0], 302)

        call(api.update_share, owner_event(
            "PATCH", "/shares/{share_id}", body={"audio": False},
            key=None, share_id=data["share_id"]))

        self.assertEqual(self.audio_request(token)[0], 404)

    def test_gateway_refuses_when_audio_was_never_shared(self):
        """Guessing the /audio path on a notes-only share must not work."""
        token = self.create()["url"].rsplit("/", 1)[-1]
        status, _, _ = self.audio_request(token)
        self.assertEqual(status, 404)
        self.s3.generate_presigned_url.assert_not_called()

    def test_gateway_honours_expiry_and_bad_tokens(self):
        data = self.create({"audio": True})
        token = data["url"].rsplit("/", 1)[-1]
        self.stored(data["share_id"])["expires_at"] = \
            iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        self.assertEqual(self.audio_request(token)[0], 410)

        self.assertEqual(self.audio_request("q" * 43)[0], 404)
        self.assertEqual(self.audio_request("nope")[0], 404)

    def test_gateway_needs_no_jwt(self):
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        with mock.patch.object(api, "_require_auth",
                               side_effect=AssertionError(
                                   "audio gateway must not require auth")):
            self.assertEqual(self.audio_request(token)[0], 302)

    def test_gateway_leaks_no_private_metadata(self):
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        _, headers, body = self.audio_request(token)
        blob = json.dumps(headers) + body
        self.assertNotIn(token, blob)
        self.assertNotIn(OWNER, blob)

    def test_lambda_never_proxies_the_audio_bytes(self):
        """A 302 keeps the bytes on the S3 <-> browser path. Proxying them
        would blow API Gateway's 29s timeout and 10MB response cap on any
        real meeting, and would break Range/206 entirely."""
        token = self.create({"audio": True})["url"].rsplit("/", 1)[-1]
        _, _, body = self.audio_request(token)
        self.assertEqual(body, "")
        self.s3.get_object.assert_not_called()


class AudioExpiryScalingTests(unittest.TestCase):
    """The FALLBACK path's expiry: scaled to the recording, not the clock.

    Used only when the gateway address cannot be resolved. A flat window here
    is what broke seeking on long meetings in the first place.
    """

    def test_short_recording_still_gets_the_floor(self):
        # An hour floor covers "open the link now, press play after lunch".
        self.assertEqual(share_schema.audio_url_expiry(300), 3600)
        self.assertEqual(share_schema.audio_url_expiry(0), 3600)
        self.assertEqual(share_schema.audio_url_expiry(None), 3600)

    def test_long_recording_scales_past_its_own_duration(self):
        # 60-minute meeting -> 3h, comfortably outliving pauses and scrubbing.
        self.assertEqual(share_schema.audio_url_expiry(3600), 10800)
        for duration in (1800, 3600, 7200):
            with self.subTest(duration=duration):
                self.assertGreater(share_schema.audio_url_expiry(duration),
                                   duration)

    def test_expiry_is_capped(self):
        self.assertEqual(share_schema.audio_url_expiry(999999),
                         share_schema.SHARE_AUDIO_MAX_EXPIRY)

    def test_junk_duration_does_not_raise(self):
        for junk in ("abc", {}, [], "12"):
            with self.subTest(junk=junk):
                self.assertGreaterEqual(share_schema.audio_url_expiry(junk),
                                        share_schema.SHARE_AUDIO_MIN_EXPIRY)


# ---------------------------------------------------------------------------
# 11. No private owner metadata may reach the public page.
# ---------------------------------------------------------------------------
class LeakageTests(ShareTestCase):

    def test_public_page_exposes_no_private_metadata(self):
        """The row is deliberately stuffed with everything private the schema
        carries. None of it may appear in the page.

        This is the test that justifies public_payload() ASSEMBLING its output
        rather than filtering the recording row: a deny-list would pass today
        and fail silently the day a new attribute is added.
        """
        # user_id stays OWNER — the ownership check is what lets the share be
        # created at all. The private OWNER identity is asserted separately
        # below via owner_id on the share row.
        self.item.update({
            "device_id": "esp32-secret-001",
            "owner_user_id": "u-1-secret-owner",
            "folder_id": "fld-secret-9",
            "stt_request_id": "req-secret-abc",
            "crm_records": [{"object": "Opportunity", "id": "006SECRET"}],
            "chat_history": [{"role": "user", "content": "secret question"}],
            "deleted_task_fingerprints": ["fp-secret"],
            "transcript_s3_key": "transcripts/u-1/secret.json",
        })
        token = self.create()["url"].rsplit("/", 1)[-1]
        status, body, _ = self.open_public(token)
        self.assertEqual(status, 200)

        for secret in ("u-1-secret-owner", "esp32-secret-001", "fld-secret-9",
                       "req-secret-abc", "006SECRET", "secret question",
                       "fp-secret", "transcripts/u-1/secret.json",
                       # The S3 key is an internal identifier AND, for the
                       # audio path, part of a credential.
                       KEY):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, body)

    def test_public_page_does_not_echo_the_token(self):
        """Nothing on the page should re-print the credential that opened it —
        a screenshot of the page must not be a working link."""
        data = self.create()
        token = data["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)
        self.assertNotIn(token, body)
        self.assertNotIn(data["share_id"], body)

    def test_meeting_content_is_html_escaped(self):
        """Meeting text is user- and AI-authored and has never been sanitised.
        Treating it as trusted would aim an XSS at people who are not even
        MinuteX users."""
        self.item["title"] = "<script>alert('xss')</script>"
        self.item["summary"] = "Bad <img src=x onerror=alert(1)> input"
        token = self.create()["url"].rsplit("/", 1)[-1]
        _, body, _ = self.open_public(token)

        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;script&gt;", body)

    def test_redact_token_never_returns_the_token(self):
        """Raw share tokens must never reach CloudWatch."""
        token = share_schema.new_token()
        redacted = share_schema.redact_token(token)
        self.assertNotIn(token, redacted)
        self.assertTrue(redacted.startswith("sha256:"))


# ---------------------------------------------------------------------------
# 10. The existing authenticated surface is untouched.
# ---------------------------------------------------------------------------
class BackwardCompatibilityTests(ShareTestCase):

    def test_get_recording_still_requires_auth_and_ownership(self):
        """Sharing must confer no authenticated access, and must not relax the
        ownership rule that every recording route depends on."""
        ev = {
            "routeKey": "GET /recordings/{key+}",
            "requestContext": {"http": {"method": "GET", "path": "/"}},
            "pathParameters": {"key": KEY},
            "headers": {"authorization": "Bearer test-token"},
        }
        with mock.patch.object(api, "transcript_store") as ts:
            ts.hydrate.side_effect = lambda _s3, _b, it: it
            status, data = parse(call(api.get_recording, ev))
        self.assertEqual(status, 200)
        self.assertEqual(data["recording"]["audio_s3_key"], KEY)

        # A stranger still gets 404, share or no share.
        self.create()
        with mock.patch.object(api, "_require_auth", return_value=STRANGER):
            status, data = parse(call(api.get_recording, ev))
        self.assertEqual(status, 404)

    def test_owner_audio_url_expiry_is_unchanged(self):
        """The share's 15-minute presign must not have shortened the owner's
        own hour-long one."""
        self.assertEqual(api.AUDIO_URL_EXPIRY, 3600)
        self.assertNotEqual(share_schema.SHARE_AUDIO_URL_EXPIRY,
                            api.AUDIO_URL_EXPIRY)

    def test_share_routes_are_registered(self):
        for method, path in (
            ("POST", "/recordings/share/{key+}"),
            ("GET", "/recordings/shares/{key+}"),
            ("PATCH", "/shares/{share_id}"),
            ("DELETE", "/shares/{share_id}"),
            ("GET", "/share/{token}"),
        ):
            with self.subTest(route=f"{method} {path}"):
                self.assertIn((method, path), api._ROUTES)

    def test_public_route_is_the_only_unauthenticated_recording_read(self):
        """Every OTHER route that reads a recording must still call
        _require_auth. This is the assertion that would catch someone
        "helpfully" widening a handler later."""
        with mock.patch.object(api, "_require_auth",
                               side_effect=AssertionError("auth required")):
            for handler, ev in (
                (api.get_recording, {
                    "routeKey": "GET /recordings/{key+}",
                    "requestContext": {"http": {"method": "GET", "path": "/"}},
                    "pathParameters": {"key": KEY}, "headers": {}}),
                (api.create_share,
                 owner_event("POST", "/recordings/share/{key+}", body={})),
                (api.list_shares,
                 owner_event("GET", "/recordings/shares/{key+}")),
            ):
                with self.subTest(handler=handler.__name__):
                    with self.assertRaises(AssertionError):
                        handler(ev)


# ---------------------------------------------------------------------------
# The value model, in isolation.
# ---------------------------------------------------------------------------
class SchemaTests(unittest.TestCase):

    def test_tokens_are_high_entropy_and_unique(self):
        tokens = {share_schema.new_token() for _ in range(500)}
        self.assertEqual(len(tokens), 500)
        for t in list(tokens)[:20]:
            self.assertGreaterEqual(len(t), 40)
            self.assertTrue(share_schema.token_looks_valid(t))

    def test_hash_is_stable_and_one_way(self):
        token = share_schema.new_token()
        self.assertEqual(share_schema.hash_token(token),
                         share_schema.hash_token(token))
        self.assertNotIn(token, share_schema.hash_token(token))
        self.assertNotEqual(share_schema.hash_token(token),
                            share_schema.hash_token(share_schema.new_token()))

    def test_default_config_withholds_transcript_and_audio(self):
        cfg = share_schema.default_config()
        self.assertFalse(cfg["transcript_enabled"])
        self.assertFalse(cfg["audio_enabled"])
        self.assertTrue(cfg["summary_enabled"])

    def test_coerce_config_ignores_junk(self):
        """A malformed body must fall back to the DEFAULT, never to True."""
        cfg = share_schema.coerce_config({"transcript": "yes", "audio": 1,
                                          "nonsense": True})
        self.assertFalse(cfg["transcript_enabled"])
        self.assertFalse(cfg["audio_enabled"])
        self.assertNotIn("nonsense", cfg)

    def test_coerce_config_preserves_unmentioned_toggles_on_patch(self):
        base = dict(share_schema.default_config())
        base["transcript_enabled"] = True
        cfg = share_schema.coerce_config({"summary": False}, base=base)
        self.assertFalse(cfg["summary_enabled"])
        self.assertTrue(cfg["transcript_enabled"])

    def test_owner_view_never_includes_the_token_hash(self):
        view = share_schema.owner_view({
            "share_id": "shr_1", "token_hash": "deadbeef",
            "recording_key": KEY, "owner_id": OWNER,
        })
        self.assertNotIn("token_hash", view)
        self.assertNotIn("owner_id", view)

    def test_expiry_boundaries(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(share_schema.is_expired({"expires_at": ""}))
        self.assertFalse(share_schema.is_expired({}))
        self.assertFalse(share_schema.is_expired(
            {"expires_at": iso(now + timedelta(hours=1))}))
        self.assertTrue(share_schema.is_expired(
            {"expires_at": iso(now - timedelta(seconds=1))}))

    def test_public_payload_has_a_fixed_key_set(self):
        """The payload is assembled, so its shape is knowable and pinned. A
        new key appearing here should be a deliberate edit to this test."""
        payload = share_schema.public_payload(
            {"title": "T", "user_id": "secret", "transcript": "x"},
            {"sections": []}, share_schema.default_config())
        self.assertEqual(
            set(payload),
            {"title", "recorded_at", "duration", "language", "overview",
             "sections", "transcript", "speaker_blocks", "audio_url",
             "expires_at"})
        self.assertIsNone(payload["transcript"])
        self.assertIsNone(payload["audio_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
