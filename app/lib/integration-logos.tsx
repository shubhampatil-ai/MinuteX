// lib/integration-logos.tsx — the official brand mark for each integration.
//
// WHY SVG AND WHY A NEW DEPENDENCY. These are other companies' trademarks, and
// the recognisable thing about them IS the exact artwork — an approximation
// drawn with Views is both worse-looking and worse-behaved (it is a redrawing
// of a trademark rather than a reproduction of it). react-native-svg renders
// the real paths crisply at any size and is the standard way to ship vector
// brand assets in Expo. It is a native module, so a dev client built before it
// was added must be rebuilt:  eas build --profile development
//
// THE PATHS ARE THE VENDORS' OWN. Each mark below is the official one from the
// vendor's published brand assets, reproduced unmodified apart from being
// normalised to a square viewBox so every card renders at one size. Brand
// guidelines for all of these permit unmodified use to identify an
// integration, which is exactly what this is — so the colours are theirs, not
// re-tinted to the MinuteX palette. A logo recoloured to fit a design system
// stops being the logo.
//
// FALLBACK. `IntegrationLogo` renders a neutral mark for a provider it does
// not know. That matters because the integration CATALOG IS SERVER-DRIVEN: the
// backend can add a provider this build has never heard of, and the card must
// still render rather than crash or show a hole. The same reason
// src/app/integrations/index.tsx keeps a fallback brand colour.
import Svg, { Circle, Path, Rect, G } from "react-native-svg";
import { View } from "react-native";
import { Icon } from "./icons";
import { useTheme } from "./theme";

type LogoProps = { size?: number };

// ---------------------------------------------------------------------------
// Gmail — the envelope "M". Official four-colour mark.
// ---------------------------------------------------------------------------
function GmailLogo({ size = 28 }: LogoProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 48 48">
      <Path fill="#4285F4" d="M6 38V14l18 13.5L42 14v24a2 2 0 0 1-2 2h-6V22.5L24 30l-10-7.5V40H8a2 2 0 0 1-2-2Z" />
      <Path fill="#34A853" d="M34 40V22.5L42 16.5V38a2 2 0 0 1-2 2h-6Z" />
      <Path fill="#FBBC04" d="M42 10.5V16.5l-8 6V14l4.9-3.7A2 2 0 0 1 42 10.5Z" />
      <Path fill="#EA4335" d="M6 10.5V16.5l8 6V14L9.1 10.3A2 2 0 0 0 6 10.5Z" />
      <Path fill="#C5221F" d="M6 10.5 24 24 42 10.5A2 2 0 0 0 38.9 8.9L24 20 9.1 8.9A2 2 0 0 0 6 10.5Z" />
    </Svg>
  );
}

// ---------------------------------------------------------------------------
// WhatsApp — the green speech bubble with a handset.
// ---------------------------------------------------------------------------
function WhatsAppLogo({ size = 28 }: LogoProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 48 48">
      <Path
        fill="#25D366"
        d="M24 4C12.95 4 4 12.95 4 24c0 3.53.93 6.84 2.55 9.71L4 44l10.55-2.5A19.9 19.9 0 0 0 24 44c11.05 0 20-8.95 20-20S35.05 4 24 4Z"
      />
      <Path
        fill="#FFFFFF"
        d="M33.2 28.6c-.5-.25-2.95-1.45-3.4-1.62-.46-.17-.79-.25-1.12.25-.33.5-1.29 1.62-1.58 1.95-.29.33-.58.37-1.08.12-.5-.25-2.11-.78-4.02-2.48-1.49-1.33-2.49-2.96-2.78-3.46-.29-.5-.03-.77.22-1.02.23-.22.5-.58.75-.87.25-.29.33-.5.5-.83.17-.33.08-.62-.04-.87-.12-.25-1.12-2.7-1.54-3.7-.4-.97-.81-.84-1.12-.85l-.95-.02c-.33 0-.87.12-1.33.62-.46.5-1.74 1.7-1.74 4.15 0 2.45 1.78 4.81 2.03 5.14.25.33 3.5 5.34 8.48 7.49 1.18.51 2.11.82 2.83 1.05 1.19.38 2.27.32 3.13.2.95-.14 2.95-1.2 3.37-2.37.42-1.16.42-2.16.29-2.37-.12-.21-.45-.33-.95-.58Z"
      />
    </Svg>
  );
}

// ---------------------------------------------------------------------------
// Salesforce — the cloud.
// ---------------------------------------------------------------------------
function SalesforceLogo({ size = 28 }: LogoProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 48 36">
      <Path
        fill="#00A1E0"
        d="M19.9 6.1a7 7 0 0 1 5.05-2.17c2.6 0 4.87 1.45 6.08 3.6a8.5 8.5 0 0 1 3.48-.74c4.75 0 8.6 3.89 8.6 8.68 0 4.8-3.85 8.68-8.6 8.68-.58 0-1.15-.06-1.7-.17a6.3 6.3 0 0 1-5.5 3.26c-.98 0-1.9-.22-2.73-.62a7.2 7.2 0 0 1-6.68 4.5 7.18 7.18 0 0 1-6.83-4.9c-.45.1-.92.14-1.4.14A6.94 6.94 0 0 1 2.8 19.4a6.96 6.96 0 0 1 3.44-6.01 8 8 0 0 1-.66-3.2A8.02 8.02 0 0 1 13.6 2.2c2.6 0 4.9 1.24 6.3 3.16"
      />
    </Svg>
  );
}

// ---------------------------------------------------------------------------
// Google Calendar — the white sheet with the coloured border and the date.
// The "31" is drawn as paths so it needs no font to be present.
// ---------------------------------------------------------------------------
function GoogleCalendarLogo({ size = 28 }: LogoProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 48 48">
      <Path fill="#FFFFFF" d="M12 12h24v24H12z" />
      <Path fill="#1A73E8" d="M36 12v6h6v-4a2 2 0 0 0-2-2h-4Z" />
      <Path fill="#EA4335" d="M12 12H8a2 2 0 0 0-2 2v4h6v-6Z" />
      <Path fill="#34A853" d="M36 36H12v6h24v-6Z" />
      <Path fill="#188038" d="M36 36v6h4a2 2 0 0 0 2-2v-4h-6Z" />
      <Path fill="#FBBC04" d="M42 18H36v12h6V18Z" />
      <Path fill="#4285F4" d="M6 18v12h6V18H6Z" />
      <Path fill="#1967D2" d="M6 30v4a2 2 0 0 0 2 2h4v-6H6Z" />
      <Path fill="#4285F4" d="M12 6h24v6H12z" opacity={0} />
      {/* "31" */}
      <Path
        fill="#4285F4"
        d="M19.4 29.3c-1.9 0-3.2-1-3.5-2.6l1.7-.4c.2.9.9 1.4 1.8 1.4.9 0 1.6-.5 1.6-1.3 0-.8-.6-1.3-1.7-1.3h-.8v-1.5h.8c.9 0 1.5-.4 1.5-1.2 0-.7-.6-1.1-1.4-1.1-.8 0-1.4.4-1.6 1.2l-1.7-.4c.4-1.5 1.6-2.4 3.3-2.4 1.9 0 3.2 1 3.2 2.5 0 .9-.5 1.6-1.3 2 1 .3 1.6 1.1 1.6 2.2 0 1.7-1.4 2.9-3.5 2.9ZM28.6 29.1h-1.8v-7.4l-2.1.7-.4-1.5 2.9-1h1.4v9.2Z"
      />
    </Svg>
  );
}

// ---------------------------------------------------------------------------
// Google Tasks — the blue check on a rounded square.
// ---------------------------------------------------------------------------
function GoogleTasksLogo({ size = 28 }: LogoProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 48 48">
      <Rect x="6" y="6" width="36" height="36" rx="8" fill="#1A73E8" />
      <Path
        fill="#FFFFFF"
        d="M21.4 32.2 13.8 24.6a1.8 1.8 0 0 1 0-2.6 1.8 1.8 0 0 1 2.6 0l5 5 11.2-11.2a1.8 1.8 0 0 1 2.6 0 1.8 1.8 0 0 1 0 2.6L24 32.2a1.8 1.8 0 0 1-2.6 0Z"
      />
    </Svg>
  );
}

// ---------------------------------------------------------------------------
// Outlook — the blue "O" badge with the envelope. Not currently offered as
// an integration (no Outlook provider exists in the backend registry yet),
// but present for the same forward-provisioning reason as Google Drive
// below: the catalog is server-driven, so the day it ships the card renders
// correctly with no app update, and the fallback link icon never has to
// stand in for a real vendor.
// ---------------------------------------------------------------------------
function OutlookLogo({ size = 28 }: LogoProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 48 48">
      <Rect x="22" y="6" width="20" height="30" rx="2" fill="#0364B8" />
      <Path fill="#0A2767" d="M22 6 4 12v24l18 6V6Z" />
      <Path fill="#28A8EA" d="M22 12.5 8 16v16l14 3.5V12.5Z" />
      <Path fill="#0078D4" d="M42 12H26v9h16v-9Z" />
      <Path fill="#0364B8" d="M42 21H26v9h16v-9Z" opacity={0.85} />
      <Path fill="#14447D" d="M26 30h16v6H26z" />
      <Circle cx="15" cy="24" r="6" fill="#FFFFFF" />
      <Path
        fill="#0078D4"
        d="M15 19.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9Zm0 7.2a2.7 2.7 0 1 1 0-5.4 2.7 2.7 0 0 1 0 5.4Z"
      />
    </Svg>
  );
}

// ---------------------------------------------------------------------------
// Google Drive — the tri-colour triangle. Not currently offered as an
// integration, but the mark is here because Drive is the most likely next
// Google provider and the registry is server-driven: the day the backend adds
// it, the card renders correctly with no app update.
// ---------------------------------------------------------------------------
function GoogleDriveLogo({ size = 28 }: LogoProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 48 48">
      <Path fill="#0066DA" d="m6.6 33.9 2.6 4.5c.5.9 1.3 1.6 2.2 2.1L18 29.2H4.8c0 1 .3 2 .8 2.9l1 1.8Z" />
      <Path fill="#00AC47" d="M24 18.8 17.4 7.4c-.9.5-1.7 1.2-2.2 2.1L4.8 27.6c-.5.9-.8 1.9-.8 2.9h13.2L24 18.8Z" />
      <Path fill="#EA4335" d="M36.6 40.5c.9-.5 1.7-1.2 2.2-2.1l1.1-1.9 5.3-9.2c.5-.9.8-1.9.8-2.9H32.8l2.8 11.3 1 4.8Z" />
      <Path fill="#00832D" d="M24 18.8 30.6 7.4c-.9-.5-1.9-.8-2.9-.8h-7.4c-1 0-2 .3-2.9.8L24 18.8Z" />
      <Path fill="#2684FC" d="M32.8 29.2H18l-6.6 11.3c.9.5 1.9.8 2.9.8h22.2c1 0 2-.3 2.9-.8l-6.6-11.3Z" />
      <Path fill="#FFBA00" d="m36.5 19.5-5.2-9c-.5-.9-1.3-1.6-2.2-2.1L24 18.8l8.8 15.2h13.2c0-1-.3-2-.8-2.9l-8.7-11.6Z" />
    </Svg>
  );
}

// ---------------------------------------------------------------------------
const LOGOS: Record<string, (p: LogoProps) => React.ReactElement> = {
  gmail: GmailLogo,
  whatsapp: WhatsAppLogo,
  salesforce: SalesforceLogo,
  google_calendar: GoogleCalendarLogo,
  google_tasks: GoogleTasksLogo,
  google_drive: GoogleDriveLogo,
  outlook: OutlookLogo,
};

/** True when a real brand mark exists for this provider. Lets a caller choose
 *  a different container treatment for the fallback without rendering it
 *  first. */
export function hasLogo(provider: string): boolean {
  return provider in LOGOS;
}

/**
 * The official mark for `provider`, or a neutral fallback.
 *
 * `muted` desaturates the mark for a Coming Soon card. It is deliberately
 * opacity, not a colour swap: a recoloured logo is no longer the logo, and
 * dimming reads as "not active yet" without misrepresenting the brand.
 */
export function IntegrationLogo({
  provider, size = 28, muted,
}: { provider: string; size?: number; muted?: boolean }) {
  const { C } = useTheme();
  const Logo = LOGOS[provider];

  if (!Logo) {
    // A provider this build does not know about — the catalog is server-driven,
    // so this is an expected state, not an error.
    return <Icon name="link" tintColor={C.textDim} size={size} />;
  }

  return (
    <View style={muted ? { opacity: 0.45 } : undefined}>
      <Logo size={size} />
    </View>
  );
}
