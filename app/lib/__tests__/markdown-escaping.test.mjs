// lib/__tests__/markdown-escaping.test.mjs — the renderer's escape handling.
//
// Run:  node --test lib/__tests__/markdown-escaping.test.mjs
//
// THE BUG THIS PINS. AI Chat replies were displaying literal `\*important\*`
// instead of emphasis. The cause was not the model and not the backend: all
// three of our Markdown renderers — lib/document-renderer.tsx (screen),
// lib/export-doc.ts (PDF) and lib/docx-export.ts (.docx) — are small
// hand-written passes over a subset we control, and NONE of them knew about
// backslash escapes.
//
// The mechanism is worth stating exactly, because "the regex didn't match" is
// the wrong summary. The emphasis pass splits on the BARE asterisk:
//
//     "\\*text\\*".split(/(\*\*[^*]+\*\*|\*[^*\n]+\*)/g)
//       -> ["\\", "*text\\*", ...]
//
// The split DOES fire — on the unescaped asterisks — producing a fragment
// (`*text\*`) that then fails the `^\*[^*\n]+\*$` emphasis test and is emitted
// as plain text. So the backslashes reached the screen verbatim.
//
// WHY UNESCAPING IS UNCONDITIONALLY CORRECT HERE. If the model meant emphasis,
// the backslash was breaking it. If it meant a literal asterisk, this subset
// has no escape syntax to express that anyway, and a bare `*` is closer to the
// intent than `\*`. There is no third case.
//
// THE HALF THAT MATTERS MOST is SafetyTests: unescaping only Markdown
// punctuation. A pass that ate every backslash would corrupt a Windows path, a
// regex or a code sample in a correct answer — a worse bug than the cosmetic
// one, because nothing downstream can tell it happened.
//
// The module under test is TypeScript, so unescapeMarkdown is mirrored below
// rather than imported, matching the convention in meeting-outputs.test.mjs
// and task-insights.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from lib/export-doc.ts ---------------------------------------

const MD_ESCAPE = /\\([\\`*_{}[\]()#+\-.!|>~])/g;

function unescapeMarkdown(s) {
  return (s || "").replace(MD_ESCAPE, "$1");
}

// The emphasis pass the three renderers share, reduced to the decision it
// makes: which fragments become bold, which italic, which stay plain.
function emphasize(text) {
  return unescapeMarkdown(text)
    .split(/(\*\*[^*]+\*\*|\*[^*\n]+\*)/g)
    .filter(Boolean)
    .map((p) => {
      if (/^\*\*[^*]+\*\*$/.test(p)) return { bold: p.slice(2, -2) };
      if (/^\*[^*\n]+\*$/.test(p)) return { italic: p.slice(1, -1) };
      return { text: p };
    });
}

describe("unescapeMarkdown", () => {
  it("turns escaped emphasis into real emphasis", () => {
    assert.equal(unescapeMarkdown("\\*Important decision\\*"),
      "*Important decision*");
    assert.equal(unescapeMarkdown("\\*\\*Important decision\\*\\*"),
      "**Important decision**");
  });

  it("leaves correct Markdown untouched", () => {
    for (const text of [
      "**Important decision**",
      "*italic*",
      "- First point\n- Second point",
      "1. First item\n2. Second item",
      "## Heading",
      "| a | b |\n|---|---|\n| 1 | 2 |",
    ]) {
      assert.equal(unescapeMarkdown(text), text, text);
    }
  });

  it("unescapes the other Markdown punctuation", () => {
    assert.equal(unescapeMarkdown("\\- First point"), "- First point");
    assert.equal(unescapeMarkdown("\\## Heading"), "## Heading");
    assert.equal(unescapeMarkdown("1\\. First item"), "1. First item");
    assert.equal(unescapeMarkdown("\\`code\\`"), "`code`");
    assert.equal(unescapeMarkdown("\\[link\\]"), "[link]");
  });

  it("handles empty input", () => {
    assert.equal(unescapeMarkdown(""), "");
    assert.equal(unescapeMarkdown(null), "");
    assert.equal(unescapeMarkdown(undefined), "");
  });
});

describe("rendering the escaped forms", () => {
  it("REGRESSION: \\*text\\* renders as emphasis, not literal backslashes", () => {
    // Before the fix this produced [{text:"\\"},{text:"*Important decision\\*"}]
    // — the exact string the user was seeing on screen.
    assert.deepEqual(emphasize("\\*Important decision\\*"),
      [{ italic: "Important decision" }]);
  });

  it("escaped bold renders bold", () => {
    assert.deepEqual(emphasize("\\*\\*Important decision\\*\\*"),
      [{ bold: "Important decision" }]);
  });

  it("no backslash survives into rendered output", () => {
    for (const part of emphasize("The \\*budget\\* was \\*\\*approved\\*\\*.")) {
      assert.equal(Object.values(part)[0].includes("\\"), false,
        JSON.stringify(part));
    }
  });

  it("unescaped Markdown still renders as it always did", () => {
    assert.deepEqual(emphasize("**Important decision**"),
      [{ bold: "Important decision" }]);
    assert.deepEqual(emphasize("*italic*"), [{ italic: "italic" }]);
  });

  it("mixed formatting inside one line", () => {
    assert.deepEqual(emphasize("Plain \\*it\\* and **bold** here."), [
      { text: "Plain " },
      { italic: "it" },
      { text: " and " },
      { bold: "bold" },
      { text: " here." },
    ]);
  });

  it("plain conversational text is unaffected", () => {
    assert.deepEqual(emphasize("The client approved the proposal."),
      [{ text: "The client approved the proposal." }]);
  });
});

describe("safety — what must NOT be unescaped", () => {
  it("keeps non-Markdown escapes intact", () => {
    // A backslash before an ordinary character is not Markdown escaping.
    for (const text of [
      "Path C:\\Users\\test stays.",
      "Regex \\d+ and \\s+ stay.",
      "The literal \\n newline escape.",
      "A \\q that means nothing.",
    ]) {
      assert.equal(unescapeMarkdown(text), text, text);
    }
  });

  it("a path inside a reply survives rendering", () => {
    assert.deepEqual(emphasize("Open C:\\Users\\shubham\\notes.txt now."),
      [{ text: "Open C:\\Users\\shubham\\notes.txt now." }]);
  });

  it("an escaped backslash collapses to one, as Markdown specifies", () => {
    assert.equal(unescapeMarkdown("A\\\\B"), "A\\B");
  });
});

describe("block formatting is preserved", () => {
  // These go through the block parser, not emphasize(), but the escape pass
  // runs first in every one of those paths — so what matters is that the
  // markers themselves survive to be recognised.
  it("bullet markers survive", () => {
    const md = unescapeMarkdown("- First point\n- Second point");
    for (const line of md.split("\n")) {
      assert.match(line, /^[-*+]\s+/);
    }
  });

  it("numbered markers survive", () => {
    const md = unescapeMarkdown("1. First item\n2. Second item");
    for (const line of md.split("\n")) {
      assert.match(line, /^\d+[.)]\s+/);
    }
  });

  it("an ESCAPED bullet becomes a real bullet", () => {
    // "\- First point" would otherwise render as a paragraph starting with a
    // backslash instead of as a list item.
    assert.match(unescapeMarkdown("\\- First point"), /^-\s+First point$/);
  });

  it("heading markers survive", () => {
    assert.match(unescapeMarkdown("## Decisions"), /^#{1,6}\s+/);
    assert.match(unescapeMarkdown("\\## Decisions"), /^#{1,6}\s+/);
  });
});
