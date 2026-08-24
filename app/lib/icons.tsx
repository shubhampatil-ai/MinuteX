// lib/icons.tsx — cross-platform icon for the MinuteX design system.
//
// expo-symbols' <SymbolView name="..."> with a plain string renders SF Symbols
// on iOS but NOTHING on Android — Android/web require a per-platform name
// object ({ android: <MaterialSymbol> }). Every icon in the app goes through
// this <Icon> instead: it keeps the SF Symbols vocabulary the codebase already
// uses and maps each name to its Material Symbols equivalent for Android/web.
//
// Adding an icon: pick the SF name, add its Material pair here. TypeScript
// narrows `name` to the mapped set, so an unmapped name is a compile error —
// not an invisibly-missing glyph on one platform.
import React from "react";
import type { ColorValue, ViewStyle } from "react-native";
import { SymbolView } from "expo-symbols";

// SF Symbol -> Material Symbol. Grouped by role for maintainability.
const MAP = {
  // Brand / audio
  "waveform": "graphic_eq",
  "mic.fill": "mic",
  "play.fill": "play_arrow",
  "pause.fill": "pause",
  "stop.fill": "stop",
  "gobackward.15": "replay_10",
  "goforward.15": "forward_10",
  "speedometer": "speed",
  "headphones": "headphones",
  "record.circle": "radio_button_checked",
  "timer": "timer",

  // Navigation / chrome
  "folder.fill": "folder",
  "house.fill": "home",
  "doc.text.fill": "description",
  "cpu.fill": "memory",
  "person.fill": "person",
  "chevron.right": "chevron_right",
  "chevron.left": "chevron_left",
  "chevron.down": "keyboard_arrow_down",
  "xmark": "close",
  "xmark.circle.fill": "cancel",
  "magnifyingglass": "search",
  "ellipsis": "more_horiz",
  // The Briefing's tab set: the Desk is filed paper, not a house, and the
  // Actions tab is a checked-off promise.
  "newspaper.fill": "article",
  "checkmark.circle": "task_alt",
  "arrow.left": "arrow_back",
  "arrow.right": "arrow_forward",
  "arrow.up": "arrow_upward",
  "clock.arrow.circlepath": "history",
  "tray.fill": "inbox",

  // Status / feedback
  "checkmark": "check",
  "checkmark.circle.fill": "check_circle",
  "checkmark.seal.fill": "verified",
  "exclamationmark.triangle.fill": "warning",
  "hourglass": "hourglass_top",
  "info.circle": "info",
  "questionmark.circle": "help",
  "clock": "schedule",
  "calendar": "event",

  // Auth / account
  "envelope": "mail",
  "lock": "lock",
  "lock.fill": "lock",
  "lock.shield": "security",
  "eye": "visibility",
  "eye.slash": "visibility_off",
  "key": "vpn_key",
  "rectangle.portrait.and.arrow.right": "logout",
  "crown.fill": "workspace_premium",
  "bolt.fill": "bolt",

  // Device / connectivity
  "dot.radiowaves.left.and.right": "sensors",
  "antenna.radiowaves.left.and.right": "signal_cellular_alt",
  "wifi": "wifi",
  "wifi.slash": "wifi_off",
  "battery.100": "battery_full",
  "battery.25": "battery_low",
  "location": "location_on",
  "bell.badge": "notifications_active",
  "bell.fill": "notifications",
  "link": "link",
  "externaldrive.fill": "storage",
  "externaldrive.fill.badge.checkmark": "verified",
  "arrow.triangle.2.circlepath": "sync",
  "square.and.arrow.up": "ios_share",
  "tray.and.arrow.up.fill": "cloud_upload",
  "icloud.and.arrow.up": "backup",
  "iphone": "smartphone",
  "arrow.down.circle": "download",

  // AI / content
  "sparkles": "auto_awesome",
  "doc.on.doc": "content_copy",
  "text.alignleft": "subject",
  "list.bullet": "format_list_bulleted",
  "checklist": "checklist",
  "lightbulb.fill": "lightbulb",
  "person.2.fill": "groups",
  "bubble.left.and.bubble.right.fill": "chat",
  "text.bubble": "chat_bubble",
  "translate": "translate",
  "bookmark": "bookmark",
  "pencil": "edit",
  "trash": "delete",
  "folder": "folder_open",
  // Folder category icons (lib/folder-appearance.tsx maps stored folder
  // tokens onto these). Small, deliberate set — a folder icon is a
  // glanceable category hint, not a sticker library.
  "briefcase.fill": "work",
  "flag.fill": "flag",
  "star.fill": "star",
  "cart.fill": "shopping_cart",
  "phone.fill": "call",
  "slider.horizontal.3": "tune",
  "brain.head.profile": "psychology",
  "shield": "shield",
  "chart.line.uptrend.xyaxis": "stacked_line_chart",
  // "Next time, put the phone on the table" — the failed-brief advice rows.
  "table.furniture": "table_restaurant",

  // AI Meeting Workspace. The highlight sections each need a distinct mark,
  // and the document/export actions need their own vocabulary.
  "gavel": "gavel",                                  // decisions
  "calendar.badge.clock": "event_upcoming",          // deadlines
  "number": "numbers",                               // important numbers
  "questionmark.bubble": "quiz",                     // open questions
  "exclamationmark.triangle": "report_problem",      // risks
  "doc.richtext": "article",                         // a generated document
  "doc.badge.plus": "post_add",                      // generate a document
  "arrow.clockwise": "refresh",                      // regenerate
  "square.and.pencil": "edit_note",                  // edit a document
  "paperplane.fill": "send",                         // send a chat message
  "envelope.badge": "forward_to_inbox",              // follow-up email
  "message.fill": "sms",                             // WhatsApp update
  "indianrupeesign.circle": "currency_rupee",        // budget
  "building.2.fill": "business",                     // sales / customer
  "ruler": "straighten",                             // site visit measurements
  "list.number": "format_list_numbered",             // minutes / numbered
  "clock.badge.checkmark": "published_with_changes", // timeline
  "chevron.up": "keyboard_arrow_up",                 // collapse
  "square.grid.2x2": "grid_view",                    // documents grid
  "arrow.down.doc": "file_download",                 // export / PDF
  "square": "check_box_outline_blank",               // empty task checkbox
  "checkmark.square.fill": "check_box",              // completed task checkbox
  "play.circle.fill": "play_circle",                 // hero player play
  "pause.circle.fill": "pause_circle",               // hero player pause
  "wand.and.stars": "auto_fix_high",                 // assistant / generate
  "plus": "add",                                     // add / attach in composer

  // CRM integrations
  "cloud.fill": "cloud",                             // Salesforce connection

  // Settings
  "gearshape.fill": "settings",
  "globe": "language",
  "sun.max.fill": "light_mode",
  "moon.fill": "dark_mode",
  "circle.lefthalf.filled": "contrast",
  "hand.raised.fill": "privacy_tip",
} as const;

export type IconName = keyof typeof MAP;

export function Icon({
  name, size = 24, tintColor, style,
}: {
  name: IconName;
  size?: number;
  tintColor?: ColorValue;
  style?: ViewStyle;
}) {
  return (
    <SymbolView
      name={{ ios: name as any, android: MAP[name] as any, web: MAP[name] as any }}
      size={size}
      tintColor={tintColor}
      style={style}
    />
  );
}
