// lib/notification-bell.tsx — the header bell and its unread badge.
//
// A component rather than markup inside the home screen because the bell is
// an ENTRY POINT, not a piece of that screen: it reads its count from the
// shared context and pushes to /notifications, so any screen that wants it
// can render <NotificationBell /> and get the same behaviour. Today only the
// MinuteX masthead does.
//
// WHY IT REFRESHES ON FOCUS. The count is loaded once when the provider
// mounts. Coming BACK to a screen (from the centre, from a task, from a
// meeting that just finished processing) is exactly when it can be stale, and
// a focus refresh is what makes the badge settle without polling on a timer —
// which would keep firing on every screen in the app for a number most of them
// never show.
import { useCallback } from "react";
import { Pressable, Text, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { Icon } from "./icons";
import { FONT, R, useTheme } from "./theme";
import { useNotifications } from "./notification-center";

// Above this the badge shows "9+" instead of the number. A two-digit count in
// a 18pt disc either overflows or shrinks the type below legibility, and the
// exact number stops being the point long before then — "a lot" is the
// information.
const BADGE_MAX = 9;

export function NotificationBell({ size = 22 }: { size?: number }) {
  const { C } = useTheme();
  const router = useRouter();
  const { unreadCount, refreshCount } = useNotifications();

  useFocusEffect(
    useCallback(() => {
      refreshCount();
    }, [refreshCount])
  );

  const has = unreadCount > 0;
  const label = unreadCount > BADGE_MAX ? `${BADGE_MAX}+` : String(unreadCount);

  return (
    <Pressable
      onPress={() => router.push("/notifications" as never)}
      hitSlop={10}
      style={({ pressed }) => [
        {
          width: 40, height: 40, borderRadius: R.pill,
          alignItems: "center", justifyContent: "center",
        },
        pressed && { opacity: 0.6 },
      ]}
      accessibilityRole="button"
      // The count is IN the label, not only in the badge: a screen reader user
      // gets the same information the badge conveys visually.
      accessibilityLabel={
        has
          ? `Notifications, ${unreadCount} unread`
          : "Notifications"
      }
    >
      {/* bell.badge is the filled-with-dot glyph — used only when there is
          something to see, so the icon itself carries the state on platforms
          where the badge is hard to notice. */}
      <Icon
        name={has ? "bell.badge" : "bell.fill"}
        tintColor={has ? C.primary : C.textDim}
        size={size}
      />
      {has ? (
        <View
          pointerEvents="none"
          style={{
            position: "absolute", top: 4, right: 3,
            minWidth: 18, height: 18, borderRadius: 9,
            paddingHorizontal: 4,
            backgroundColor: C.danger,
            alignItems: "center", justifyContent: "center",
            // A ring in the page colour so the badge reads as a separate
            // object where it overlaps the bell rather than merging with it.
            borderWidth: 2, borderColor: C.bg,
          }}
        >
          <Text
            style={{
              fontFamily: FONT.bold, fontSize: 10, lineHeight: 13,
              color: C.textOnPrimary,
            }}
            numberOfLines={1}
          >
            {label}
          </Text>
        </View>
      ) : null}
    </Pressable>
  );
}
