#!/usr/bin/env python3
"""test_salesforce_setup_guide.py — the customer setup guide cannot drift.

docs/salesforce-setup-guide.md is followed click by click (by an admin, or a
browser assistant such as Claude in Chrome), so every fact it states must be
true of the code it describes:

  * the four code blocks ARE the starter receiver files, byte for byte, in
    the order the guide says to save them;
  * every custom field the code uses is one the guide tells you to create,
    and every field name typed in Step 3 is one the code uses;
  * every MinuteX button, status line and error it quotes is really in the
    app;
  * the receiver answers at the address the guide gives;
  * no Apex variable or helper is named like a system type or standard
    object (Apex is case-insensitive: `id` hides `Id`, and the build fails —
    found the hard way in a customer org).

Run:  python -m pytest tests/test_salesforce_setup_guide.py
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUIDE = (REPO / "docs/salesforce-setup-guide.md").read_text(encoding="utf-8")
CLASSES = REPO / "salesforce/minutex-starter/force-app/main/default/classes"
ORDER = ["MinuteXMeetingSync", "MinuteXReceiver", "MinuteXMeetingSyncTest", "MinuteXReceiverTest"]
CODE = {name: (CLASSES / f"{name}.cls").read_text(encoding="utf-8") for name in ORDER}
MAIN_CODE = CODE["MinuteXMeetingSync"] + CODE["MinuteXReceiver"]
APP = "\n".join(p.read_text(encoding="utf-8") for p in [
    REPO / "app/lib/webhook-ui.tsx", REPO / "app/lib/webhook-meeting.tsx",
    REPO / "app/lib/webhook-pickers.tsx", REPO / "app/lib/webhook-flow.ts",
    REPO / "app/src/app/salesforce/index.tsx",
    # Speaker tagging's picker, where people are searched live (Step 9.2).
    REPO / "app/lib/contact-picker.tsx"])

# Custom OBJECTS (not fields) the code names.
OBJECTS = {"MinuteX_Webhook_Settings__c", "MinuteX_Participant__c", "Property__c"}


def code_block(number, name):
    m = re.search(r"## Code " + str(number) + r" — `" + name + r"`\n\n```apex\n(.*?)\n```", GUIDE, re.S)
    assert m, f"no Code {number} block for {name}"
    return m.group(1)


def fields_used():
    """Custom field API names the main classes read or write. MinuteX_Meeting__c
    is both an object and a field (the Participant lookup), so it stays."""
    return set(re.findall(r"\b([A-Z][A-Za-z0-9_]*__c)\b", MAIN_CODE)) - OBJECTS


class CodeBlocks(unittest.TestCase):
    def test_the_guide_embeds_the_real_files_in_save_order(self):
        for number, name in enumerate(ORDER, 1):
            with self.subTest(name=name):
                self.assertEqual(code_block(number, name), CODE[name].rstrip("\n"))
        step4 = GUIDE.split("## Step 4")[1].split("## Step 5")[0]
        rows = re.findall(r"^\| (\d) \| `(\w+)` \| Code (\d) \|", step4, re.M)
        self.assertEqual([r[1] for r in rows], ORDER)

    def test_the_sync_class_saves_first(self):
        # Code 1 must not depend on Code 2, or it can't be saved first.
        code_only = re.sub(r"/\*.*?\*/|//[^\n]*", "", CODE["MinuteXMeetingSync"], flags=re.S)
        self.assertNotIn("MinuteXReceiver", code_only)
        self.assertIn("MinuteXMeetingSync.apply", CODE["MinuteXReceiver"])


class Fields(unittest.TestCase):
    def test_every_field_the_code_uses_is_created_by_the_guide(self):
        named = set(re.findall(r"`([A-Za-z0-9_]+__c)`", GUIDE))
        missing = sorted(fields_used() - named)
        self.assertEqual(missing, [], f"the code uses fields the guide never creates: {missing}")

    def test_every_field_typed_in_step_3_is_used_by_the_code(self):
        step3 = GUIDE.split("## Step 3")[1].split("## Step 4")[0]
        row = re.compile(r"^\s*\| [^|]+ \| `[^`]+` \| [^|]+ \| `([A-Za-z_]+)` \|", re.M)
        typed = {f"{n}__c" for n in row.findall(step3)}
        self.assertGreaterEqual(len(typed), 20)
        self.assertEqual(sorted(typed - fields_used()), [])

    def test_the_meeting_check_lists_its_17_fields(self):
        listed = set(re.findall(r"`([A-Za-z0-9_]+__c)`",
                                re.search(r"lists all 17 fields with these API names:\n(.*?)\n", GUIDE).group(1)))
        self.assertEqual(len(listed), 17)
        self.assertTrue(listed <= fields_used())

    def test_the_settings_check_lists_its_3_fields(self):
        listed = set(re.findall(r"`([A-Za-z0-9_]+__c)`",
                                re.search(r"three custom fields with API names (.*?)\.\n", GUIDE).group(1)))
        self.assertEqual(listed, {"Enabled__c", "Shared_Secret__c", "Previous_Secret__c"})


class AppWording(unittest.TestCase):
    def test_quoted_buttons_and_statuses_exist_in_the_app(self):
        for label in ["Connect Salesforce", "Copy secret", "Test connection", "Search contacts",
                      "Push to Salesforce", "Delivered", "Create new signing secret",
                      "Disconnect Salesforce", "Retry"]:
            with self.subTest(label=label):
                self.assertIn(label, GUIDE)
                self.assertIn(f'"{label}"', APP)
        self.assertIn("Salesforce address", GUIDE)
        self.assertIn("Salesforce address", APP)

    def test_quoted_errors_exist_in_the_app(self):
        for text in ["Salesforce rejected MinuteX's signature",
                     "That address redirects. Use the final address.",
                     "No MinuteX receiver was found at that address",
                     "That address isn't a public web address MinuteX can reach",
                     "Salesforce isn't answering right now"]:
            with self.subTest(text=text):
                self.assertIn(text, GUIDE)
                self.assertIn(text, APP)

    def test_the_test_connection_line_matches(self):
        self.assertIn("Connected — configuration version", APP)
        n = len(re.findall(r"^\s+(?:plain|withRecord)\(plain\(|^\s+plain\(", CODE["MinuteXReceiver"], re.M))
        self.assertIn(f"Connected — configuration version …, {n} meeting types", GUIDE)


class Address(unittest.TestCase):
    def test_the_receiver_answers_where_the_guide_points(self):
        self.assertIn("@RestResource(urlMapping='/minutex/v2/*')", CODE["MinuteXReceiver"])
        self.assertIn("/services/apexrest/minutex/v2", GUIDE)
        self.assertIn("/services/apexrest/DevMinuteX/minutex/v2", GUIDE)  # namespaced orgs
        self.assertIn("The MinuteX receiver is not enabled in this org", CODE["MinuteXReceiver"])
        self.assertIn("The MinuteX receiver is not enabled in this org", GUIDE)


APEX_FILES = sorted((REPO / "salesforce/minutex-starter").rglob("*.cls")) + \
    sorted((REPO / "salesforce/minutex-webhook").rglob("*.cls"))
# System types and standard objects a local name must never spell (in any case).
SHADOWED = ("id", "type", "url", "string", "json", "date", "datetime", "blob", "database",
            "schema", "math", "crypto", "test", "system", "limits", "task", "event",
            "contact", "account", "user", "lead", "opportunity", "case")
DECL = re.compile(r"\b(?:String|Id|Integer|Long|Decimal|Boolean|Object|Blob|Date|Datetime)\s+("
                  + "|".join(SHADOWED) + r")\s*[:,)=;]", re.I)
METHOD = re.compile(r"\bstatic\s+[\w<>., ]+?\s+(" + "|".join(SHADOWED) + r")\s*\(", re.I)


class ApexNames(unittest.TestCase):
    def test_no_variable_or_helper_is_named_like_a_system_type(self):
        offenders = []
        for f in APEX_FILES:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                m = DECL.search(line) or METHOD.search(line)
                # `public String url;` is a result FIELD kept for the wire
                # shape; its class qualifies System.Url, pinned below.
                if m and not (f.name == "MinuteXEventResult.cls" and m.group(1).lower() == "url"):
                    offenders.append(f"{f.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_url_is_qualified_where_a_field_is_named_url(self):
        result = (REPO / "salesforce/minutex-webhook/force-app/main/default/classes/"
                  "MinuteXEventResult.cls").read_text(encoding="utf-8")
        self.assertIn("System.Url.getOrgDomainUrl()", result)
        self.assertNotRegex(result, r"(?<!System\.)\bUrl\.getOrgDomainUrl")


if __name__ == "__main__":
    unittest.main()
