// lib/__tests__/spoken-dates.test.mjs — resolving a spoken deadline to a day.
//
// Run:  node --test lib/__tests__/spoken-dates.test.mjs
//
// WHAT THIS PINS, and why it matters more than most of this codebase.
//
// The AI stores a meeting's deadlines EXACTLY as spoken ("next Tuesday", "end
// of Q3") because prompts.py forbids it from resolving them — it does not know
// the meeting's date, so any resolution would be a guess. lib/spoken-dates.ts
// does that resolution on the client, anchored to `recorded_at`.
//
// A calendar is read at a glance and planned around. A deadline shown on the
// wrong Thursday is worse than one shown with no date at all, so the whole
// design of that module is "resolve only the unambiguous, return null for
// everything else". These tests exist to hold that line — especially the
// REFUSALS, which are the part a future change is most likely to erode by
// making the parser "smarter".
//
// The module is mirrored here rather than imported, matching the convention in
// contact-ordering.test.mjs and task-insights.test.mjs: node --test runs .mjs
// without a transpile step and the source is TypeScript.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from lib/task-insights.ts (the two helpers it uses) -----------

function toDayKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function addDays(from, days) {
  const d = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  d.setDate(d.getDate() + days);
  return d;
}

function startOfWeek(d) {
  const day = d.getDay();
  const back = day === 0 ? 6 : day - 1;
  return addDays(d, -back);
}

// --- mirrored from lib/spoken-dates.ts --------------------------------------

const UNPLACEABLE = { dayKey: "", confidence: "none" };

const MONTHS = {
  jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3,
  apr: 4, april: 4, may: 5, jun: 6, june: 6, jul: 7, july: 7,
  aug: 8, august: 8, sep: 9, sept: 9, september: 9, oct: 10, october: 10,
  nov: 11, november: 11, dec: 12, december: 12,
};

const WEEKDAYS = {
  monday: 0, mon: 0,
  tuesday: 1, tue: 1, tues: 1,
  wednesday: 2, wed: 2,
  thursday: 3, thu: 3, thurs: 3,
  friday: 4, fri: 4,
  saturday: 5, sat: 5,
  sunday: 6, sun: 6,
};

const pad = (n) => String(n).padStart(2, "0");

function validDay(y, m, d) {
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  const probe = new Date(y, m - 1, d);
  return probe.getMonth() === m - 1 && probe.getDate() === d;
}

function dayKeyOf(y, m, d) {
  return validDay(y, m, d) ? `${y}-${pad(m)}-${pad(d)}` : "";
}

function resolveSpokenDate(when, anchor) {
  const raw = String(when || "").trim().toLowerCase();
  if (!raw) return UNPLACEABLE;

  if (/\b(sometime|around|maybe|possibly|hopefully|or so|ish)\b/.test(raw)) {
    return UNPLACEABLE;
  }

  const y0 = anchor.getFullYear();

  const iso = raw.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
  if (iso) {
    const key = dayKeyOf(+iso[1], +iso[2], +iso[3]);
    return key ? { dayKey: key, confidence: "exact" } : UNPLACEABLE;
  }

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

  const inN = raw.match(/\bin\s+(\d{1,3})\s+(day|days|week|weeks)\b/);
  if (inN) {
    const n = +inN[1];
    const days = inN[2].startsWith("week") ? n * 7 : n;
    return { dayKey: toDayKey(addDays(anchor, days)), confidence: "relative" };
  }

  if (/\bend of (?:the )?week\b/.test(raw)) {
    return {
      dayKey: toDayKey(addDays(startOfWeek(anchor), 4)),
      confidence: "relative",
    };
  }
  if (/\bend of (?:the )?month\b/.test(raw)) {
    const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
    return { dayKey: toDayKey(last), confidence: "relative" };
  }

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
      if (delta <= 0) delta += 7;
      if (wd[1] === "next" && target > anchorIdx) delta += 7;
      return {
        dayKey: toDayKey(addDays(monday, anchorIdx + delta)),
        confidence: "relative",
      };
    }
  }

  return UNPLACEABLE;
}

const isPlaceable = (r) => !!r.dayKey;

// --- fixtures ---------------------------------------------------------------

// The meeting was recorded on Wednesday 2026-08-26.
const MEETING = new Date(2026, 7, 26, 10, 30);

const resolve = (s) => resolveSpokenDate(s, MEETING);

// --- tests ------------------------------------------------------------------

describe("explicit calendar dates", () => {
  it("reads an ISO date", () => {
    assert.deepEqual(resolve("2026-03-15"), {
      dayKey: "2026-03-15",
      confidence: "exact",
    });
  });

  it("reads day-month in the forms people speak", () => {
    for (const s of ["15th March", "15 March", "15th of March"]) {
      assert.equal(resolve(s).dayKey, "2026-03-15", s);
      assert.equal(resolve(s).confidence, "exact", s);
    }
  });

  it("reads month-day, with and without an ordinal", () => {
    assert.equal(resolve("March 15").dayKey, "2026-03-15");
    assert.equal(resolve("March 15th").dayKey, "2026-03-15");
  });

  it("honours an explicit year and defaults to the meeting's otherwise", () => {
    assert.equal(resolve("15 March 2027").dayKey, "2027-03-15");
    assert.equal(resolve("15 March").dayKey, "2026-03-15");
  });

  it("accepts common month abbreviations", () => {
    assert.equal(resolve("3rd Sept").dayKey, "2026-09-03");
    assert.equal(resolve("1 Dec").dayKey, "2026-12-01");
  });

  it("refuses an impossible day rather than rolling it over", () => {
    // The classic Date bug: Feb 31 silently becomes March 3.
    assert.equal(isPlaceable(resolve("31st February")), false);
    assert.equal(isPlaceable(resolve("2026-02-30")), false);
  });

  it("refuses ambiguous numeric dates entirely", () => {
    // "3/4" is March 4th or April 3rd depending on the speaker. Guessing is a
    // coin flip on the month, so it is never placed.
    for (const s of ["3/4", "03/04/2026", "3-4"]) {
      assert.equal(isPlaceable(resolve(s)), false, s);
    }
  });
});

describe("relative phrases, anchored to the meeting date", () => {
  it("resolves today / tomorrow against the MEETING, not the clock", () => {
    assert.equal(resolve("today").dayKey, "2026-08-26");
    assert.equal(resolve("tomorrow").dayKey, "2026-08-27");
    assert.equal(resolve("the day after tomorrow").dayKey, "2026-08-28");
  });

  it("marks relative resolutions as inferred, not exact", () => {
    assert.equal(resolve("tomorrow").confidence, "relative");
    assert.equal(resolve("next Tuesday").confidence, "relative");
  });

  it("counts forward in days and weeks", () => {
    assert.equal(resolve("in 3 days").dayKey, "2026-08-29");
    assert.equal(resolve("in 2 weeks").dayKey, "2026-09-09");
  });

  it("reads end of week as the Friday of the meeting's week", () => {
    // 2026-08-26 is a Wednesday; that week's Friday is the 28th.
    assert.equal(resolve("end of the week").dayKey, "2026-08-28");
    assert.equal(resolve("end of week").dayKey, "2026-08-28");
  });

  it("reads end of month as its real last day", () => {
    assert.equal(resolve("end of the month").dayKey, "2026-08-31");
    // February, including a leap year, without a lookup table.
    assert.equal(
      resolveSpokenDate("end of month", new Date(2027, 1, 10)).dayKey,
      "2027-02-28"
    );
    assert.equal(
      resolveSpokenDate("end of month", new Date(2028, 1, 10)).dayKey,
      "2028-02-29"
    );
  });
});

describe("weekdays", () => {
  it("takes a weekday still ahead this week as that day", () => {
    // Meeting is Wednesday; Friday is two days later.
    assert.equal(resolve("Friday").dayKey, "2026-08-28");
  });

  it("pushes a weekday that has already passed into the following week", () => {
    // Monday already happened this week, so "Monday" means the next one.
    assert.equal(resolve("Monday").dayKey, "2026-08-31");
  });

  it("never resolves a bare weekday to the meeting's own day", () => {
    // "Wednesday" said in a Wednesday meeting means next Wednesday — a
    // deadline is not usually the moment it is being set.
    assert.equal(resolve("Wednesday").dayKey, "2026-09-02");
  });

  it("reads 'next X' as the following week when X is still ahead", () => {
    // Friday is ahead this week, so "next Friday" is the week after.
    assert.equal(resolve("next Friday").dayKey, "2026-09-04");
  });

  it("reads 'next X' as the upcoming one when X has already passed", () => {
    // Monday has passed; "next Monday" is the only Monday it can mean.
    assert.equal(resolve("next Monday").dayKey, "2026-08-31");
  });

  it("accepts abbreviations", () => {
    assert.equal(resolve("Fri").dayKey, "2026-08-28");
    assert.equal(resolve("Tues").dayKey, "2026-09-01");
  });

  it("is case-insensitive and tolerates surrounding words", () => {
    assert.equal(resolve("by FRIDAY please").dayKey, "2026-08-28");
  });
});

describe("refusals — the part that protects the calendar", () => {
  it("does not place a quarter, season or holiday", () => {
    for (const s of [
      "end of Q3",
      "Q4",
      "before the holidays",
      "over the summer",
      "next quarter",
      "end of the year",
    ]) {
      assert.equal(isPlaceable(resolve(s)), false, `must not place: ${s}`);
      assert.equal(resolve(s).confidence, "none", s);
    }
  });

  it("does not place a vague horizon", () => {
    for (const s of ["soon", "shortly", "ASAP", "in a few weeks", "later"]) {
      assert.equal(isPlaceable(resolve(s)), false, `must not place: ${s}`);
    }
  });

  it("refuses a hedged phrase even when it contains a date", () => {
    // "sometime around the 15th" is a hope, not a deadline.
    for (const s of [
      "sometime around 15th March",
      "maybe Friday",
      "around the 3rd of June",
      "Friday-ish",
    ]) {
      assert.equal(isPlaceable(resolve(s)), false, `must not place: ${s}`);
    }
  });

  it("returns unplaceable for empty or missing input", () => {
    assert.equal(isPlaceable(resolve("")), false);
    assert.equal(isPlaceable(resolve("   ")), false);
    assert.equal(isPlaceable(resolveSpokenDate(null, MEETING)), false);
    assert.equal(isPlaceable(resolveSpokenDate(undefined, MEETING)), false);
  });

  it("returns unplaceable for text with no date in it at all", () => {
    assert.equal(isPlaceable(resolve("when the contract is signed")), false);
    assert.equal(isPlaceable(resolve("after legal review")), false);
  });
});

describe("anchoring", () => {
  it("resolves the same phrase differently for different meetings", () => {
    // This is the whole reason resolution happens client-side with the
    // meeting's own date: "tomorrow" in a January meeting is not "tomorrow"
    // in an August one, and re-reading it against today's clock would silently
    // move an old meeting's deadline every time the app opened.
    const jan = new Date(2026, 0, 5);
    const aug = new Date(2026, 7, 26);
    assert.equal(resolveSpokenDate("tomorrow", jan).dayKey, "2026-01-06");
    assert.equal(resolveSpokenDate("tomorrow", aug).dayKey, "2026-08-27");
  });

  it("crosses a month boundary correctly", () => {
    // Meeting on the 30th; "in 3 days" lands in the next month.
    assert.equal(
      resolveSpokenDate("in 3 days", new Date(2026, 7, 30)).dayKey,
      "2026-09-02"
    );
  });

  it("crosses a year boundary correctly", () => {
    assert.equal(
      resolveSpokenDate("in 1 week", new Date(2026, 11, 29)).dayKey,
      "2027-01-05"
    );
  });
});
