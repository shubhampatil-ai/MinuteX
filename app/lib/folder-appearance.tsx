// lib/folder-appearance.tsx — what a folder's stored color/icon TOKEN looks
// like on screen.
//
// The backend stores a token ("blue", "briefcase"), never a hex value or a raw
// icon name. This file is the only place that turns tokens into pixels, which
// is what makes the palette re-themeable: changing a swatch here restyles every
// existing folder, with no stored data to migrate.
//
// Each color has a light and dark variant. The dark values are lifted, not the
// same hex — the light palette's mid-tones lose too much contrast against the
// dark surface, and a folder swatch has to stay legible as a small 28pt chip.
//
// Tokens are kept in lockstep with FOLDER_COLORS / FOLDER_ICONS in
// lambda-userapi. An unknown token (older app, newer backend) falls back rather
// than rendering nothing — same defensive posture the backend takes.
import type { FolderColor, FolderIconToken } from "./api";
import type { IconName } from "./icons";
import { useTheme } from "./theme";

export const FOLDER_COLOR_TOKENS: FolderColor[] = [
  "slate", "blue", "green", "amber", "teal", "red", "purple",
];

// The swatch the picker shows and the tint a folder renders with. `soft` backs
// the icon chip; `solid` is the swatch itself and the icon's own tint.
type Swatch = { solid: string; soft: string };

const LIGHT: Record<FolderColor, Swatch> = {
  // "slate" is the neutral default — deliberately near-black rather than gray,
  // matching the first swatch in the reference design.
  slate: { solid: "#2A2D3A", soft: "#ECEDF2" },
  blue: { solid: "#5B8DEF", soft: "#E8F0FE" },
  green: { solid: "#4ECB8F", soft: "#E4F8ED" },
  amber: { solid: "#F9B075", soft: "#FEF1E5" },
  teal: { solid: "#5CCBC8", soft: "#E4F7F7" },
  red: { solid: "#F58089", soft: "#FDECEE" },
  purple: { solid: "#C084F5", soft: "#F5EBFE" },
};

const DARK: Record<FolderColor, Swatch> = {
  slate: { solid: "#9BA0B4", soft: "#242836" },
  blue: { solid: "#7BA5FF", soft: "#1A2340" },
  green: { solid: "#5FD9A0", soft: "#12291F" },
  amber: { solid: "#FFC08A", soft: "#33240F" },
  teal: { solid: "#6FDCD9", soft: "#122A2A" },
  red: { solid: "#FF97A0", soft: "#301A1D" },
  purple: { solid: "#CE9BFF", soft: "#261A38" },
};

/** Resolve a folder's color token for the active theme. Unknown tokens fall
 * back to the neutral default rather than rendering an undefined color. */
export function useFolderSwatch(color?: string): Swatch {
  const { mode } = useTheme();
  const table = mode === "dark" ? DARK : LIGHT;
  return table[(color as FolderColor) ?? "slate"] ?? table.slate;
}

/** Same, but callable outside a component (list renderers that already hold
 * the mode). */
export function folderSwatch(color: string | undefined, dark: boolean): Swatch {
  const table = dark ? DARK : LIGHT;
  return table[(color as FolderColor) ?? "slate"] ?? table.slate;
}

// Icon token -> the app's existing SF-Symbol vocabulary. Every value here MUST
// be a real key in lib/icons.tsx's MAP, or the glyph silently fails to render
// on one platform — TypeScript enforces that via IconName.
const ICONS: Record<FolderIconToken, IconName> = {
  folder: "folder",
  briefcase: "briefcase.fill",
  "person.2": "person.2.fill",
  building: "building.2.fill",
  chart: "chart.line.uptrend.xyaxis",
  lightbulb: "lightbulb.fill",
  flag: "flag.fill",
  heart: "checkmark.seal.fill",
  star: "star.fill",
  phone: "phone.fill",
  cart: "cart.fill",
  gear: "gearshape.fill",
};

export const FOLDER_ICON_TOKENS = Object.keys(ICONS) as FolderIconToken[];

/** The renderable icon for a folder's icon token. */
export function folderIcon(token?: string): IconName {
  return ICONS[(token as FolderIconToken) ?? "folder"] ?? ICONS.folder;
}
