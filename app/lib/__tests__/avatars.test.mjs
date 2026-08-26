// lib/__tests__/avatars.test.mjs — avatar helpers and the display precedence.
//
// Run:  node --test lib/__tests__/avatars.test.mjs
//
// WHAT THIS PINS. Two things in the photo feature are pure logic and therefore
// worth pinning here rather than only in the backend suite:
//
//   1. extFromUri — the extension an image is uploaded under. Get it wrong and
//      the presign is rejected (an extension the backend does not accept) or
//      the object is stored mislabelled. Android content:// URIs make this
//      less obvious than it looks: they can carry a query string, and they can
//      carry no extension at all.
//   2. The Avatar decision — photo or initials. It is a one-line rule spread
//      across four screens before the shared component existed, and its
//      interesting case is the one a naive boolean gets wrong: an avatar URL
//      that failed to load, then REPLACED by a freshly re-signed URL for the
//      same image, must be attempted again rather than staying hidden.
//
// Both are mirrored below rather than imported: lib/avatars.ts pulls in
// expo-file-system and lib/ui.tsx pulls in react-native, neither of which
// loads under plain node. Same convention as phone-contact-dedup.test.mjs.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from lib/avatars.ts ------------------------------------------

const ALLOWED_EXT = new Set(["jpg", "jpeg", "png", "webp", "heic"]);
const DEFAULT_EXT = "jpg";

function extFromUri(uri) {
  const path = String(uri || "").split(/[?#]/)[0];
  const dot = path.lastIndexOf(".");
  if (dot < 0) return DEFAULT_EXT;
  const ext = path.slice(dot + 1).toLowerCase();
  return ALLOWED_EXT.has(ext) ? ext : DEFAULT_EXT;
}

// --- mirrored from Avatar in lib/ui.tsx ------------------------------------
// The whole decision, as a function of the props and the one piece of state.
function showsPhoto({ photoUri, failedUri }) {
  return !!photoUri && failedUri !== photoUri;
}

// --- tests ------------------------------------------------------------------

describe("extFromUri", () => {
  it("keeps every format the backend accepts", () => {
    for (const ext of ["jpg", "jpeg", "png", "webp", "heic"]) {
      assert.equal(extFromUri(`file:///tmp/pic.${ext}`), ext);
    }
  });

  it("lowercases, so PIC.JPG is not sent as an unknown format", () => {
    assert.equal(extFromUri("file:///tmp/PIC.JPG"), "jpg");
    assert.equal(extFromUri("file:///tmp/Photo.PNG"), "png");
  });

  it("strips a query string — an Android content:// URI can carry one", () => {
    // "jpg?width=320" is not a format the backend knows; without the strip the
    // presign is rejected outright.
    assert.equal(
      extFromUri("content://media/external/images/1.jpg?width=320"),
      "jpg"
    );
    assert.equal(extFromUri("file:///tmp/a.png#frag"), "png");
  });

  it("falls back to jpg when there is no extension at all", () => {
    // Common for content:// URIs, which are opaque ids rather than paths. The
    // picker has already transcoded to a real image by this point, so the
    // extension only has to be an honest label.
    assert.equal(extFromUri("content://com.android.contacts/photo/17"), "jpg");
    assert.equal(extFromUri("file:///tmp/noextension"), "jpg");
  });

  it("falls back to jpg for a format the backend would reject", () => {
    // Never pass through an extension the presign will refuse — the upload
    // would fail after the user already picked a photo.
    assert.equal(extFromUri("file:///tmp/pic.gif"), "jpg");
    assert.equal(extFromUri("file:///tmp/pic.bmp"), "jpg");
    assert.equal(extFromUri("file:///tmp/clip.mp4"), "jpg");
  });

  it("is not fooled by a dot in a directory name", () => {
    assert.equal(extFromUri("file:///my.photos/pic.png"), "png");
    // A dotted directory with an extensionless file must not read the
    // directory's suffix as the format.
    assert.equal(extFromUri("file:///my.photos/pic"), "jpg");
  });

  it("handles empty and nullish input without throwing", () => {
    assert.equal(extFromUri(""), "jpg");
    assert.equal(extFromUri(undefined), "jpg");
    assert.equal(extFromUri(null), "jpg");
  });
});

describe("Avatar: photo or initials", () => {
  it("draws initials when there is no photo", () => {
    assert.equal(showsPhoto({ photoUri: undefined, failedUri: undefined }), false);
    assert.equal(showsPhoto({ photoUri: "", failedUri: undefined }), false);
  });

  it("draws the photo when there is one", () => {
    assert.equal(showsPhoto({ photoUri: "https://s3/a.jpg" }), true);
  });

  it("falls back to initials when that photo failed to load", () => {
    // An expired presigned URL, or a revoked local path. A blank disc would be
    // worse than the initials it replaced.
    const uri = "https://s3/a.jpg?expired";
    assert.equal(showsPhoto({ photoUri: uri, failedUri: uri }), false);
  });

  it("retries a FRESH url after an earlier one failed", () => {
    // The case a plain `failed` boolean gets wrong. Re-signing yields a
    // different URL for the same image, so the new one deserves an attempt —
    // otherwise one expired load hides the photo for as long as the row stays
    // mounted, which on a list the user is scrolling is most of the session.
    assert.equal(
      showsPhoto({
        photoUri: "https://s3/a.jpg?sig=new",
        failedUri: "https://s3/a.jpg?sig=old",
      }),
      true
    );
  });

  it("still falls back when a photo is cleared after a failure", () => {
    assert.equal(showsPhoto({ photoUri: "", failedUri: "https://s3/a.jpg" }), false);
  });
});

describe("avatar precedence, as the API reports it", () => {
  // The app never computes this — the backend resolves it and states the answer
  // in avatar_source. What IS the app's job is rendering avatar_view_url
  // whatever its source, and offering a delete ONLY for a photo this account
  // owns. These pin the two rules the screens depend on.
  const own = {
    avatar_url: "avatars/u-1/contact/mine.jpg",
    avatar_view_url: "https://s3/mine.jpg",
    avatar_source: "own",
  };
  const linked = {
    avatar_url: "",
    avatar_view_url: "https://s3/theirs.jpg",
    avatar_source: "minutex",
  };
  const none = { avatar_url: "", avatar_view_url: "", avatar_source: "" };

  // What the contact screen passes as canRemove.
  const canRemove = (c) => !!c.avatar_url;

  it("renders a photo for both sources", () => {
    assert.equal(showsPhoto({ photoUri: own.avatar_view_url }), true);
    assert.equal(showsPhoto({ photoUri: linked.avatar_view_url }), true);
  });

  it("renders initials when nobody has a photo", () => {
    assert.equal(showsPhoto({ photoUri: none.avatar_view_url }), false);
  });

  it("offers Remove only for a photo this account stored", () => {
    assert.equal(canRemove(own), true);
    // A linked MinuteX user's photo is THEIRS. Offering to delete it would be
    // an action that cannot work — the row holds no key to clear.
    assert.equal(canRemove(linked), false);
    assert.equal(canRemove(none), false);
  });

  it("distinguishes 'has a photo' from 'owns that photo'", () => {
    // The distinction the whole avatar_source field exists for: a contact can
    // display a picture while this account has nothing to edit or delete.
    assert.equal(!!linked.avatar_view_url, true);
    assert.equal(canRemove(linked), false);
  });
});
