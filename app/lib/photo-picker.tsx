// lib/photo-picker.tsx — "choose a photo" for a person, shared by the profile
// screen and the contact screen.
//
// One flow, two callers, because the interesting parts are identical and are
// the parts easy to get wrong: three sources (library, camera, remove), an OS
// permission that can be denied recoverably OR permanently, an upload that can
// fail, and a build that may not include the native picker at all.
//
// WHAT THIS DOES NOT DO: it uploads and hands back an S3 KEY. Storing that key
// on a user or a contact is the caller's job (updateMe / updateContact), which
// keeps this component from needing to know which kind of row it is editing —
// and means an abandoned sheet writes nothing.
import { useCallback, useState } from "react";
import { Linking, Modal, Pressable, StyleSheet, Text, View } from "react-native";
import { Icon } from "./icons";
import { S, R, FONT, useTheme, ColorScale } from "./theme";
import { Avatar, Button, ErrorText } from "./ui";
import {
  PickOutcome, canPickImage, pickImageFromCamera, pickImageFromLibrary,
  uploadAvatar,
} from "./avatars";
import { ApiError } from "./api";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    backdrop: {
      flex: 1, backgroundColor: "rgba(0,0,0,0.45)",
      justifyContent: "flex-end" as const,
    },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.card * 1.5,
      borderTopRightRadius: R.card * 1.5, padding: S.lg, gap: S.sm,
    },
    header: { alignItems: "center" as const, gap: S.sm, paddingBottom: S.sm },
    title: { fontFamily: FONT.bold, fontSize: 17, color: C.text },
    sub: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textFaint,
      textAlign: "center" as const,
    },
    option: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      paddingVertical: 14, paddingHorizontal: S.md,
      backgroundColor: C.surface, borderRadius: R.card,
    },
    optionTxt: { fontFamily: FONT.semibold, fontSize: 15, color: C.text },
    destructiveTxt: { fontFamily: FONT.semibold, fontSize: 15, color: C.danger },
  });
}

export type PhotoPickerProps = {
  visible: boolean;
  onClose: () => void;
  /** Shown in the sheet's preview disc, so the user sees what they are changing. */
  name: string;
  /** The photo currently rendered, if any — a presigned avatar_view_url. */
  currentPhotoUri?: string;
  /** Whose photo this is. "contact" needs contactId when the contact exists. */
  scope: "user" | "contact";
  contactId?: string;
  /**
   * Called with the uploaded S3 KEY, or "" when the user chose Remove. The
   * caller persists it and should keep the sheet open until it resolves — this
   * component stays in its busy state until then, so a slow save cannot be
   * double-submitted.
   */
  onPicked: (avatarKey: string) => Promise<void> | void;
  /** Hides the Remove option when there is nothing to remove. */
  canRemove?: boolean;
  /**
   * Set when the displayed photo belongs to the linked MinuteX user rather than
   * to this contact. Their photo cannot be removed from here — it is theirs —
   * so the sheet says so instead of offering an action that would do nothing.
   */
  isLinkedPhoto?: boolean;
};

export function PhotoPicker({
  visible, onClose, name, currentPhotoUri, scope, contactId, onPicked,
  canRemove = false, isLinkedPhoto = false,
}: PhotoPickerProps) {
  const { C } = useTheme();
  const st = buildStyles(C);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Set when the OS has permanently declined: the only way forward is Settings,
  // so the sheet offers that rather than a button that silently does nothing.
  const [blocked, setBlocked] = useState("");

  const available = canPickImage();

  const handle = useCallback(
    async (pick: () => Promise<PickOutcome>) => {
      setError("");
      setBlocked("");
      const outcome = await pick();
      if (outcome.status === "cancelled") return;
      if (outcome.status === "denied") {
        setError("MinuteX needs access to your photos to set a picture.");
        return;
      }
      if (outcome.status === "blocked") {
        setBlocked(
          available
            ? "Photo access is turned off for MinuteX. You can turn it back on in Settings."
            : "Choosing a photo needs a newer build of the app."
        );
        return;
      }
      if (outcome.status === "error") {
        setError(outcome.message);
        return;
      }
      setBusy(true);
      try {
        const key = await uploadAvatar({
          uri: outcome.uri,
          size: outcome.size,
          scope,
          contactId,
        });
        await onPicked(key);
        onClose();
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not upload that photo."
        );
      } finally {
        setBusy(false);
      }
    },
    [available, contactId, onClose, onPicked, scope]
  );

  const remove = useCallback(async () => {
    setError("");
    setBusy(true);
    try {
      await onPicked("");
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not remove the photo.");
    } finally {
      setBusy(false);
    }
  }, [onClose, onPicked]);

  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={busy ? undefined : onClose}
    >
      <Pressable style={st.backdrop} onPress={busy ? undefined : onClose}>
        {/* Swallow taps on the sheet itself so they don't dismiss it. */}
        <Pressable style={st.sheet} onPress={() => {}}>
          <View style={st.header}>
            <Avatar name={name} photoUri={currentPhotoUri} size={72} />
            <Text style={st.title}>
              {currentPhotoUri ? "Change photo" : "Add a photo"}
            </Text>
            {isLinkedPhoto ? (
              <Text style={st.sub}>
                {name.split(" ")[0]} is on MinuteX, so their own profile photo
                shows here. Adding one of your own will show that instead.
              </Text>
            ) : null}
          </View>

          {blocked ? (
            <>
              <Text style={st.sub}>{blocked}</Text>
              {available ? (
                <Button
                  label="Open Settings"
                  variant="secondary"
                  onPress={() => void Linking.openSettings()}
                />
              ) : null}
            </>
          ) : available ? (
            <>
              <Pressable
                style={st.option}
                onPress={() => void handle(pickImageFromLibrary)}
                disabled={busy}
                accessibilityRole="button"
                accessibilityLabel="Choose from your photos"
              >
                <Icon name="photo" size={19} tintColor={C.primary} />
                <Text style={st.optionTxt}>Choose from photos</Text>
              </Pressable>
              <Pressable
                style={st.option}
                onPress={() => void handle(pickImageFromCamera)}
                disabled={busy}
                accessibilityRole="button"
                accessibilityLabel="Take a photo"
              >
                <Icon name="camera" size={19} tintColor={C.primary} />
                <Text style={st.optionTxt}>Take a photo</Text>
              </Pressable>
              {/* Remove is offered only for a photo this account actually owns.
                  A linked MinuteX user's photo is theirs — there is nothing
                  here to delete, and implying otherwise would be a lie. */}
              {canRemove && !isLinkedPhoto ? (
                <Pressable
                  style={st.option}
                  onPress={() => void remove()}
                  disabled={busy}
                  accessibilityRole="button"
                  accessibilityLabel="Remove the current photo"
                >
                  <Icon name="trash" size={19} tintColor={C.danger} />
                  <Text style={st.destructiveTxt}>Remove photo</Text>
                </Pressable>
              ) : null}
            </>
          ) : (
            <Text style={st.sub}>
              Choosing a photo needs a newer build of the app.
            </Text>
          )}

          {error ? <ErrorText>{error}</ErrorText> : null}

          <Button
            label={busy ? "Working…" : "Cancel"}
            variant="ghost"
            loading={busy}
            onPress={onClose}
            disabled={busy}
          />
        </Pressable>
      </Pressable>
    </Modal>
  );
}
