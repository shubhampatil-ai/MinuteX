// lib/theme.tsx — MinuteX by Exceller Tech · "Workspace" design system.
// Single source of truth for brand, colors, and type/spacing scales.
//
// Palette rationale: clean, modern, app-native — white surfaces, soft gray
// backgrounds, a single confident blue accent for every primary action and
// active state, rounded cards with soft shadows instead of hairline rules.
// This replaces the earlier "Briefing" editorial theme (warm paper, serif
// headlines, sienna accent) entirely: MinuteX is now a premium AI Meeting
// Workspace, not a printed brief.
//
// TYPE — one family, every job. No serif. Weight carries hierarchy instead of
// a second typeface — headlines are bold/semibold system sans, body is
// regular. Numerals (timers, durations) use a monospaced face for alignment.
//
// IMPORTANT (React Native): custom fonts do NOT respond to `fontWeight`. You
// must name the exact family per weight. buildTypeScale() does that mapping —
// never write `fontWeight` alongside `fontFamily` in a screen.
import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { Appearance } from "react-native";
import { store } from "./storage";

export const APP_NAME = "MinuteX";
export const COMPANY = "Exceller Tech";
export const TAGLINE = "Your AI meeting workspace.";

// ---------------------------------------------------------------------------
// Font families. These strings must match what expo-font registered in
// src/app/_layout.tsx.
// ---------------------------------------------------------------------------
export const FONT = {
  regular: "PlusJakartaSans_400Regular",
  medium: "PlusJakartaSans_500Medium",
  semibold: "PlusJakartaSans_600SemiBold",
  bold: "PlusJakartaSans_700Bold",
  extrabold: "PlusJakartaSans_800ExtraBold",
  mono: "JetBrainsMono_500Medium",
} as const;

export type ThemeMode = "dark" | "light";
export type ColorScale = {
  bg: string; surface: string; surface2: string; surfaceRaised: string; border: string; borderStrong: string;
  primary: string; primarySoft: string; primaryStrong: string;
  // Kept for API compatibility with call sites that still use <Gradient>.
  gradient: [string, string];
  accent: string; accentSoft: string;
  danger: string; dangerSoft: string; warn: string; warnSoft: string; success: string; successSoft: string;
  text: string; textDim: string; textFaint: string; textOnPrimary: string;
  // Translucent surface + border for floating bars / glass panels.
  glass: string; glassBorder: string;
  // Shadow color for card elevation (RN shadow props want a solid color).
  shadow: string;
  // Speaker / avatar colours. Warm, distinguishable, never clash with primary.
  speakers: string[];
};

const LIGHT: ColorScale = {
  bg: "#F6F7FB",          // app background — cool, very light gray
  surface: "#FFFFFF",     // card stock
  surface2: "#F0F2F7",    // inset / skeleton / chip background
  surfaceRaised: "#FFFFFF",
  border: "#E6E9F0",      // hairline rule / card border
  borderStrong: "#D8DCE6",
  primary: "#3E6BFF",     // confident blue — every primary action & active state
  primarySoft: "#EAF0FF",
  primaryStrong: "#2E52D6",
  gradient: ["#3E6BFF", "#7C5CFF"],
  accent: "#7C5CFF",      // violet — AI / assistant accent, pairs with primary
  accentSoft: "#F1EEFF",
  danger: "#E5484D",
  dangerSoft: "#FDECEC",
  warn: "#F5A623",
  warnSoft: "#FEF3E0",
  success: "#1FA972",
  successSoft: "#E7F8F0",
  text: "#12131A",
  textDim: "#5B6072",
  textFaint: "#9297A8",
  textOnPrimary: "#FFFFFF",
  glass: "rgba(255,255,255,0.82)",
  glassBorder: "rgba(18,19,26,0.06)",
  shadow: "#1A2033",
  speakers: ["#3E6BFF", "#1FA972", "#7C5CFF", "#F5A623", "#E5484D", "#0EA5B7"],
};

const DARK: ColorScale = {
  bg: "#0B0D14",
  surface: "#151823",
  surface2: "#1D2130",
  surfaceRaised: "#1D2130",
  border: "#262B3B",
  borderStrong: "#323852",
  primary: "#5C86FF",
  primarySoft: "#1A2340",
  primaryStrong: "#7C9EFF",
  gradient: ["#5C86FF", "#9B82FF"],
  accent: "#9B82FF",
  accentSoft: "#231C3D",
  danger: "#FF6B6E",
  dangerSoft: "#2E1A1C",
  warn: "#FFB84D",
  warnSoft: "#332411",
  success: "#3DCB93",
  successSoft: "#14291F",
  text: "#F3F4F8",
  textDim: "#9BA0B4",
  textFaint: "#666C82",
  textOnPrimary: "#FFFFFF",
  glass: "rgba(21,24,35,0.82)",
  glassBorder: "rgba(255,255,255,0.08)",
  shadow: "#000000",
  speakers: ["#5C86FF", "#3DCB93", "#9B82FF", "#FFB84D", "#FF6B6E", "#41C6DA"],
};

// Radii: soft, app-native cards. `pill` is a real pill now (badges, chips,
// segmented control, FAB).
export const R = { sm: 8, md: 12, card: 16, lg: 20, xl: 28, pill: 999 };

// Spacing scale (4pt grid) — prefer S.md over magic numbers.
export const S = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 };

// Soft elevation — cards float gently above the page. Use with C.shadow.
export const ELEV = {
  sm: { shadowOffset: { width: 0, height: 1 }, shadowOpacity: 0.06, shadowRadius: 3, elevation: 1 },
  md: { shadowOffset: { width: 0, height: 4 }, shadowOpacity: 0.08, shadowRadius: 12, elevation: 3 },
  lg: { shadowOffset: { width: 0, height: 10 }, shadowOpacity: 0.12, shadowRadius: 24, elevation: 8 },
};

// Monospaced tabular figures — timers, battery %, durations, counts.
export const TABULAR: { fontFamily: string; fontVariant: ["tabular-nums"] } = {
  fontFamily: FONT.mono,
  fontVariant: ["tabular-nums"],
};

// Caps micro-label — section headings and metadata labels.
export const CAPS = {
  fontFamily: FONT.semibold,
  fontSize: 11,
  letterSpacing: 0.6,
  textTransform: "uppercase" as const,
};

function buildTypeScale(C: ColorScale) {
  return {
    // Screen title / masthead ("The Desk", meeting title)
    display: { fontFamily: FONT.extrabold, fontSize: 30, lineHeight: 36, color: C.text, letterSpacing: -0.4 },
    // Meeting detail headline
    headline: { fontFamily: FONT.extrabold, fontSize: 22, lineHeight: 28, color: C.text, letterSpacing: -0.3 },
    // List row headline
    headlineSm: { fontFamily: FONT.bold, fontSize: 17, lineHeight: 22, color: C.text },
    // Pull quotes / empty-state statements
    quote: { fontFamily: FONT.semibold, fontSize: 17, lineHeight: 24, color: C.text },
    // Big statistics
    numeral: { fontFamily: FONT.extrabold, fontSize: 26, lineHeight: 30, color: C.text },

    h1: { fontFamily: FONT.bold, fontSize: 20, color: C.text, letterSpacing: -0.2 },
    h2: { fontFamily: FONT.bold, fontSize: 17, color: C.text },
    h3: { fontFamily: FONT.semibold, fontSize: 15, color: C.text },
    body: { fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.text },
    bodyLead: { fontFamily: FONT.regular, fontSize: 15.5, lineHeight: 24, color: C.text },
    bodyDim: { fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim },
    label: { ...CAPS, color: C.textDim },
    labelDim: { ...CAPS, color: C.textFaint },
    caption: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint },
  };
}

function buildStatusScale(C: ColorScale) {
  return {
    ok: C.success,
    recording: C.danger,
    connecting: C.primary,
    offline: C.textFaint,
    error: C.danger,
  };
}

type ThemeValue = {
  mode: ThemeMode;
  isSystemDefault: boolean;
  C: ColorScale;
  T: ReturnType<typeof buildTypeScale>;
  STATUS: ReturnType<typeof buildStatusScale>;
  setMode: (m: ThemeMode | "system") => void;
};

const ThemeCtx = createContext<ThemeValue | null>(null);
const THEME_KEY = "minutex.theme"; // "dark" | "light" | "system"

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [pref, setPref] = useState<ThemeMode | "system">("system");
  const [systemScheme, setSystemScheme] = useState<ThemeMode>(
    Appearance.getColorScheme() === "dark" ? "dark" : "light"
  );
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    (async () => {
      const saved = await store.getItemAsync(THEME_KEY);
      if (saved === "dark" || saved === "light" || saved === "system") setPref(saved);
      setLoaded(true);
    })();
  }, []);

  useEffect(() => {
    const sub = Appearance.addChangeListener(({ colorScheme }) => {
      setSystemScheme(colorScheme === "dark" ? "dark" : "light");
    });
    return () => sub.remove();
  }, []);

  const setMode = (m: ThemeMode | "system") => {
    setPref(m);
    store.setItemAsync(THEME_KEY, m).catch(() => {});
  };

  const mode: ThemeMode = pref === "system" ? systemScheme : pref;
  const C = mode === "dark" ? DARK : LIGHT;

  const value = useMemo<ThemeValue>(() => ({
    mode,
    isSystemDefault: pref === "system",
    C,
    T: buildTypeScale(C),
    STATUS: buildStatusScale(C),
    setMode,
  }), [mode, pref]);

  if (!loaded) return null;

  return <ThemeCtx.Provider value={value}>{children}</ThemeCtx.Provider>;
}

export function useTheme(): ThemeValue {
  const ctx = useContext(ThemeCtx);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}

export type { ColorScale as ColorScaleType };
