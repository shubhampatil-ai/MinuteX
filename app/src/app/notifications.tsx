// src/app/notifications.tsx — the Notification Centre.
//
// One screen: a grouped, paginated list of what MinuteX has told this user,
// with the unread ones marked and every actionable row opening the thing it
// is about.
//
// THE READ RULE (requirement section 17). Opening the CENTRE does not mark
// anything read — that would empty the badge without the user having seen a
// single row, which is exactly the "notifications I never actually read"
// problem the unread state exists to prevent. A notification becomes read
// when the user OPENS IT, or when they explicitly press Mark all read.
//
// STATE COMES FROM THE CONTEXT, not from this screen. The badge in the header
// and this list are the same data; if this screen fetched its own, marking a
// row read here would leave the bell showing the old number until it happened
// to refetch. See lib/notification-center.tsx.
import { useCallback } from "react";
import {
  ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { AppNotification } from "../../lib/api";
import { Icon } from "../../lib/icons";
import {
  destinationFor, groupNotifications, iconForNotification, isActionable,
  relativeTime, toneForNotification, useNotifications,
} from "../../lib/notification-center";
import { ColorScale, FONT, R, S, useTheme } from "../../lib/theme";
import { Button, EmptyState, ErrorText, SkeletonCard } from "../../lib/ui";

export default function NotificationsScreen() {
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const {
    notifications, unreadCount, loading, loadingMore, error, hasMore,
    refresh, loadMore, markRead, markAllRead,
  } = useNotifications();

  // Refetched on every focus rather than only on mount: coming back from a
  // task or a meeting is exactly when a new notification may have landed, and
  // the centre showing a stale list would contradict the badge.
  useFocusEffect(useCallback(() => { refresh(); }, [refresh]));

  const open = useCallback(
    async (n: AppNotification) => {
      const to = destinationFor(n);
      // Marked read even when there is nowhere to go: the user has seen it.
      // Not awaited before navigating — the context updates optimistically, so
      // making the tap wait on a round trip would only add latency.
      if (!n.is_read) markRead(n.notification_id);
      if (to) router.push(to as never);
    },
    [markRead, router]
  );

  const st = buildStyles(C);
  const groups = groupNotifications(notifications);

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <Stack.Screen
        options={{
          title: "Notifications",
          // Offered ONLY when there is something to mark — a permanently
          // visible control that does nothing teaches people to ignore it.
          headerRight: () =>
            unreadCount > 0 ? (
              <Pressable
                onPress={markAllRead}
                hitSlop={8}
                style={({ pressed }) => pressed && { opacity: 0.6 }}
                accessibilityRole="button"
                accessibilityLabel="Mark all notifications as read"
              >
                <Text style={st.headerAction}>Mark all read</Text>
              </Pressable>
            ) : null,
        }}
      />

      <ScrollView
        contentContainerStyle={{
          paddingHorizontal: 20,
          paddingTop: S.lg,
          paddingBottom: insets.bottom + S.xxl,
        }}
        keyboardShouldPersistTaps="handled"
      >
        {/* An error over an EMPTY list replaces the list; an error over rows we
            already have sits above them, because stale-but-labelled beats
            blanking content the user was reading. */}
        {error ? (
          <View style={{ marginBottom: S.md }}>
            <ErrorText>{error}</ErrorText>
            <Button label="Try again" variant="ghost" onPress={refresh} />
          </View>
        ) : null}

        {loading && notifications.length === 0 ? (
          <View style={{ gap: S.md }}>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </View>
        ) : null}

        {!loading && !error && notifications.length === 0 ? (
          <EmptyState
            icon="bell.fill"
            title="No notifications yet"
            subtitle="Meeting updates, task assignments and deadlines will show up here."
          />
        ) : null}

        {groups.map((group) => (
          <View key={group.title}>
            <Text style={st.groupTitle}>{group.title}</Text>
            <View style={{ gap: S.sm }}>
              {group.items.map((n) => (
                <NotificationRow
                  key={n.notification_id}
                  n={n}
                  C={C}
                  st={st}
                  onPress={() => open(n)}
                />
              ))}
            </View>
          </View>
        ))}

        {hasMore ? (
          <View style={{ marginTop: S.lg }}>
            {loadingMore ? (
              <ActivityIndicator color={C.primary} />
            ) : (
              <Button label="Load more" variant="ghost" onPress={loadMore} />
            )}
          </View>
        ) : null}
      </ScrollView>
    </View>
  );
}

function NotificationRow({
  n, C, st, onPress,
}: {
  n: AppNotification;
  C: ColorScale;
  st: ReturnType<typeof buildStyles>;
  onPress: () => void;
}) {
  const tone = toneForNotification(n.type);
  const tint =
    tone === "danger" ? C.danger
      : tone === "warn" ? C.warn
        : tone === "success" ? C.success
          : tone === "accent" ? C.accent
            : C.primary;
  const soft =
    tone === "danger" ? C.dangerSoft
      : tone === "warn" ? C.warnSoft
        : tone === "success" ? C.successSoft
          : tone === "accent" ? C.accentSoft
            : C.primarySoft;

  const actionable = isActionable(n);
  const when = relativeTime(n.created_at);

  const body = (
    <View
      style={[
        st.row,
        // Unread rows are tinted and keep a leading dot. Read rows drop BOTH
        // rather than only dimming the text: on a list of twenty, a difference
        // in text colour alone is not reliably visible.
        !n.is_read && { backgroundColor: C.surface, borderColor: C.borderStrong },
      ]}
    >
      <View style={[st.iconWrap, { backgroundColor: soft }]}>
        <Icon name={iconForNotification(n.type) as any} tintColor={tint} size={18} />
      </View>

      <View style={{ flex: 1, gap: 2 }}>
        <View style={st.titleLine}>
          {!n.is_read ? <View style={[st.unreadDot, { backgroundColor: tint }]} /> : null}
          <Text
            style={[st.title, n.is_read && { fontFamily: FONT.medium, color: C.textDim }]}
            numberOfLines={1}
          >
            {n.title}
          </Text>
        </View>
        {n.message ? (
          <Text style={st.message} numberOfLines={2}>
            {n.message}
          </Text>
        ) : null}
        {when ? <Text style={st.when}>{when}</Text> : null}
      </View>

      {actionable ? (
        <Icon name="chevron.right" tintColor={C.textFaint} size={16} />
      ) : null}
    </View>
  );

  // A row with nowhere to go is rendered as plain content, never as a
  // Pressable — a tappable-looking row that does nothing reads as a bug.
  if (!actionable) return body;

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => pressed && { opacity: 0.7 }}
      accessibilityRole="button"
      accessibilityLabel={`${n.title}. ${n.message}${n.is_read ? "" : ". Unread"}`}
    >
      {body}
    </Pressable>
  );
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    headerAction: {
      fontFamily: FONT.semibold, fontSize: 14, color: C.primary,
    },
    groupTitle: {
      fontFamily: FONT.bold, fontSize: 11, letterSpacing: 1.2,
      textTransform: "uppercase",
      color: C.textFaint,
      marginTop: S.lg, marginBottom: S.sm,
    },
    row: {
      flexDirection: "row", alignItems: "flex-start", gap: S.md,
      backgroundColor: C.surface2,
      borderRadius: R.card, borderWidth: 1, borderColor: C.border,
      padding: S.md,
    },
    iconWrap: {
      width: 36, height: 36, borderRadius: R.md,
      alignItems: "center", justifyContent: "center",
    },
    titleLine: { flexDirection: "row", alignItems: "center", gap: 6 },
    unreadDot: { width: 7, height: 7, borderRadius: 4 },
    title: {
      flex: 1, fontFamily: FONT.semibold, fontSize: 15, color: C.text,
    },
    message: { fontFamily: FONT.regular, fontSize: 13, color: C.textDim },
    when: { fontFamily: FONT.medium, fontSize: 11, color: C.textFaint, marginTop: 2 },
  });
}
