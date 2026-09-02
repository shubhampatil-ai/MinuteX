// lib/meeting-share.tsx — share a meeting as a read-only public web link.
//
// WHAT THIS REPLACES. The overflow menu's "Share meeting" used to call
// Share.share({message: rec.title}) — it sent the meeting's TITLE as plain
// text and nothing else. Anyone receiving it got a sentence, not the meeting.
// Exporting a PDF (lib/mom-pdf.ts) worked but sends a file: it cannot be
// updated, cannot be revoked, and is awkward to read on a phone.
//
// A link fixes all three. The recipient needs no MinuteX account and no app —
// the backend renders a mobile web page from the token in the URL.
//
// TWO THINGS ABOUT THE LINK DRIVE THIS WHOLE UI:
//
//   1. THE URL IS SHOWN EXACTLY ONCE. Only sha256(token) is stored server
//      side, so the server genuinely cannot hand the URL back later — see
//      lib/api.ts's Meeting Share section. That is why creation lands on a
//      dedicated "copy it now" screen, and why the existing-links list offers
//      Revoke but no Copy. Presenting a disabled Copy button on old links
//      would read as a bug; the honest UI is to not offer it.
//
//   2. THE TOGGLES ARE ENFORCED ON THE SERVER. Transcript and audio default
//      OFF and, when off, never enter the rendered page at all. So this sheet
//      is choosing what to PUBLISH, not what to display — which is why the
//      two sensitive rows carry an explicit caption rather than sitting
//      silently among the others.
//
// Three states in one component, because they are one flow: configure ->
// created -> manage. Splitting them across files would put the "copy it now"
// screen out of reach of the state that produced the URL.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, Alert, Modal, Pressable, ScrollView, Share, StyleSheet,
  Text, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, FONT, useTheme, ColorScale } from "./theme";
import { Button, Divider, ErrorText, scrollFormProps } from "./ui";
import { copyText } from "./clipboard";
import {
  DEFAULT_SHARE_CONFIG, MeetingShare, ShareConfig, createShare, listShares,
  revokeShare,
} from "./api";

// The checklist, in the order the spec lays it out. `caption` is present only
// on the two rows that publish raw meeting material — the ones where a user
// needs to understand what they are turning on before they turn it on.
const ROWS: {
  key: keyof ShareConfig;
  label: string;
  caption?: string;
}[] = [
  { key: "summary", label: "Summary" },
  { key: "highlights", label: "Highlights" },
  { key: "decisions", label: "Decisions" },
  { key: "tasks", label: "Action Items" },
  { key: "participants", label: "Participants" },
  {
    key: "transcript", label: "Transcript",
    caption: "Publishes the full word-for-word transcript.",
  },
  {
    key: "audio", label: "Audio",
    caption: "Lets anyone with the link play the recording.",
  },
];

// null = never. Kept short and concrete: an expiry the user cannot reason
// about is one they will not set.
const EXPIRY_OPTIONS: { label: string; days: number | null }[] = [
  { label: "Never", days: null },
  { label: "24 hours", days: 1 },
  { label: "7 days", days: 7 },
  { label: "30 days", days: 30 },
];

function expiryLabel(share: MeetingShare): string {
  if (share.revoked_at) return "Revoked";
  if (!share.expires_at) return "Never expires";
  const at = new Date(share.expires_at);
  if (Number.isNaN(at.getTime())) return "Expired";
  if (at.getTime() <= Date.now()) return "Expired";
  return `Expires ${at.toLocaleDateString(undefined, {
    day: "numeric", month: "short", year: "numeric",
  })}`;
}

function enabledSummary(share: MeetingShare): string {
  const on = ROWS.filter((r) => {
    const attr = `${r.key === "tasks" ? "tasks" : r.key}_enabled` as keyof MeetingShare;
    return Boolean(share[attr]);
  }).map((r) => r.label);
  return on.length ? on.join(" · ") : "Nothing shared";
}

export type ShareMeetingSheetProps = {
  visible: boolean;
  onClose: () => void;
  recordingKey: string;
  meetingTitle: string;
};

export function ShareMeetingSheet({
  visible, onClose, recordingKey, meetingTitle,
}: ShareMeetingSheetProps) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  const [config, setConfig] = useState<ShareConfig>({ ...DEFAULT_SHARE_CONFIG });
  const [expiryDays, setExpiryDays] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  // The created link. Holding it in state is the ONLY copy that will ever
  // exist on this device — see the file header.
  const [createdUrl, setCreatedUrl] = useState("");
  const [copied, setCopied] = useState(false);

  const [manageOpen, setManageOpen] = useState(false);
  const [shares, setShares] = useState<MeetingShare[]>([]);
  const [loadingShares, setLoadingShares] = useState(false);

  // Reset on open so a link created for a previous meeting can never be shown
  // attached to this one.
  useEffect(() => {
    if (!visible) return;
    setConfig({ ...DEFAULT_SHARE_CONFIG });
    setExpiryDays(null);
    setCreatedUrl("");
    setCopied(false);
    setError("");
    setManageOpen(false);
  }, [visible, recordingKey]);

  const refreshShares = useCallback(async () => {
    setLoadingShares(true);
    try {
      setShares(await listShares(recordingKey));
    } catch {
      // Non-fatal: the manage list is secondary to creating a link. The
      // existing-links row simply shows nothing rather than blocking the
      // sheet the user actually opened.
      setShares([]);
    } finally {
      setLoadingShares(false);
    }
  }, [recordingKey]);

  useEffect(() => {
    if (visible) refreshShares();
  }, [visible, refreshShares]);

  const activeCount = shares.filter((s) => s.active).length;

  const toggle = (key: keyof ShareConfig) =>
    setConfig((c) => ({ ...c, [key]: !c[key] }));

  const nothingSelected = !Object.values(config).some(Boolean);

  const onCreate = async () => {
    setCreating(true);
    setError("");
    try {
      const res = await createShare(recordingKey, config, expiryDays);
      setCreatedUrl(res.url);
      setCopied(false);
      refreshShares();
    } catch (e: any) {
      setError(e?.message || "Couldn't create the share link.");
    } finally {
      setCreating(false);
    }
  };

  const onCopy = async () => {
    const result = await copyText(createdUrl);
    if (result === "failed") {
      setError("Couldn't copy the link.");
      return;
    }
    setCopied(true);
  };

  const onShare = async () => {
    try {
      // `message` carries the URL as well as `url` does, deliberately: `url`
      // is honoured by iOS share targets, while on Android only `message` is
      // read — a content object with the link ONLY in `url` shares an empty
      // body there. `title` is iOS-only and `dialogTitle` is its Android
      // counterpart, so both are passed to name the sheet on either platform.
      await Share.share(
        {
          message: `${meetingTitle}\n${createdUrl}`,
          url: createdUrl,
          title: meetingTitle,
        },
        { dialogTitle: `Share “${meetingTitle}”` }
      );
    } catch {
      // user cancelled
    }
  };

  const onRevoke = (share: MeetingShare) => {
    Alert.alert(
      "Revoke this link?",
      "Anyone holding it will immediately lose access. This can't be undone — "
      + "you can create a new link instead.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Revoke",
          style: "destructive",
          onPress: async () => {
            // Optimistic: the row flips to Revoked at once, because waiting on
            // a round trip after a destructive confirm feels broken.
            setShares((rows) => rows.map((r) =>
              r.share_id === share.share_id
                ? { ...r, active: false, revoked_at: new Date().toISOString() }
                : r));
            try {
              await revokeShare(share.share_id);
            } catch (e: any) {
              Alert.alert("Couldn't revoke", e?.message || "Please try again.");
            } finally {
              refreshShares();
            }
          },
        },
      ]
    );
  };

  // ---- Created ----------------------------------------------------------
  // A separate screen rather than a toast, because this is the one moment the
  // URL exists and the user must act on it.
  if (createdUrl) {
    return (
      <Modal visible={visible} transparent animationType="slide"
        onRequestClose={onClose}>
        <Pressable style={st.backdrop} onPress={onClose}>
          <Pressable style={st.sheet} onPress={() => { }}>
            <View style={st.grabber} />
            <View style={st.doneIcon}>
              <Icon name="link" tintColor={C.primary} size={22} />
            </View>
            <Text style={st.title}>Share Link Created</Text>
            <Text style={st.subtitle}>
              Anyone with this link can view the selected meeting content.
            </Text>

            <View style={st.linkBox}>
              <Text style={st.linkText} numberOfLines={2} selectable>
                {createdUrl}
              </Text>
            </View>

            {/* Said plainly, because it changes what the user must do now. */}
            <Text style={st.warnText}>
              This link is shown only once — copy it before closing.
            </Text>

            {!!error && <ErrorText>{error}</ErrorText>}

            <Button
              label={copied ? "Copied" : "Copy Link"}
              onPress={onCopy}
              icon={<Icon name={copied ? "checkmark" : "doc.on.doc"}
                tintColor={C.textOnPrimary} size={17} />}
            />
            <Button label="Share" variant="secondary" onPress={onShare}
              style={{ marginTop: S.sm }}
              icon={<Icon name="square.and.arrow.up" tintColor={C.text} size={17} />} />
            <Button label="Done" variant="ghost" onPress={onClose}
              style={{ marginTop: S.xs }} />
          </Pressable>
        </Pressable>
      </Modal>
    );
  }

  // ---- Manage existing links -------------------------------------------
  if (manageOpen) {
    return (
      <Modal visible={visible} transparent animationType="slide"
        onRequestClose={() => setManageOpen(false)}>
        <Pressable style={st.backdrop} onPress={() => setManageOpen(false)}>
          <Pressable style={st.sheet} onPress={() => { }}>
            <View style={st.grabber} />
            <View style={st.headerRow}>
              <Pressable onPress={() => setManageOpen(false)} hitSlop={10}
                accessibilityLabel="Back">
                <Icon name="chevron.left" tintColor={C.textDim} size={18} />
              </Pressable>
              <Text style={st.title}>Share Links</Text>
              <View style={{ width: 18 }} />
            </View>

            <ScrollView style={st.list} {...scrollFormProps}>
              {loadingShares && !shares.length ? (
                <ActivityIndicator color={C.primary} style={{ marginVertical: S.lg }} />
              ) : !shares.length ? (
                <Text style={st.empty}>No share links for this meeting yet.</Text>
              ) : (
                shares.map((share) => (
                  <View key={share.share_id} style={st.shareRow}>
                    <View style={{ flex: 1 }}>
                      <View style={st.shareTitleRow}>
                        <View style={[st.statusDot, {
                          backgroundColor: share.active ? C.success : C.textDim,
                        }]} />
                        <Text style={st.shareTitle}>
                          {share.active ? "Active link" : "Inactive link"}
                        </Text>
                      </View>
                      <Text style={st.shareMeta}>{enabledSummary(share)}</Text>
                      <Text style={st.shareMeta}>
                        {expiryLabel(share)}
                        {share.view_count > 0
                          ? ` · ${share.view_count} view${share.view_count === 1 ? "" : "s"}`
                          : ""}
                      </Text>
                    </View>
                    {share.active && (
                      <Pressable onPress={() => onRevoke(share)} hitSlop={8}
                        accessibilityLabel="Revoke this share link">
                        <Text style={st.revoke}>Revoke</Text>
                      </Pressable>
                    )}
                  </View>
                ))
              )}
            </ScrollView>

            <Button label="Create New Link" variant="secondary"
              onPress={() => setManageOpen(false)} />
          </Pressable>
        </Pressable>
      </Modal>
    );
  }

  // ---- Configure --------------------------------------------------------
  return (
    <Modal visible={visible} transparent animationType="slide"
      onRequestClose={onClose}>
      <Pressable style={st.backdrop} onPress={onClose}>
        <Pressable style={st.sheet} onPress={() => { }}>
          <View style={st.grabber} />
          <Text style={st.title}>Share Meeting</Text>
          <Text style={st.subtitle}>
            Creates a private web link. The recipient does not need a
            MinuteX account.
          </Text>

          <ScrollView style={st.list} {...scrollFormProps}>
            <Text style={st.sectionLabel}>What can they see?</Text>
            {ROWS.map((row) => {
              const on = config[row.key];
              return (
                <Pressable key={row.key} style={st.toggleRow}
                  onPress={() => toggle(row.key)}
                  accessibilityRole="switch"
                  accessibilityState={{ checked: on }}
                  accessibilityLabel={row.label}
                >
                  {/* Drawn rather than iconised: the icon set has a filled
                      check circle but no matching empty one, and a mismatched
                      pair reads as a rendering bug. */}
                  <View style={[st.check, on && st.checkOn]}>
                    {on && <Icon name="checkmark" tintColor={C.textOnPrimary} size={13} />}
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={[st.toggleLabel, on && { color: C.text }]}>
                      {row.label}
                    </Text>
                    {!!row.caption && (
                      <Text style={st.toggleCaption}>{row.caption}</Text>
                    )}
                  </View>
                </Pressable>
              );
            })}

            <Divider style={{ marginVertical: S.md }} />

            <Text style={st.sectionLabel}>Link expiration</Text>
            <View style={st.chips}>
              {EXPIRY_OPTIONS.map((opt) => {
                const on = expiryDays === opt.days;
                return (
                  <Pressable key={opt.label}
                    style={[st.chip, on && st.chipOn]}
                    onPress={() => setExpiryDays(opt.days)}
                    accessibilityRole="button"
                    accessibilityState={{ selected: on }}
                  >
                    <Text style={[st.chipTxt, on && { color: C.textOnPrimary }]}>
                      {opt.label}
                    </Text>
                  </Pressable>
                );
              })}
            </View>

            {activeCount > 0 && (
              <Pressable style={st.manageRow} onPress={() => setManageOpen(true)}
                accessibilityLabel="View existing share links">
                <Icon name="link" tintColor={C.primary} size={16} />
                <Text style={st.manageTxt}>
                  {activeCount} active link{activeCount === 1 ? "" : "s"} — manage
                </Text>
                <Icon name="chevron.right" tintColor={C.textDim} size={15} />
              </Pressable>
            )}
          </ScrollView>

          {!!error && <ErrorText>{error}</ErrorText>}

          <Button
            label="Create Share Link"
            onPress={onCreate}
            loading={creating}
            disabled={nothingSelected}
          />
          {nothingSelected && (
            <Text style={st.hint}>Select at least one section to share.</Text>
          )}
          <Button label="Cancel" variant="ghost" onPress={onClose}
            style={{ marginTop: S.xs }} />
        </Pressable>
      </Pressable>
    </Modal>
  );
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    backdrop: {
      flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end",
    },
    sheet: {
      backgroundColor: C.surface,
      borderTopLeftRadius: R.lg, borderTopRightRadius: R.lg,
      paddingHorizontal: S.lg, paddingTop: S.sm, paddingBottom: S.xl,
      maxHeight: "88%",
    },
    grabber: {
      width: 38, height: 4, borderRadius: 2, backgroundColor: C.border,
      alignSelf: "center", marginBottom: S.md,
    },
    headerRow: {
      flexDirection: "row", alignItems: "center",
      justifyContent: "space-between", marginBottom: S.xs,
    },
    doneIcon: {
      alignSelf: "center", width: 46, height: 46, borderRadius: 23,
      backgroundColor: C.surface2, alignItems: "center",
      justifyContent: "center", marginBottom: S.sm,
    },
    title: {
      fontFamily: FONT.bold, fontSize: 18, color: C.text, textAlign: "center",
    },
    subtitle: {
      fontFamily: FONT.regular, fontSize: 13.5, color: C.textDim,
      textAlign: "center", marginTop: S.xs, marginBottom: S.md,
      lineHeight: 19,
    },
    sectionLabel: {
      fontFamily: FONT.bold, fontSize: 12, color: C.textDim,
      textTransform: "uppercase", letterSpacing: 0.6, marginBottom: S.sm,
    },
    list: { maxHeight: 380, marginBottom: S.md },
    toggleRow: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingVertical: S.sm,
    },
    check: {
      width: 22, height: 22, borderRadius: 11, borderWidth: 1.5,
      borderColor: C.border, alignItems: "center", justifyContent: "center",
    },
    checkOn: { backgroundColor: C.primary, borderColor: C.primary },
    toggleLabel: { fontFamily: FONT.medium, fontSize: 15, color: C.textDim },
    toggleCaption: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textDim,
      marginTop: 2, lineHeight: 16,
    },
    chips: { flexDirection: "row", flexWrap: "wrap", gap: S.sm },
    chip: {
      paddingHorizontal: S.md, paddingVertical: S.sm - 2, borderRadius: R.pill,
      borderWidth: 1, borderColor: C.border, backgroundColor: C.surface2,
    },
    chipOn: { backgroundColor: C.primary, borderColor: C.primary },
    chipTxt: { fontFamily: FONT.medium, fontSize: 13, color: C.text },
    manageRow: {
      flexDirection: "row", alignItems: "center", gap: S.sm,
      marginTop: S.lg, paddingVertical: S.sm,
    },
    manageTxt: { flex: 1, fontFamily: FONT.medium, fontSize: 14, color: C.primary },
    linkBox: {
      backgroundColor: C.surface2, borderRadius: R.md, padding: S.md,
      borderWidth: 1, borderColor: C.border, marginBottom: S.sm,
    },
    linkText: { fontFamily: FONT.regular, fontSize: 13, color: C.text, lineHeight: 18 },
    warnText: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textDim,
      textAlign: "center", marginBottom: S.md,
    },
    hint: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textDim,
      textAlign: "center", marginTop: S.xs,
    },
    empty: {
      fontFamily: FONT.regular, fontSize: 14, color: C.textDim,
      textAlign: "center", paddingVertical: S.lg,
    },
    shareRow: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingVertical: S.md, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    shareTitleRow: { flexDirection: "row", alignItems: "center", gap: S.sm },
    statusDot: { width: 8, height: 8, borderRadius: 4 },
    shareTitle: { fontFamily: FONT.medium, fontSize: 14.5, color: C.text },
    shareMeta: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim,
      marginTop: 2,
    },
    revoke: { fontFamily: FONT.bold, fontSize: 13.5, color: C.danger },
  });
}
