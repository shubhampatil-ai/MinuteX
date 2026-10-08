"""Custom permission + the two permission sets of the MinuteX package,
generated from the package's own field files (re-run after adding a field)."""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "force-app", "main", "default")
os.chdir(ROOT)
H = '<?xml version="1.0" encoding="UTF-8"?>\n'

OBJECTS = ["MinuteX_Meeting__c", "MinuteX_Participant__c", "MinuteX_Meeting_Link__c"]


def fields(obj):
    out = []
    d = os.path.join("objects", obj, "fields")
    for f in sorted(os.listdir(d)):
        x = open(os.path.join(d, f), encoding="utf-8").read()
        if "<required>true</required>" in x or "<type>MasterDetail</type>" in x:
            continue   # FLS can't be set on these; they're always visible
        out.append(f.replace(".field-meta.xml", ""))
    return out


def fp(field, edit):
    return (f"    <fieldPermissions>\n        <editable>{'true' if edit else 'false'}</editable>\n"
            f"        <field>{field}</field>\n        <readable>true</readable>\n    </fieldPermissions>\n")


def op(obj, create, edit, delete):
    b = lambda v: "true" if v else "false"
    return (f"    <objectPermissions>\n        <allowCreate>{b(create)}</allowCreate>\n"
            f"        <allowDelete>{b(delete)}</allowDelete>\n        <allowEdit>{b(edit)}</allowEdit>\n"
            f"        <allowRead>true</allowRead>\n        <modifyAllRecords>false</modifyAllRecords>\n"
            f"        <object>{obj}</object>\n        <viewAllRecords>false</viewAllRecords>\n"
            f"    </objectPermissions>\n")


os.makedirs("customPermissions", exist_ok=True)
open("customPermissions/MinuteX_Integration.customPermission-meta.xml", "w", encoding="utf-8").write(H + """<CustomPermission xmlns="http://soap.sforce.com/2006/04/metadata">
    <description>The MinuteX integration user: may set up MinuteX's signing secret in this org (MinuteXReceiver setup/secret). Given only by the MinuteX Integration permission set.</description>
    <isLicensed>false</isLicensed>
    <label>MinuteX Integration</label>
</CustomPermission>
""")

os.makedirs("permissionsets", exist_ok=True)

# The integration user: what MinuteX does in the org.
fps = "".join(fp(f"{o}.{f}", True) for o in OBJECTS for f in fields(o))
# Activity fields are granted on Task.
fps += "".join(fp(f"Task.{f}", True) for f in fields("Activity"))
ops = (op("MinuteX_Meeting__c", True, True, False) + op("MinuteX_Participant__c", True, True, False)
       + op("MinuteX_Meeting_Link__c", True, True, True)
       + "".join(op(o, False, False, False) for o in ("Account", "Contact", "Lead", "Opportunity")))
open("permissionsets/MinuteX_Integration.permissionset-meta.xml", "w", encoding="utf-8").write(H + f"""<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
    <classAccesses>
        <apexClass>MinuteXMeetingSync</apexClass>
        <enabled>true</enabled>
    </classAccesses>
    <classAccesses>
        <apexClass>MinuteXReceiver</apexClass>
        <enabled>true</enabled>
    </classAccesses>
    <customPermissions>
        <enabled>true</enabled>
        <name>MinuteX_Integration</name>
    </customPermissions>
    <description>For the MinuteX integration user only: receives meetings, links them to records (read only on Leads, Contacts, Accounts and Opportunities: MinuteX never changes them), creates Tasks and Files, and sets up the signing secret.</description>
{fps}    <hasActivationRequired>false</hasActivationRequired>
    <label>MinuteX Integration</label>
{ops}    <tabSettings>
        <tab>MinuteX_Meeting__c</tab>
        <visibility>Visible</visibility>
    </tabSettings>
</PermissionSet>
""")

# Sales users: see the meetings, read only.
fps = "".join(fp(f"{o}.{f}", False) for o in OBJECTS for f in fields(o))
ops = "".join(op(o, False, False, False) for o in OBJECTS)
open("permissionsets/MinuteX_Meeting_Viewer.permissionset-meta.xml", "w", encoding="utf-8").write(H + f"""<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
    <description>See MinuteX meetings: the MinuteX Meetings tab, and the "MinuteX Meetings" related list on Leads, Contacts, Accounts and Opportunities (read only).</description>
{fps}    <hasActivationRequired>false</hasActivationRequired>
    <label>MinuteX Meeting Viewer</label>
{ops}    <tabSettings>
        <tab>MinuteX_Meeting__c</tab>
        <visibility>Visible</visibility>
    </tabSettings>
</PermissionSet>
""")
for o in OBJECTS:
    print(o, len(fields(o)), "fields with FLS")
