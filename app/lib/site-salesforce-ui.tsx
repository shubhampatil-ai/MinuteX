// lib/site-salesforce-ui.tsx — small shared pieces for the Salesforce Site
// screens (src/app/site-salesforce/*).
import { useEffect, useRef, useState } from "react";
import { AppState, Text, View } from "react-native";
import { ApiError } from "./api";
import { IN_FLIGHT_STATUSES, siteErrorMessage } from "./site-salesforce-flow";
import { SiteSalesforceDataKind, subscribeSiteSalesforce } from "./site-salesforce-events";
import { FONT, R, useTheme } from "./theme";

// Same conversion lib/ui.tsx uses privately for StatusPill.
function withAlpha(hex: string, alpha: number): string {
  if (hex.startsWith("rgb")) return hex;
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/** Is the Site Salesforce UI switched on for this build? Off unless the build
 *  sets EXPO_PUBLIC_SITE_SALESFORCE=1, so production is unchanged until the
 *  new architecture is enabled on purpose. */
export const SITE_SALESFORCE_ENABLED = process.env.EXPO_PUBLIC_SITE_SALESFORCE === "1";

/** One permission/property flag: "Queryable", "Createable", ... Shown dim
 *  and struck when false, so a missing permission is visible, not hidden. */
export function Flag({ label, on, tone }: { label: string; on: boolean; tone?: "info" | "warn" }) {
  const { C } = useTheme();
  const color = !on ? C.textFaint : tone === "warn" ? C.warn : tone === "info" ? C.primary : C.success;
  return (
    <View style={{
      borderRadius: R.pill, paddingHorizontal: 8, paddingVertical: 3,
      backgroundColor: withAlpha(color, on ? 0.12 : 0.06), marginRight: 6, marginTop: 6,
    }}>
      <Text style={{
        fontFamily: FONT.bold, fontSize: 10.5, color,
        textDecorationLine: on ? "none" : "line-through",
      }}>
        {label}
      </Text>
    </View>
  );
}

export function FlagRow({ children }: { children: React.ReactNode }) {
  return <View style={{ flexDirection: "row", flexWrap: "wrap" }}>{children}</View>;
}

/** A user-facing message for an error from these routes: the Site flow's
 *  own wording for codes a tester can act on (site-salesforce-flow.ts), else
 *  the server's message, which is already user-facing. */
export function siteSfError(e: unknown): string {
  if (e instanceof ApiError) return siteErrorMessage(e.code, e.message);
  return e instanceof Error ? e.message : "Something went wrong.";
}

export const IN_FLIGHT = new Set(IN_FLIGHT_STATUSES);

export function syncLabel(status?: string): string {
  switch (status) {
    case "PENDING": return "Queued";
    case "SYNCING": return "In progress";
    case "RETRYING": return "Retrying";
    case "SYNCED": return "Done";
    case "FAILED": return "Failed";
    default: return status ? status : "Not run";
  }
}

/** A number that changes whenever any of `kinds` is invalidated
 *  (site-salesforce-events.ts). Put it in a load callback's dependencies and
 *  the screen reloads the moment another screen's sync/connect/disconnect
 *  finishes — no stale list, no pull-to-refresh needed. */
export function useSiteSalesforceVersion(...kinds: SiteSalesforceDataKind[]): number {
  const [version, setVersion] = useState(0);
  const key = kinds.join(",");
  useEffect(() => subscribeSiteSalesforce(key.split(",") as SiteSalesforceDataKind[],
    () => setVersion((v) => v + 1)), [key]);
  return version;
}

/** Calls `tick` every `ms` while `active`; stops as soon as it is not.
 *  Ticks are skipped while the app is in the background: a screen left open
 *  must not keep asking the server every few seconds. */
export function usePolling(active: boolean, tick: () => void, ms = 3000) {
  const ref = useRef(tick);
  useEffect(() => { ref.current = tick; });
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => { if (AppState.currentState === "active") ref.current(); }, ms);
    return () => clearInterval(id);
  }, [active, ms]);
}
