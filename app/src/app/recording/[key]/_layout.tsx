// src/app/recording/[key]/_layout.tsx — mounts MeetingProvider ONCE for every
// screen nested under this meeting: Overview+Transcript (index), Assistant,
// Task Detail, Assign To and Notify. One provider instance means assigning a
// task on Assign To and navigating back to Task Detail sees the update
// immediately — each screen no longer fetches/holds its own copy.
import { Stack, useLocalSearchParams } from "expo-router";
import { useTheme, FONT } from "../../../../lib/theme";
import { MeetingProvider } from "../../../../lib/meeting-context";

export default function MeetingLayout() {
  const { C } = useTheme();
  const params = useLocalSearchParams<{ key: string | string[] }>();
  const key = Array.isArray(params.key) ? params.key.join("/") : (params.key ?? "");

  return (
    <MeetingProvider meetingKey={key}>
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: C.bg },
          headerTintColor: C.text,
          headerTitleStyle: { color: C.text, fontFamily: FONT.semibold, fontSize: 16 },
          headerShadowVisible: false,
          contentStyle: { backgroundColor: C.bg },
        }}
      >
        <Stack.Screen name="index" options={{ title: "Meeting" }} />
        <Stack.Screen name="assistant" options={{ title: "Assistant", presentation: "modal" }} />
        <Stack.Screen name="task/index" options={{ title: "Tasks" }} />
        <Stack.Screen name="task/[taskId]/index" options={{ title: "Task Detail" }} />
        <Stack.Screen name="task/[taskId]/assign" options={{ title: "Assign To" }} />
        <Stack.Screen name="task/[taskId]/notify" options={{ title: "Notify Assignee" }} />
      </Stack>
    </MeetingProvider>
  );
}
