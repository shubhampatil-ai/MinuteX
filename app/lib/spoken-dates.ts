// lib/spoken-dates.ts — turning a spoken deadline into a calendar day.
//
// THE PROBLEM. The AI extracts a `deadlines` array of {what, when} per meeting
// (see MeetingHighlights), and `when` is stored EXACTLY AS SPOKEN — "next
// Tuesday", "end of Q3", "15th March", "before the holidays". prompts.py
// forbids the model from resolving these to calendar dates, deliberately:
// the transcript is the record, and a model guessing at "next Tuesday" without
// knowing the meeting's date would be inventing information.
//
// So resolution has to happen here, on the client, where we DO know the
// meeting's date (`recorded_at`) and can anchor a relative phrase to it.
//
// THE RULE THIS FILE FOLLOWS. Resolve only what is unambiguous; return null
// for everything else. A wrong date on a calendar is worse than no date,
// because a calendar is trusted at a glance — someone who sees a deadline on
// Thursday will plan around Thursday. So:
//
//   * "next Tuesday", "15th March", "tomorrow", "end of month" -> resolved,
//     anchored to the meeting date.
//   * "end of Q3", "before the holidays", "soon", "in a few weeks" -> null.
//     These are real deadlines and the user should still SEE them, but they
//     are not days, and the calendar shows them as unplaced rather than
//     pinning them to a date nobody said.
//
// Everything returns a `confidence` so the UI can mark an inferred date as
// inferred. A resolved date is never presented as if the speaker said it.
import { addDays, startOfWeek, toDayKey } from "./task-insights";

export type SpokenResolution = {
  /** YYYY-MM-DD, or "" when the phrase is not a placeable day. */
  dayKey: string;
  /** How the phrase was read.
   *  exact    — an explicit calendar date was spoken ("15th March")
   *  relative — anchored to the meeting date ("next Tuesday", "tomorrow")
   *  none     — a real deadline, but not a day ("end of Q3", "soon") */
  confidence: "exact" | "relative" | "none";
};

const UNPLACEABLE: SpokenResolution = { dayKey: "", confidence: "none" };

const MONTHS: Record<string, number> = {
  jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3,
  apr: 4, april: 4, may: 5, jun: 6, june: 6, jul: 7, july: 7,
  aug: 8, august: 8, sep: 9, sept: 9, september: 9, oct: 10, october: 10,
  nov: 11, november: 11, dec: 12, december: 12,
};

// Monday-first, matching startOfWeek() and the calendar grid.
const WEEKDAYS: Record<string, number> = {
  monday: 0, mon: 0,
  tuesday: 1, tue: 1, tues: 1,
  wednesday: 2, wed: 2,
  thursday: 3, thu: 3, thurs: 3,
  friday: 4, fri: 4,
  saturday: 5, sat: 5,
  sunday: 6, sun: 6,
};

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Is this a real calendar day? Guards against "31st February" and the like,
 * which a speaker can absolutely say and which must not silently roll over
 * into March. */
function validDay(y: number, m: number, d: number): boolean {
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  const probe = new Date(y, m - 1, d);
  return probe.getMonth() === m - 1 && probe.getDate() === d;
}

function dayKeyOf(y: number, m: number, d: number): string {
  return validDay(y, m, d) ? `${y}-${pad(m)}-${pad(d)}` : "";
}

/** Resolve a spoken deadline against the date it was spoken on.
 *
 * `anchor` is the meeting's `recorded_at` — "next Tuesday" means nothing
 * without it, and using "now" instead would silently re-point an old
 * meeting's deadline every time the app is opened. */
export function resolveSpokenDate(
  when: string,
  anchor: Date
): SpokenResolution {
  const raw = String(when || "").trim().toLowerCase();
  if (!raw) return UNPLACEABLE;

  // Anything hedged is not a date, however date-like the rest of it looks.
  // "sometime around the 15th" is a hope, not a deadline.
  if (/\b(sometime|around|maybe|possibly|hopefully|or so|ish)\b/.test(raw)) {
    return UNPLACEABLE;
  }

  const y0 = anchor.getFullYear();

  // --- explicit calendar dates ---------------------------------------------

  // ISO: 2026-03-15
  const iso = raw.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
  if (iso) {
    const key = dayKeyOf(+iso[1], +iso[2], +iso[3]);
    return key ? { dayKey: key, confidence: "exact" } : UNPLACEABLE;
  }

  // "15th March", "15 March 2027", "March 15", "March 15th"
  const dm = raw.match(
    /\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)(?:\s+(\d{4}))?\b/
  );
  if (dm && MONTHS[dm[2]]) {
    const year = dm[3] ? +dm[3] : y0;
    const key = dayKeyOf(year, MONTHS[dm[2]], +dm[1]);
    if (key) return { dayKey: key, confidence: "exact" };
  }
  const md = raw.match(/\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?\b/);
  if (md && MONTHS[md[1]]) {
    const year = md[3] ? +md[3] : y0;
    const key = dayKeyOf(year, MONTHS[md[1]], +md[2]);
    if (key) return { dayKey: key, confidence: "exact" };
  }

  // Numeric d/m — DELIBERATELY NOT SUPPORTED. "3/4" is March 4th to an
  // American and April 3rd to everyone else, and there is no way to tell
  // which the speaker meant. A 50% chance of being a month wrong is exactly
  // the kind of confident error this module exists to avoid.

  // --- relative, anchored to the meeting ------------------------------------

  if (/\btoday\b/.test(raw)) {
    return { dayKey: toDayKey(anchor), confidence: "relative" };
  }
  // Checked BEFORE plain "tomorrow" — the longer phrase CONTAINS the shorter
  // one, so testing "tomorrow" first swallows it and silently loses a day.
  if (/\bday after tomorrow\b/.test(raw)) {
    return { dayKey: toDayKey(addDays(anchor, 2)), confidence: "relative" };
  }
  if (/\btomorrow\b/.test(raw)) {
    return { dayKey: toDayKey(addDays(anchor, 1)), confidence: "relative" };
  }

  // "in 3 days" / "in 2 weeks". Months are not handled: "in 2 months" from
  // the 31st has no obvious day, and the phrase is rarely meant precisely.
  const inN = raw.match(/\bin\s+(\d{1,3})\s+(day|days|week|weeks)\b/);
  if (inN) {
    const n = +inN[1];
    const days = inN[2].startsWith("week") ? n * 7 : n;
    return { dayKey: toDayKey(addDays(anchor, days)), confidence: "relative" };
  }

  // "end of the week" -> Friday of the meeting's week.
  if (/\bend of (?:the )?week\b/.test(raw)) {
    return {
      dayKey: toDayKey(addDays(startOfWeek(anchor), 4)),
      confidence: "relative",
    };
  }
  // "end of the month" -> its last day. Day 0 of the next month is exactly
  // that, and handles February and leap years without a table.
  if (/\bend of (?:the )?month\b/.test(raw)) {
    const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
    return { dayKey: toDayKey(last), confidence: "relative" };
  }

  // Weekdays: "Friday", "next Tuesday", "this Thursday".
  const wd = raw.match(
    /\b(next|this|coming)?\s*(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thu|friday|fri|saturday|sat|sunday|sun)\b/
  );
  if (wd) {
    const target = WEEKDAYS[wd[2]];
    if (target !== undefined) {
      const monday = startOfWeek(anchor);
      const anchorIdx = Math.round(
        (new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate()).getTime() -
          monday.getTime()) / 86400000
      );
      let delta = target - anchorIdx;
      // A bare or "this" weekday that has already passed this week means the
      // NEXT one — "Friday" said on a Saturday is not yesterday.
      if (delta <= 0) delta += 7;
      // "next X" means the following week when X still lies ahead this week;
      // when it has already passed, "next X" is that same upcoming one.
      if (wd[1] === "next" && target > anchorIdx) delta += 7;
      return { dayKey: toDayKey(addDays(monday, anchorIdx + delta)), confidence: "relative" };
    }
  }

  // --- deliberately unplaceable --------------------------------------------
  //
  // Quarters, seasons, holidays and vague horizons ("soon", "shortly", "in a
  // few weeks", "before the holidays", "end of Q3"). Each is a real deadline
  // the user should see — and none of them is a day. They fall through to
  // UNPLACEABLE and the UI lists them without a date.
  return UNPLACEABLE;
}

/** True when the phrase carries a real date the calendar can place. */
export function isPlaceable(r: SpokenResolution): boolean {
  return !!r.dayKey;
}
