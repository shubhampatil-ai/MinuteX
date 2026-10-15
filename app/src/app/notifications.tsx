// src/app/notifications.tsx — the Notification Centre.
//
// One screen: a grouped, paginated list of what MinuteX has told this user,
// with the unread ones marked and every actionable row opening the thing it
// is about. Flat list rows (not cards): a thin coloured bar and dot for the
// kind of notification, title and message, then the time and a chevron.
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
// to refetch. See lib/notification-center.tsx. The filter tabs narrow the
// loaded rows on the device; they never fetch.
import { useCallback, useState } from "react";
import {
  ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { AppNotification } from "../../lib/api";
import { Icon } from "../../lib/icons";
import {
  NOTIFICATION_FILTERS, NotificationFilter, clockTime, destinationFor, filterNotifications,
  groupNotifications, isActionable, toneForNotification, useNotifications,
} from "../../lib/notification-center";
import { ColorScale, FONT, GUTTER, S, useTheme } from "../../lib/theme";
import { Button, EmptyState, ErrorText } from "../../lib/ui";

export default function NotificationsScreen() {
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const {
    notifications, unreadCount, loading, loadingMore, error, hasMore,
    refresh, loadMore, markRead, markAllRead,
  } = useNotifications();
  const [filter, setFilter] = useState<NotificationFilter>("all");

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
  const visible = filterNotifications(notifications, filter);
  const groups = groupNotifications(visible);
  const filterLabel = NOTIFICATION_FILTERS.find((f) => f.key === filter)?.label ?? "";

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <Stack.Screen
        options={{
          title: "Notifications",
          headerTitle: () => (
            <View style={st.titleRow}>
              <Text style={st.screenTitle} numberOfLines={1}>Notifications</Text>
              {unreadCount > 0 ? (
                <View style={st.countBadge}
                  accessibilityLabel={`${unreadCount} unread`}>
                  <Text style={st.countText}>{unreadCount > 99 ? "99+" : unreadCount}</Text>
                </View>
              ) : null}
            </View>
          ),
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

      {/* Filter tabs: fixed above the list, text + underline only. Scrolls
          sideways rather than squeezing on the narrowest phones. */}
      <View style={st.tabsBar}>
        <ScrollView horizontal showsHorizontalScrollIndicator={false}
          contentContainerStyle={st.tabs}>
          {NOTIFICATION_FILTERS.map((f) => {
            const on = f.key === filter;
            return (
              <Pressable key={f.key} onPress={() => setFilter(f.key)}
                style={[st.tab, on && st.tabOn]}
                accessibilityRole="tab" accessibilityState={{ selected: on }}>
                <Text style={[st.tabText, on && st.tabTextOn]}>{f.label}</Text>
              </Pressable>
            );
          })}
        </ScrollView>
      </View>

      <ScrollView
        contentContainerStyle={{
          paddingHorizontal: GUTTER,
          paddingBottom: insets.bottom + S.xxl,
        }}
        keyboardShouldPersistTaps="handled"
      >
        {/* An error over an EMPTY list replaces the list; an error over rows we
            already have sits above them, because stale-but-labelled beats
            blanking content the user was reading. */}
        {error ? (
          <View style={{ marginTop: S.lg }}>
            <ErrorText>{error}</ErrorText>
            <Button label="Try again" variant="ghost" onPress={refresh} />
          </View>
        ) : null}

        {loading && notifications.length === 0 ? (
          <View style={{ marginTop: S.lg }}>
            {[0, 1, 2].map((i) => <SkeletonRow key={i} st={st} />)}
          </View>
        ) : null}

        {!loading && !error && notifications.length === 0 ? (
          <EmptyState
            icon="bell.fill"
            title="No notifications yet"
            subtitle="Meeting updates, task assignments and deadlines will show up here."
          />
        ) : null}

        {!loading && notifications.length > 0 && visible.length === 0 ? (
          <Text style={st.filterEmpty}>No {filterLabel} notifications.</Text>
        ) : null}

        {groups.map((group) => (
          <View key={group.title}>
            <Text style={st.groupTitle}>{group.title}</Text>
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
        ))}

        {hasMore ? (
          <View style={{ marginTop: S.lg }}>
            {loadingMore ? (
              <ActivityIndicator color={C.accent} />
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

  const actionable = isActionable(n);
  const when = clockTime(n.created_at);

  const body = (
    <View style={st.row}>
      {/* The bar and dot carry the kind of notification; unread is the dot.
          A read row keeps a faint bar so the list's rhythm doesn't change. */}
      <View style={[st.bar, { backgroundColor: n.is_read ? C.border : tint }]} />
      <View style={st.dotSlot}>
        {!n.is_read ? <View style={[st.dot, { backgroundColor: tint }]} /> : null}
      </View>

      <View style={st.content}>
        <Text
          style={[st.title, n.is_read && { fontFamily: FONT.medium, color: C.textDim }]}
          numberOfLines={2}
        >
          {n.title}
        </Text>
        {n.message ? (
          <Text style={st.message} numberOfLines={3}>
            {n.message}
          </Text>
        ) : null}
      </View>

      <View style={st.trailing}>
        {when ? <Text style={st.when}>{when}</Text> : null}
        {actionable ? (
          <Icon name="chevron.right" tintColor={C.textFaint} size={18} />
        ) : null}
      </View>
    </View>
  );

  // A row with nowhere to go is rendered as plain content, never as a
  // Pressable — a tappable-looking row that does nothing reads as a bug.
  if (!actionable) return body;

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => pressed && { opacity: 0.6 }}
      accessibilityRole="button"
      accessibilityLabel={`${n.title}. ${n.message}${n.is_read ? "" : ". Unread"}`}
    >
      {body}
    </Pressable>
  );
}

function SkeletonRow({ st }: { st: ReturnType<typeof buildStyles> }) {
  return (
    <View style={st.row}>
      <View style={[st.bar, st.skeleton]} />
      <View style={st.dotSlot} />
      <View style={st.content}>
        <View style={[st.skeleton, { height: 14, width: "55%", borderRadius: 4 }]} />
        <View style={[st.skeleton, { height: 12, width: "85%", borderRadius: 4, marginTop: 8 }]} />
      </View>
    </View>
  );
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    titleRow: { flexDirection: "row", alignItems: "center", gap: S.sm, flexShrink: 1 },
    screenTitle: { fontFamily: FONT.bold, fontSize: 20, color: C.text, flexShrink: 1 },
    countBadge: {
      minWidth: 24, height: 22, paddingHorizontal: 7, borderRadius: 11,
      backgroundColor: C.accentSoft, alignItems: "center", justifyContent: "center",
    },
    countText: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.accent },
    headerAction: {
      fontFamily: FONT.semibold, fontSize: 14, color: C.accent,
    },

    tabsBar: {
      borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.border,
    },
    tabs: { paddingHorizontal: GUTTER, gap: S.xl },
    tab: {
      paddingTop: S.md, paddingBottom: S.md - 2, paddingHorizontal: 2,
      borderBottomWidth: 2, borderBottomColor: "transparent",
      minHeight: 44, justifyContent: "center",
    },
    tabOn: { borderBottomColor: C.accent },
    tabText: { fontFamily: FONT.medium, fontSize: 14.5, color: C.textFaint },
    tabTextOn: { fontFamily: FONT.semibold, color: C.accent },

    groupTitle: {
      fontFamily: FONT.semibold, fontSize: 12, letterSpacing: 0.8,
      textTransform: "uppercase",
      color: C.textFaint,
      marginTop: S.xl, marginBottom: S.xs,
    },
    filterEmpty: {
      fontFamily: FONT.regular, fontSize: 13.5, color: C.textDim,
      textAlign: "center", marginTop: S.xxl,
    },

    row: {
      flexDirection: "row", alignItems: "flex-start",
      paddingVertical: S.lg,
      borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.border,
    },
    bar: { width: 3, height: 38, borderRadius: 2, marginTop: 1 },
    dotSlot: { width: 26, alignItems: "center", paddingTop: 7 },
    dot: { width: 8, height: 8, borderRadius: 4 },
    content: { flex: 1, minWidth: 0 },
    title: { fontFamily: FONT.semibold, fontSize: 15.5, lineHeight: 21, color: C.text },
    message: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 19, color: C.textDim, marginTop: 3,
    },
    trailing: {
      flexDirection: "row", alignItems: "center", gap: 6,
      marginLeft: S.md, paddingTop: 1,
    },
    when: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint },
    skeleton: { backgroundColor: C.border },
  });
}
