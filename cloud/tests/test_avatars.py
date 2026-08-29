#!/usr/bin/env python3
# =============================================================
# test_avatars.py — profile photos and contact photos.
#
# OFFLINE like the rest of tests/: DynamoDB is tests/fake_dynamodb.py and S3 is
# a stub whose presigner returns a recognisable string. Both come from
# test_workspace_org.OrgTestCase, which already wires the tables, the
# resource-level handle (for the batched Users read) and auth.
#
# WHAT IS ACTUALLY AT RISK HERE, and therefore what these tests are about:
#
#   1. CROSS-TENANT READ. A contact's photo may come from ANOTHER user's
#      profile — that is the feature ("if it is a MinuteX user, its MinuteX
#      profile image will be seen"). The read must be narrow (photo only) and
#      must never be reachable for a user the owner has not already matched.
#   2. CLIENT-SUPPLIED S3 KEYS. avatar_url arrives in a request body and the
#      server presigns reads of it. An unchecked key would let one account have
#      the API sign reads of another account's image.
#   3. PRECEDENCE. Own photo beats linked-profile photo beats initials, and the
#      app is told WHICH it got, because "the photo you saved" and "their
#      profile photo" are not interchangeable to a user.
#   4. N+1. A page of contacts must cost ONE Users read, not one per contact.
#
# Run:  python tests/test_avatars.py
# =============================================================
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_workspace_org import (  # noqa: E402
    OTHER, USER, OrgTestCase, api, call, event, parse,
)


def upload_event(body):
    return event("POST", "/avatars/upload-request", body=body)


class TestAvatarUploadRequest(OrgTestCase):
    """POST /avatars/upload-request — presign only, nothing written."""

    def test_presigns_a_user_scoped_key(self):
        code, body = parse(call(api.request_avatar_upload,
                                upload_event({"format": "jpg"})))
        self.assertEqual(code, 200)
        # The owner segment comes first — that is what makes ownership
        # decidable from the key alone.
        self.assertTrue(body["key"].startswith(f"avatars/{USER}/user/"))
        self.assertTrue(body["key"].endswith(".jpg"))
        self.assertEqual(body["content_type"], "image/jpeg")
        self.assertIn("upload_url", body)

    def test_writes_nothing_to_the_user_row(self):
        """The key is stored by the follow-up PATCH, never by the presign.

        Otherwise an abandoned upload leaves a profile pointing at bytes that
        never arrived.
        """
        self.t["users"].put_item(Item={"user_id": USER, "email": "a@b.c"})
        parse(call(api.request_avatar_upload, upload_event({"format": "png"})))
        row = self.t["users"].get_item(Key={"user_id": USER})["Item"]
        self.assertEqual(row.get("avatar_url", ""), "")

    def test_every_upload_is_a_new_object(self):
        a = parse(call(api.request_avatar_upload,
                       upload_event({"format": "jpg"})))[1]["key"]
        b = parse(call(api.request_avatar_upload,
                       upload_event({"format": "jpg"})))[1]["key"]
        self.assertNotEqual(a, b)

    def test_rejects_unsupported_format(self):
        code, body = parse(call(api.request_avatar_upload,
                                upload_event({"format": "gif"})))
        self.assertEqual(code, 400)
        self.assertIn("unsupported image format", body["error"])

    def test_rejects_wav_so_an_image_cannot_enter_the_audio_pipeline(self):
        """.wav is the S3 trigger's filter — an avatar must never match it."""
        self.assertNotIn("wav", api.AVATAR_FORMATS)
        code, _ = parse(call(api.request_avatar_upload,
                             upload_event({"format": "wav"})))
        self.assertEqual(code, 400)

    def test_rejects_oversized_image(self):
        code, body = parse(call(api.request_avatar_upload,
                                upload_event({"format": "jpg",
                                              "size": api.MAX_AVATAR_BYTES + 1})))
        self.assertEqual(code, 413)

    def test_accepts_image_at_the_limit(self):
        code, _ = parse(call(api.request_avatar_upload,
                             upload_event({"format": "jpg",
                                           "size": api.MAX_AVATAR_BYTES})))
        self.assertEqual(code, 200)

    def test_contact_scope_requires_an_owned_contact(self):
        code, _ = parse(call(api.request_avatar_upload,
                             upload_event({"format": "jpg",
                                           "scope": "contact",
                                           "contact_id": "nope"})))
        self.assertEqual(code, 404)

    def test_contact_scope_refuses_another_users_contact(self):
        """A presigned PUT must not be handed out for someone else's contact —
        the follow-up PATCH would be rejected, but the URL would still work."""
        self.t["contacts"].put_item(Item={
            "contact_id": "c-other", "owner_user_id": OTHER, "name": "Theirs",
        })
        code, _ = parse(call(api.request_avatar_upload,
                             upload_event({"format": "jpg",
                                           "scope": "contact",
                                           "contact_id": "c-other"})))
        self.assertEqual(code, 404)

    def test_contact_scope_presigns_for_an_owned_contact(self):
        cid = self.mk_contact()[1]["contact"]["id"]
        code, body = parse(call(api.request_avatar_upload,
                                upload_event({"format": "jpg",
                                              "scope": "contact",
                                              "contact_id": cid})))
        self.assertEqual(code, 200)
        self.assertTrue(body["key"].startswith(f"avatars/{USER}/contact/"))

    def test_contact_scope_without_an_id_is_the_phone_import_case(self):
        """The photo is uploaded BEFORE the contact exists, so there is no id
        to own-check. The key must still be owner-scoped."""
        code, body = parse(call(api.request_avatar_upload,
                                upload_event({"format": "jpg",
                                              "scope": "contact"})))
        self.assertEqual(code, 200)
        self.assertTrue(body["key"].startswith(f"avatars/{USER}/contact/"))
        # And the key it yields is one the caller may later store.
        self.assertTrue(api._owns_avatar_key(USER, body["key"]))

    def test_rejects_unknown_scope(self):
        code, _ = parse(call(api.request_avatar_upload,
                             upload_event({"format": "jpg", "scope": "org"})))
        self.assertEqual(code, 400)


class TestAvatarKeyOwnership(OrgTestCase):
    """_owns_avatar_key — the guard on every client-supplied key."""

    def test_own_prefix_accepted(self):
        self.assertTrue(api._owns_avatar_key(USER, f"avatars/{USER}/user/x.jpg"))

    def test_another_users_prefix_rejected(self):
        self.assertFalse(api._owns_avatar_key(USER,
                                              f"avatars/{OTHER}/user/x.jpg"))

    def test_prefix_confusion_rejected(self):
        """"u-1" must not match "u-11" — the trailing slash is load-bearing."""
        self.assertFalse(api._owns_avatar_key("u-1", "avatars/u-11/user/x.jpg"))

    def test_non_avatar_key_rejected(self):
        self.assertFalse(api._owns_avatar_key(
            USER, f"recordings/{USER}/mobile/x.wav"))

    def test_empty_rejected(self):
        self.assertFalse(api._owns_avatar_key(USER, ""))


class TestProfileAvatar(OrgTestCase):
    """PATCH /me — storing, replacing and clearing the profile photo."""

    def setUp(self):
        super().setUp()
        self.t["users"].put_item(Item={
            "user_id": USER, "email": "me@company.com", "name": "Me",
            "created_at": "2026-08-01T00:00:00Z",
        })

    def patch_me(self, body):
        return parse(call(api.patch_me, event("PATCH", "/me", body=body)))

    def own_key(self, name="a.jpg"):
        return f"avatars/{USER}/user/{name}"

    def test_stores_key_and_returns_a_presigned_view_url(self):
        code, body = self.patch_me({"avatar_url": self.own_key()})
        self.assertEqual(code, 200)
        # The stored value is the KEY; the app renders avatar_view_url.
        self.assertEqual(body["user"]["avatar_url"], self.own_key())
        self.assertIn(self.own_key(), body["user"]["avatar_view_url"])
        self.assertTrue(body["user"]["avatar_view_url"].startswith("https://"))

    def test_get_me_re_signs_on_every_read(self):
        """A stored URL would expire; the key is re-signed per request."""
        self.patch_me({"avatar_url": self.own_key()})
        code, body = parse(call(api.get_me, event("GET", "/me")))
        self.assertEqual(code, 200)
        self.assertIn(self.own_key(), body["user"]["avatar_view_url"])

    def test_no_photo_yields_empty_view_url_not_a_broken_link(self):
        code, body = parse(call(api.get_me, event("GET", "/me")))
        self.assertEqual(body["user"]["avatar_view_url"], "")

    def test_rejects_another_users_key(self):
        code, body = self.patch_me(
            {"avatar_url": f"avatars/{OTHER}/user/theirs.jpg"})
        self.assertEqual(code, 400)
        self.assertIn("upload-request", body["error"])
        row = self.t["users"].get_item(Key={"user_id": USER})["Item"]
        self.assertEqual(row.get("avatar_url", ""), "")

    def test_rejects_a_recording_key(self):
        """Otherwise the API would presign reads of arbitrary bucket objects."""
        code, _ = self.patch_me({"avatar_url": f"recordings/{USER}/mobile/x.wav"})
        self.assertEqual(code, 400)

    def test_clearing_removes_the_photo(self):
        self.patch_me({"avatar_url": self.own_key()})
        code, body = self.patch_me({"avatar_url": ""})
        self.assertEqual(code, 200)
        self.assertEqual(body["user"]["avatar_url"], "")
        self.assertEqual(body["user"]["avatar_view_url"], "")

    def test_replacing_deletes_the_old_object(self):
        self.patch_me({"avatar_url": self.own_key("old.jpg")})
        self.s3.delete_object.reset_mock()
        self.patch_me({"avatar_url": self.own_key("new.jpg")})
        self.s3.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key=self.own_key("old.jpg"))

    def test_clearing_deletes_the_old_object(self):
        self.patch_me({"avatar_url": self.own_key("old.jpg")})
        self.s3.delete_object.reset_mock()
        self.patch_me({"avatar_url": ""})
        self.s3.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key=self.own_key("old.jpg"))

    def test_editing_only_the_name_never_touches_the_photo(self):
        self.patch_me({"avatar_url": self.own_key()})
        self.s3.delete_object.reset_mock()
        code, body = self.patch_me({"name": "New Name"})
        self.assertEqual(body["user"]["name"], "New Name")
        self.assertEqual(body["user"]["avatar_url"], self.own_key())
        self.s3.delete_object.assert_not_called()

    def test_a_failed_presign_degrades_to_no_photo(self):
        """S3 trouble must not fail the profile route."""
        self.patch_me({"avatar_url": self.own_key()})
        self.s3.generate_presigned_url.side_effect = RuntimeError("s3 down")
        code, body = parse(call(api.get_me, event("GET", "/me")))
        self.assertEqual(code, 200)
        self.assertEqual(body["user"]["avatar_view_url"], "")

    def test_never_leaks_credentials(self):
        self.patch_me({"avatar_url": self.own_key()})
        code, body = parse(call(api.get_me, event("GET", "/me")))
        self.assertNotIn("password_hash", body["user"])
        self.assertNotIn("salt", body["user"])


class TestContactOwnPhoto(OrgTestCase):
    """A photo the OWNER set on the contact — imported or picked by hand."""

    def own_key(self, name="c.jpg"):
        return f"avatars/{USER}/contact/{name}"

    def test_created_with_a_photo(self):
        code, body = parse(call(api.create_contact, event(
            "POST", "/contacts",
            body={"name": "Rahul Sharma", "email": "rahul@company.com",
                  "avatar_url": self.own_key()})))
        self.assertEqual(code, 201)
        self.assertEqual(body["contact"]["avatar_url"], self.own_key())
        self.assertIn(self.own_key(), body["contact"]["avatar_view_url"])
        self.assertEqual(body["contact"]["avatar_source"], "own")

    def test_created_without_a_photo_reports_none(self):
        code, body = self.mk_contact()
        self.assertEqual(body["contact"]["avatar_view_url"], "")
        self.assertEqual(body["contact"]["avatar_source"], "")

    def test_create_rejects_another_users_key(self):
        code, body = parse(call(api.create_contact, event(
            "POST", "/contacts",
            body={"name": "Rahul Sharma",
                  "avatar_url": f"avatars/{OTHER}/contact/theirs.jpg"})))
        self.assertEqual(code, 400)

    def test_patch_sets_a_photo(self):
        cid = self.mk_contact()[1]["contact"]["id"]
        code, body = parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"avatar_url": self.own_key()})))
        self.assertEqual(code, 200)
        self.assertEqual(body["contact"]["avatar_source"], "own")

    def test_patch_rejects_another_users_key(self):
        cid = self.mk_contact()[1]["contact"]["id"]
        code, _ = parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"avatar_url": f"avatars/{OTHER}/contact/x.jpg"})))
        self.assertEqual(code, 400)

    def test_patch_replacing_deletes_the_old_object(self):
        cid = self.mk_contact()[1]["contact"]["id"]
        for name in ("old.jpg",):
            parse(call(api.update_contact, event(
                "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
                body={"avatar_url": self.own_key(name)})))
        self.s3.delete_object.reset_mock()
        parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"avatar_url": self.own_key("new.jpg")})))
        self.s3.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key=self.own_key("old.jpg"))

    def test_patch_clearing_removes_it(self):
        cid = self.mk_contact()[1]["contact"]["id"]
        parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"avatar_url": self.own_key()})))
        code, body = parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"avatar_url": ""})))
        self.assertEqual(body["contact"]["avatar_view_url"], "")
        self.assertEqual(body["contact"]["avatar_source"], "")

    def test_deleting_the_contact_deletes_its_photo(self):
        cid = self.mk_contact()[1]["contact"]["id"]
        parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"avatar_url": self.own_key()})))
        self.s3.delete_object.reset_mock()
        parse(call(api.delete_contact, event(
            "DELETE", "/contacts/{contact_id}", path={"contact_id": cid})))
        self.s3.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key=self.own_key())


class TestPhoneImportPhoto(OrgTestCase):
    """Importing from the phone address book, which may carry a picture."""

    def own_key(self, name="p.jpg"):
        return f"avatars/{USER}/contact/{name}"

    def import_contact(self, avatar_url="", **over):
        body = {"name": "Rahul Sharma", "email": "rahul@company.com"}
        body.update(over)
        if avatar_url:
            body["avatar_url"] = avatar_url
        return parse(call(api.create_contact,
                          event("POST", "/contacts", body=body)))

    def test_existing_contact_without_a_photo_gains_the_imported_one(self):
        """Re-importing the same person must not be the reason they have no
        photo — the first import created them, the second brings the picture."""
        self.import_contact()
        code, body = self.import_contact(avatar_url=self.own_key())
        self.assertEqual(code, 200)
        self.assertTrue(body["existing"])
        self.assertEqual(body["contact"]["avatar_source"], "own")
        self.assertIn(self.own_key(), body["contact"]["avatar_view_url"])

    def test_existing_photo_is_never_overwritten_by_an_import(self):
        """Replacing a photo is the owner's explicit call, not a side effect."""
        self.import_contact(avatar_url=self.own_key("chosen.jpg"))
        code, body = self.import_contact(avatar_url=self.own_key("phone.jpg"))
        self.assertEqual(code, 200)
        self.assertEqual(body["contact"]["avatar_url"],
                         self.own_key("chosen.jpg"))

    def test_a_rejected_import_photo_is_not_left_orphaned_in_s3(self):
        self.import_contact(avatar_url=self.own_key("chosen.jpg"))
        self.s3.delete_object.reset_mock()
        self.import_contact(avatar_url=self.own_key("phone.jpg"))
        self.s3.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key=self.own_key("phone.jpg"))


class TestLinkedMinutexPhoto(OrgTestCase):
    """A contact who is also a MinuteX user shows THEIR profile photo.

    This is the cross-account read. Every test here is about keeping it narrow.
    """

    def add_minutex_user(self, user_id=OTHER, email="rahul@company.com",
                         avatar="", name="Rahul Sharma"):
        item = {"user_id": user_id, "email": email, "name": name,
                "password_hash": "hash", "salt": "salt"}
        if avatar:
            item["avatar_url"] = avatar
        self.t["users"].put_item(Item=item)

    def linked_contact(self, email="rahul@company.com"):
        code, body = self.mk_contact(email=email)
        self.assertEqual(code, 201)
        return body["contact"]

    def test_linked_users_photo_is_shown(self):
        their_key = f"avatars/{OTHER}/user/theirs.jpg"
        self.add_minutex_user(avatar=their_key)
        contact = self.linked_contact()
        self.assertEqual(contact["minutex_user_id"], OTHER)
        self.assertEqual(contact["avatar_source"], "minutex")
        self.assertIn(their_key, contact["avatar_view_url"])

    def test_their_key_is_not_stored_on_our_contact_row(self):
        """Storing it would go stale the moment they change their photo, and
        would put another account's key in our row where a later PATCH could
        echo it back as if it were ours."""
        self.add_minutex_user(avatar=f"avatars/{OTHER}/user/theirs.jpg")
        contact = self.linked_contact()
        self.assertEqual(contact["avatar_url"], "")
        row = self.t["contacts"].get_item(
            Key={"contact_id": contact["id"]})["Item"]
        self.assertEqual(row.get("avatar_url", ""), "")

    def test_own_photo_wins_over_the_linked_one(self):
        self.add_minutex_user(avatar=f"avatars/{OTHER}/user/theirs.jpg")
        contact = self.linked_contact()
        code, body = parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": contact["id"]},
            body={"avatar_url": f"avatars/{USER}/contact/mine.jpg"})))
        self.assertEqual(body["contact"]["avatar_source"], "own")
        self.assertIn("mine.jpg", body["contact"]["avatar_view_url"])

    def test_clearing_our_photo_falls_back_to_theirs(self):
        their_key = f"avatars/{OTHER}/user/theirs.jpg"
        self.add_minutex_user(avatar=their_key)
        contact = self.linked_contact()
        parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": contact["id"]},
            body={"avatar_url": f"avatars/{USER}/contact/mine.jpg"})))
        code, body = parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": contact["id"]},
            body={"avatar_url": ""})))
        self.assertEqual(body["contact"]["avatar_source"], "minutex")
        self.assertIn(their_key, body["contact"]["avatar_view_url"])

    def test_a_linked_user_with_no_photo_falls_back_to_initials(self):
        self.add_minutex_user(avatar="")
        contact = self.linked_contact()
        self.assertEqual(contact["minutex_user_id"], OTHER)
        self.assertEqual(contact["avatar_source"], "")
        self.assertEqual(contact["avatar_view_url"], "")

    def test_an_unlinked_contact_never_reads_the_users_table(self):
        self.add_minutex_user(avatar=f"avatars/{OTHER}/user/theirs.jpg")
        self.ddb.batch_calls = 0
        self.mk_contact(email="", name="Nobody Known")
        self.assertEqual(self.ddb.batch_calls, 0)

    def test_only_the_photo_crosses_the_account_boundary(self):
        """The cross-account projection is the whole safety argument: their
        name, email and account state must not be reachable through this."""
        self.add_minutex_user(avatar=f"avatars/{OTHER}/user/theirs.jpg",
                              name="Their Real Name", email="rahul@company.com")
        contact = self.linked_contact()
        blob = json.dumps(contact)
        self.assertNotIn("Their Real Name", blob)
        self.assertNotIn("hash", blob)
        self.assertNotIn("salt", blob)
        # The contact's OWN name — the one the owner typed — is what shows.
        self.assertEqual(contact["name"], "Rahul Sharma")

    def test_the_projection_is_enforced_not_merely_requested(self):
        self.add_minutex_user(avatar=f"avatars/{OTHER}/user/theirs.jpg")
        got = api._linked_avatar_map([OTHER])
        self.assertEqual(got, {OTHER: f"avatars/{OTHER}/user/theirs.jpg"})

    def test_a_missing_user_row_is_absent_not_none(self):
        self.assertEqual(api._linked_avatar_map(["u-nonexistent"]), {})

    def test_a_users_read_failure_degrades_to_initials(self):
        self.add_minutex_user(avatar=f"avatars/{OTHER}/user/theirs.jpg")
        with mock.patch.object(self.ddb, "batch_get_item",
                               side_effect=RuntimeError("ddb down")):
            contact = self.linked_contact()
        self.assertEqual(contact["avatar_source"], "")
        self.assertEqual(contact["avatar_view_url"], "")

    def test_listing_many_contacts_costs_one_users_read(self):
        """The N+1 guard. A page of linked contacts must batch."""
        for i in range(12):
            uid = f"u-linked-{i}"
            self.add_minutex_user(user_id=uid, email=f"p{i}@company.com",
                                  avatar=f"avatars/{uid}/user/a.jpg")
            self.mk_contact(name=f"Person {i}", email=f"p{i}@company.com")
        self.ddb.batch_calls = 0
        code, body = parse(call(api.list_contacts, event("GET", "/contacts")))
        self.assertEqual(code, 200)
        self.assertEqual(len(body["contacts"]), 12)
        self.assertEqual(self.ddb.batch_calls, 1)
        for c in body["contacts"]:
            self.assertEqual(c["avatar_source"], "minutex")
            self.assertTrue(c["avatar_view_url"])

    def test_batching_chunks_above_the_hundred_key_limit(self):
        """BatchGetItem caps at 100 keys; 150 must be two calls, not a
        ValidationException that loses every photo."""
        ids = []
        for i in range(150):
            uid = f"u-many-{i}"
            self.add_minutex_user(user_id=uid, email=f"m{i}@company.com",
                                  avatar=f"avatars/{uid}/user/a.jpg")
            ids.append(uid)
        self.ddb.batch_calls = 0
        got = api._linked_avatar_map(ids)
        self.assertEqual(len(got), 150)
        self.assertEqual(self.ddb.batch_calls, 2)

    def test_a_batch_failure_on_a_LIST_never_fans_out_into_n_reads(self):
        """The degradation must not become the N+1 it exists to prevent.

        _public_contact falls back to its own lookup when handed None, so if
        _public_contacts passed a failed batch through as None a 50-contact page
        would answer a dead Users table with 50 more reads.
        """
        for i in range(5):
            uid = f"u-fail-{i}"
            self.add_minutex_user(user_id=uid, email=f"f{i}@company.com",
                                  avatar=f"avatars/{uid}/user/a.jpg")
            self.mk_contact(name=f"Person {i}", email=f"f{i}@company.com")
        calls = {"n": 0}

        def boom(**kwargs):
            calls["n"] += 1
            raise RuntimeError("ddb down")

        with mock.patch.object(self.ddb, "batch_get_item", side_effect=boom):
            code, body = parse(call(api.list_contacts,
                                    event("GET", "/contacts")))
        self.assertEqual(code, 200)
        self.assertEqual(len(body["contacts"]), 5)
        # One attempt for the whole page, not one per contact.
        self.assertEqual(calls["n"], 1)
        for c in body["contacts"]:
            self.assertEqual(c["avatar_view_url"], "")

    def test_contact_detail_shows_the_linked_photo(self):
        their_key = f"avatars/{OTHER}/user/theirs.jpg"
        self.add_minutex_user(avatar=their_key)
        contact = self.linked_contact()
        code, body = parse(call(api.get_contact, event(
            "GET", "/contacts/{contact_id}", path={"contact_id": contact["id"]})))
        self.assertEqual(code, 200)
        self.assertEqual(body["contact"]["avatar_source"], "minutex")
        self.assertIn(their_key, body["contact"]["avatar_view_url"])

    def test_deleting_a_contact_never_deletes_the_linked_users_photo(self):
        """The contact row only ever holds its OWN key, so this cannot happen
        by construction — asserted because the cost of it happening is another
        user losing their profile picture."""
        self.add_minutex_user(avatar=f"avatars/{OTHER}/user/theirs.jpg")
        contact = self.linked_contact()
        self.s3.delete_object.reset_mock()
        parse(call(api.delete_contact, event(
            "DELETE", "/contacts/{contact_id}",
            path={"contact_id": contact["id"]})))
        self.s3.delete_object.assert_not_called()


class TestAvatarObjectDeletionGuards(OrgTestCase):
    """_delete_avatar_object must never be turned into a recording delete."""

    def test_refuses_a_non_avatar_key(self):
        api._delete_avatar_object(f"recordings/{USER}/mobile/x.wav")
        self.s3.delete_object.assert_not_called()

    def test_ignores_empty(self):
        api._delete_avatar_object("")
        api._delete_avatar_object(None)
        self.s3.delete_object.assert_not_called()

    def test_a_failed_delete_is_swallowed(self):
        self.s3.delete_object.side_effect = RuntimeError("s3 down")
        api._delete_avatar_object(f"avatars/{USER}/user/a.jpg")  # must not raise


class TestAvatarRoute(unittest.TestCase):
    def test_route_is_registered(self):
        self.assertIs(api._ROUTES[("POST", "/avatars/upload-request")],
                      api.request_avatar_upload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
