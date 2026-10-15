"""The MinuteX package's three permission sets, generated from its own field
files (re-run after adding a field or object):

  MinuteX Integration     the one integration user: receives meetings,
                          writes MinuteX's objects, Tasks and Files; reads
                          the records meetings link to; sets the secret
  MinuteX Admin           Salesforce admins: the MinuteX Setup tab
  MinuteX Meeting Viewer  sales users: see meetings, read only
"""
import os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "force-app", "main", "default")
os.chdir(ROOT)
H = '<?xml version="1.0" encoding="UTF-8"?>\n'

MEETING_OBJECTS = ["MinuteX_Meeting__c", "MinuteX_Participant__c", "MinuteX_Meeting_Link__c",
                   "MinuteX_Delivery_Log__c"]
SETUP_OBJECT = "MinuteX_Linkable_Object__c"


def fields(obj):
    out = []
    d = os.path.join("objects", obj, "fields")
    for f in sorted(os.listdir(d)):
        x = open(os.path.join(d, f), encoding="utf-8").read()
        if "<required>true</required>" in x or "<type>MasterDetail</type>" in x:
            continue   # FLS can't be set on these; they're always visible
        out.append(f.replace(".field-meta.xml", ""))
    return out


def fps(objs, edit):
    return "".join(
        f"    <fieldPermissions>\n        <editable>{'true' if edit else 'false'}</editable>\n"
        f"        <field>{o}.{f}</field>\n        <readable>true</readable>\n    </fieldPermissions>\n"
        for o in objs for f in fields(o))


def op(obj, create=False, edit=False, delete=False):
    b = lambda v: "true" if v else "false"
    return (f"    <objectPermissions>\n        <allowCreate>{b(create)}</allowCreate>\n"
            f"        <allowDelete>{b(delete)}</allowDelete>\n        <allowEdit>{b(edit)}</allowEdit>\n"
            f"        <allowRead>true</allowRead>\n        <modifyAllRecords>false</modifyAllRecords>\n"
            f"        <object>{obj}</object>\n        <viewAllRecords>false</viewAllRecords>\n"
            f"    </objectPermissions>\n")


def classes(*names):
    return "".join(f"    <classAccesses>\n        <apexClass>{n}</apexClass>\n        <enabled>true</enabled>\n"
                   f"    </classAccesses>\n" for n in names)


def custom(name):
    return f"    <customPermissions>\n        <enabled>true</enabled>\n        <name>{name}</name>\n    </customPermissions>\n"


def app():
    return ("    <applicationVisibilities>\n        <application>MinuteX</application>\n"
            "        <visible>true</visible>\n    </applicationVisibilities>\n")


def tabs(*names):
    return "".join(f"    <tabSettings>\n        <tab>{n}</tab>\n        <visibility>Visible</visibility>\n"
                   f"    </tabSettings>\n" for n in names)


def write(name, label, description, body):
    # Elements in the Metadata API's order.
    open(f"permissionsets/{name}.permissionset-meta.xml", "w", encoding="utf-8", newline="\n").write(
        H + '<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        + body["apps"] + body["classes"] + body["custom"]
        + f"    <description>{description}</description>\n"
        + body["fields"] + "    <hasActivationRequired>false</hasActivationRequired>\n"
        + f"    <label>{label}</label>\n" + body["objects"] + body["tabs"] + "</PermissionSet>\n")


os.makedirs("permissionsets", exist_ok=True)

write("MinuteX_Integration", "MinuteX Integration",
      "For the MinuteX integration user only: receives meetings, links them to records (read only on "
      "Leads, Contacts, Accounts and Opportunities: MinuteX never changes them), creates Tasks and Files, "
      "logs deliveries, and sets up the signing secret.",
      {"apps": "", "classes": classes("MinuteXDeliveryLog", "MinuteXMeetingSync", "MinuteXReceiver"),
       "custom": custom("MinuteX_Integration"),
       "fields": fps(MEETING_OBJECTS, True) + fps([SETUP_OBJECT], False)
                 + "".join(f"    <fieldPermissions>\n        <editable>true</editable>\n"
                           f"        <field>Task.{f}</field>\n        <readable>true</readable>\n"
                           f"    </fieldPermissions>\n" for f in fields("Activity")),
       "objects": (op("Account") + op("Contact") + op("Lead")
                   + op("MinuteX_Delivery_Log__c", True, True, True)
                   + op(SETUP_OBJECT)
                   + op("MinuteX_Meeting_Link__c", True, True, True)
                   + op("MinuteX_Meeting__c", True, True) + op("MinuteX_Participant__c", True, True)
                   + op("Opportunity")),
       "tabs": tabs("MinuteX_Meeting__c")})

write("MinuteX_Admin", "MinuteX Admin",
      "For Salesforce admins: the MinuteX app and its MinuteX Setup tab (which records meetings can "
      "link to, and what each one receives), plus read access to MinuteX meetings and delivery logs.",
      {"apps": app(), "classes": classes("MinuteXRecordMeetings", "MinuteXSetupController"),
       "custom": custom("MinuteX_Admin"),
       "fields": fps(MEETING_OBJECTS, False) + fps([SETUP_OBJECT], True),
       "objects": (op("MinuteX_Delivery_Log__c") + op(SETUP_OBJECT, True, True, True)
                   + op("MinuteX_Meeting_Link__c") + op("MinuteX_Meeting__c") + op("MinuteX_Participant__c")),
       "tabs": tabs("MinuteX_Meeting__c", "MinuteX_Setup")})

write("MinuteX_Meeting_Viewer", "MinuteX Meeting Viewer",
      "For sales users: the MinuteX app, the MinuteX Meetings tab, and the meetings on their records "
      "(the MinuteX Meetings component on any record page, and the related list on Leads, Contacts, "
      "Accounts and Opportunities). Read only.",
      {"apps": app(), "classes": classes("MinuteXRecordMeetings"), "custom": "",
       "fields": fps(["MinuteX_Meeting__c", "MinuteX_Participant__c", "MinuteX_Meeting_Link__c"], False),
       "objects": op("MinuteX_Meeting_Link__c") + op("MinuteX_Meeting__c") + op("MinuteX_Participant__c"),
       "tabs": tabs("MinuteX_Meeting__c")})

for o in MEETING_OBJECTS + [SETUP_OBJECT]:
    print(o, len(fields(o)), "fields with FLS")
