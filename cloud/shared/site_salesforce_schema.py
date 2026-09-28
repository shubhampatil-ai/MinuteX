"""site_salesforce_schema.py — discovered Salesforce metadata, manual Task
mappings, and Task specs, for the Site architecture. Pure: no AWS, no network.

THE RULE THIS MODULE EXISTS TO ENFORCE: Salesforce is the only source of
object and field names, and a person is the only source of mapping decisions.
Nothing here guesses an object from meeting text, classifies a meeting, or
invents an API name; every object, field, picklist value, record id and
assignee a mapping or Task uses is validated against metadata DISCOVERED from
the customer's org (as the integration user sees it) or against the People
Store. No LLM is involved anywhere in this path.

STORAGE SHAPES (SiteSalesforceSchema table, PK workspace_id, SK item_key):
  OBJ#<ApiName>  one accessible object: label, flags, key_prefix,
                 task_relation ("what" | "who" | ""), run id, synced_at
  FLD#<ApiName>  that object's readable fields, compacted
"""
import re
from datetime import date, datetime, timedelta, timezone

OBJ_PREFIX = "OBJ#"
FLD_PREFIX = "FLD#"

_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Task fields the Task spec sets itself; a mapping's extra_fields may not.
MANAGED_TASK_FIELDS = frozenset({"subject", "description", "activitydate", "status",
                                 "priority", "ownerid", "whatid", "whoid"})
EXTRA_FIELD_TYPES = frozenset({"string", "textarea", "picklist", "boolean", "date",
                               "double", "integer", "percent", "currency", "email",
                               "phone", "url"})
DUE_RULES = ("none", "days_after_meeting", "days_after_creation")
MAX_DUE_DAYS = 365
# Keep one FLD# item well under DynamoDB's 400 KB item limit.
MAX_FIELDS_ITEM_BYTES = 300_000


class SchemaError(ValueError):
    """Refused input. `code` is stable; `message` is safe to show."""

    def __init__(self, code, message, field="", status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.status = status


def is_api_name(value):
    return bool(_API_NAME_RE.match(str(value or "")))


def _b(value):
    return value is True


def _s(value, limit=None):
    out = "" if value is None else str(value).strip()
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# Discovered metadata
# ---------------------------------------------------------------------------

def task_relation(api_name, what_objects, who_objects):
    """How a Task relates to this object, per the org's own Task schema."""
    if api_name in (who_objects or ()):
        return "who"
    if api_name in (what_objects or ()):
        return "what"
    return ""


def normalize_object(raw, what_objects=(), who_objects=()):
    """One object from object_schema(objects), or None if unusable."""
    if not isinstance(raw, dict) or not is_api_name(raw.get("api_name")):
        return None
    name = raw["api_name"]
    out = {
        "api_name": name,
        "label": _s(raw.get("label"), 255) or name,
        "label_plural": _s(raw.get("label_plural"), 255),
        "key_prefix": _s(raw.get("key_prefix"), 3),
        "is_custom": _b(raw.get("is_custom")),
        "queryable": _b(raw.get("queryable")),
        "searchable": _b(raw.get("searchable")),
        "createable": _b(raw.get("createable")),
        "updateable": _b(raw.get("updateable")),
        "deletable": _b(raw.get("deletable")),
        "task_relation": task_relation(name, what_objects, who_objects),
    }
    # Receiver 1.4+: do the object's records appear in "Recently Viewed"?
    # A platform property separating business record objects from machinery.
    # Absent (older receiver) = unknown: the naming rules alone apply.
    if isinstance(raw.get("mru_enabled"), bool):
        out["mru_enabled"] = raw["mru_enabled"]
    return out


def normalize_field(raw):
    if not isinstance(raw, dict) or not is_api_name(raw.get("api_name")):
        return None
    out = {
        "api_name": raw["api_name"],
        "label": _s(raw.get("label"), 255) or raw["api_name"],
        "type": _s(raw.get("type"), 40).lower(),
        "createable": _b(raw.get("createable")),
        "updateable": _b(raw.get("updateable")),
        "required": _b(raw.get("required")),
        "nillable": _b(raw.get("nillable")),
        "is_custom": _b(raw.get("is_custom")),
        "name_field": _b(raw.get("name_field")),
        "length": int(raw.get("length") or 0),
        "reference_to": [r for r in (raw.get("reference_to") or []) if is_api_name(r)][:50],
    }
    values = raw.get("picklist_values")
    if isinstance(values, list):
        out["picklist_values"] = [
            {"value": _s(v.get("value"), 255), "label": _s(v.get("label"), 255),
             "default": _b(v.get("default"))}
            for v in values if isinstance(v, dict) and _s(v.get("value"))][:200]
    return out


def fit_fields(fields, max_bytes=MAX_FIELDS_ITEM_BYTES):
    """(fields, picklists_trimmed). Trims picklist value lists — never drops a
    field — until the list fits one DynamoDB item. The flag records it."""
    import json

    def size(fs):
        return len(json.dumps(fs, separators=(",", ":")))

    if size(fields) <= max_bytes:
        return fields, False
    for cap in (50, 10, 0):
        fields = [dict(f, picklist_values=(f.get("picklist_values") or [])[:cap])
                  if "picklist_values" in f else f for f in fields]
        if size(fields) <= max_bytes:
            break
    return fields, True


def fields_by_name(fields_item):
    return {f["api_name"]: f for f in (fields_item or {}).get("fields") or []}


# ---------------------------------------------------------------------------
# The object catalog
#
#   Salesforce metadata (object_schema, every page)
#          -> broad discovery
#          -> technical / MinuteX / permission filtering   (catalog_exclusion)
#          -> object catalog (OBJ# rows, with capabilities)
#          -> recommended objects + search + workspace preferences (PREF#)
#          -> capability check per operation (meeting vs Task relationship)
#
# DISCOVERY IS BROAD. Every object the integration user can query is kept,
# standard or custom, EXCEPT clearly technical/system objects, MinuteX's own
# objects and objects it cannot query. No name allowlist decides discovery: a
# customer's Property__c, Building__c or acme__Deal__c is kept because the org
# says it exists and is readable, not because MinuteX recognises it.
#
# DISCOVERED IS NOT SHOWN. What a person sees first is small and predictable:
# the RECOMMENDED objects (defaults, not a filter) plus the objects the
# workspace has enabled; everything else in the catalog is found by search.
#
# CAPABILITIES ARE SEPARATE, and neither is a discovery filter:
#   meeting_relationship_supported  a meeting can be linked to its records
#                                   (the object is in the catalog and queryable)
#   task_relationship_supported     Salesforce lets a Task point at it
#                                   (Task.WhatId/WhoId referenceTo -> task_relation)
# A Building without "Allow Activities" is still a meeting relationship.
# ---------------------------------------------------------------------------
RECOMMENDED_OBJECTS = ("Account", "Contact", "Lead", "Opportunity", "Case")
_RECOMMENDED_ORDER = {name: i for i, name in enumerate(RECOMMENDED_OBJECTS)}
PREFS_KEY = "PREF#objects"
MAX_ENABLED_OBJECTS = 200

# MinuteX's own objects (the receiver's, and the removed package's).
MINUTEX_OBJECT_PREFIXES = ("MinuteX_", "minutex__")
# Discovery also keeps Task itself: its fields (Status/Priority picklists) are
# what Task specs are validated against. It is never offered as a relationship.
DISCOVERY_ALWAYS = ("Task",)
# Objects that are a PERSON (Task.WhoId) even when an org's metadata is read
# before task_relation is known.
PERSON_OBJECTS = ("Contact", "Lead")

# Technical companions and platform object kinds, by API-name suffix. These
# are structural naming conventions of the platform, not business guesses.
TECHNICAL_SUFFIXES = (
    "__mdt", "__e", "__b", "__x", "__xo", "__hd", "__dlm", "__dll", "__chn", "__kav",
    "__ka", "__Share", "__History", "__Feed", "__Tag", "__ChangeEvent", "__votestat",
    "__ViewStat", "__DataCategorySelection",
    "ChangeEvent", "Share", "History", "Feed", "Tag", "OwnerSharingRule", "Event__e",
    "EventStore", "EventStream", "Event",
)
# Standard objects that are platform machinery (code, metadata, security,
# setup, logging, analytics, activities) — by API-name prefix. Applied to
# STANDARD objects only: a custom Listing__c or Report_Card__c is never caught.
TECHNICAL_STANDARD_PREFIXES = (
    "Apex", "Api", "App", "Async", "Aura", "Auth", "Background", "Brand", "Bulk",
    "Cron", "Csp", "Custom", "Dashboard", "Data", "Datacloud", "Debug", "Devops",
    "Document", "Domain", "Duplicate", "Email", "Embedded", "Entity", "Event",
    "External", "Field", "FileSearch", "Flow", "Folder", "Group", "Installed",
    "Lightning", "ListView", "Login", "Macro", "Matching", "Metadata", "Named",
    "Oauth", "Org", "Package", "Permission", "Platform", "Process", "Profile",
    "Prompt", "Push", "Queue", "Quick", "Record", "Remote", "Report", "Saml",
    "Scontrol", "Search", "Security", "Session", "Setup", "Site", "Static",
    "Tab", "Tenant", "Test", "Third", "Todo", "Topic", "Transaction", "Ui",
    "User", "Vote", "Wave", "Web",
)
TECHNICAL_STANDARD_NAMES = frozenset({
    "Task", "Event", "Attachment", "Note", "ContentDocument", "ContentVersion",
    "ContentDocumentLink", "ContentNote", "ContentWorkspace", "FeedItem", "FeedComment",
    "Idea", "Holiday", "BusinessHours", "Organization", "Period", "FiscalYearSettings",
    "Pricebook2", "PricebookEntry", "Partner", "OpportunityContactRole", "AccountContactRole",
    "CaseComment", "CaseSolution", "CollaborationGroup", "Approval", "ActivityHistory",
    "OpenActivity", "Name", "RecentlyViewed", "AggregateResult", "StreamingChannel",
})


def catalog_exclusion(obj):
    """Why a discovered object stays OUT of the catalog, or "" to keep it.

    Only three reasons: MinuteX's own object, a technical/system object, or
    one the integration user cannot query. For STANDARD objects, receivers
    1.4+ also report `mru_enabled` (records appear in Recently Viewed): an
    object without it is platform machinery (events, config, logs). Custom
    objects never depend on it — every custom object is kept."""
    obj = obj if isinstance(obj, dict) else {}
    name = _s(obj.get("api_name"))
    if not is_api_name(name):
        return "invalid"
    if name.startswith(MINUTEX_OBJECT_PREFIXES) or "__MinuteX_" in name:
        return "minutex"
    if not obj.get("queryable"):
        return "not_queryable"
    if name.endswith(TECHNICAL_SUFFIXES):
        return "technical"
    if obj.get("is_custom"):
        # Every custom OBJECT (…__c, including a package's ns__Obj__c) is kept.
        return "" if name.endswith("__c") else "technical"
    if name in TECHNICAL_STANDARD_NAMES or name.startswith(TECHNICAL_STANDARD_PREFIXES):
        return "technical"
    if obj.get("mru_enabled") is False:
        return "technical"
    return ""


def object_category(obj):
    """"standard", "custom" or "" (not in the catalog)."""
    if catalog_exclusion(obj):
        return ""
    return "custom" if (obj or {}).get("is_custom") else "standard"


def is_usable_object(obj):
    """In the catalog: a person can find it and link a meeting to it."""
    return object_category(obj) != ""


def meeting_relationship_supported(obj):
    return is_usable_object(obj)


def task_relationship_supported(obj):
    """Can a Salesforce Task be attached to this object in this org? Checked
    only when a Task is created or changed — never a discovery filter."""
    return is_usable_object(obj) and bool((obj or {}).get("task_relation"))


def supports_tasks(obj):
    return bool((obj or {}).get("task_relation"))


def is_recommended(obj):
    return _s((obj or {}).get("api_name")) in _RECOMMENDED_ORDER


def keep_in_discovery(obj):
    """Discovery stores every catalog object, plus Task (for its fields)."""
    return is_usable_object(obj) or _s((obj or {}).get("api_name")) in DISCOVERY_ALWAYS


def describe_fields_at_discovery(api_name):
    """Fields are described up front only where MinuteX needs them without a
    person asking: Task (picklists for Task specs) and the recommended
    objects. Any other object's fields are described live, on first view —
    so a 1,000-object catalog costs a few calls, not a hundred."""
    return api_name in DISCOVERY_ALWAYS or api_name in _RECOMMENDED_ORDER


def object_sort_key(obj, enabled=()):
    """Recommended (in their fixed order), then workspace-enabled, then the
    rest — each by label."""
    name = _s(obj.get("api_name"))
    label = _s(obj.get("label")).lower() or name.lower()
    if name in _RECOMMENDED_ORDER:
        return (0, _RECOMMENDED_ORDER[name], "")
    if name in enabled:
        return (1, 0, label)
    return (2, 0, label)


def public_object(item, enabled=()):
    out = {k: item.get(k) for k in (
        "api_name", "label", "label_plural", "key_prefix", "is_custom", "queryable",
        "searchable", "createable", "updateable", "deletable", "task_relation",
        "fields_synced_at", "synced_at")}
    out["category"] = object_category(item)
    out["meeting_relationship_supported"] = meeting_relationship_supported(item)
    out["task_relationship_supported"] = task_relationship_supported(item)
    out["recommended"] = is_recommended(item)
    out["enabled"] = _s(item.get("api_name")) in enabled
    return out


def normalize_enabled(names, known):
    """A workspace's enabled-object list: valid, catalogued, de-duplicated."""
    out = []
    for n in names or []:
        n = _s(n)
        if is_api_name(n) and n in known and n not in out:
            out.append(n)
    return out[:MAX_ENABLED_OBJECTS]


# ---------------------------------------------------------------------------
# Task relationships
#
# A Salesforce Task has TWO relationship fields, and they mean different
# things: WhoId is a person (Contact or Lead), WhatId is a business record
# (Account, Opportunity, a custom object...). One Task may carry one of each —
# a call with Asha Rao (Who) about the Big Deal opportunity (What). Which field
# an object goes into is this org's own answer (task_relation, from
# Task.WhoId/WhatId's referenceTo), never assumed from the object's name.
#
# Salesforce's one cross-field rule: a Task whose Who is a LEAD cannot also
# have a What (a lead is not yet attached to anything).
# ---------------------------------------------------------------------------
RELATION_SLOTS = ("who", "what")


def relation_ref(obj, record_id, record_name="", source="manual", person_key=""):
    """One side of a relationship, validated against the discovered object.
    Raises SchemaError; returns {slot, object_api_name, object_label,
    record_id, record_name, source, person_key}."""
    if not obj:
        raise SchemaError("invalid_object", "That Salesforce object has not been "
                          "discovered in this org.", "object_api_name")
    if not meeting_relationship_supported(obj):
        raise SchemaError("invalid_object", f"Meetings cannot be related to "
                          f"{obj.get('label_plural') or obj.get('api_name')} in this org.",
                          "object_api_name")
    if not valid_record_id(record_id, obj.get("key_prefix")):
        raise SchemaError("invalid_record", f"That is not a {obj.get('label') or 'Salesforce'} "
                          "record.", "record_id")
    slot = obj.get("task_relation") or ("who" if obj["api_name"] in PERSON_OBJECTS else "what")
    return {
        "slot": slot,
        # False: a meeting can be about this record, but Salesforce will not
        # attach a Task to it (activities are off for the object).
        "tasks_supported": supports_tasks(obj),
        "object_api_name": obj["api_name"],
        "object_label": _s(obj.get("label"), 255) or obj["api_name"],
        "record_id": _s(record_id),
        "record_name": _s(record_name, 255),
        "source": source,
        "person_key": _s(person_key, 400),
    }


def combine_relations(base=None, override=None):
    """{who, what} from a base relationship with one side replaced.

    `override` (a relation_ref) replaces the side it belongs to and keeps the
    other — "about the Big Deal, still with Asha". Choosing a LEAD drops the
    What (Salesforce's rule); choosing a What while the Who is a Lead drops
    the Lead, because the person just chose the record explicitly."""
    out = {slot: (dict(base[slot]) if isinstance(base, dict) and base.get(slot) else None)
           for slot in RELATION_SLOTS}
    if override:
        out[override["slot"]] = dict(override)
        if override["slot"] == "who" and override["object_api_name"] == "Lead":
            out["what"] = None
        elif override["slot"] == "what" and (out.get("who") or {}).get("object_api_name") == "Lead":
            out["who"] = None
    return out


def task_relations(relations):
    """The part of a relationship a Salesforce Task can carry: sides whose
    object does not allow activities are dropped. Returns ({who, what},
    dropped_refs)."""
    out, dropped = {}, []
    for slot in RELATION_SLOTS:
        ref = (relations or {}).get(slot)
        if ref and ref.get("tasks_supported") is False:
            dropped.append(ref)
            ref = None
        out[slot] = ref
    return out, dropped


def primary_relation(relations):
    """The single record a pre-1.2 receiver understands: the What when there
    is one (the business record), else the Who."""
    relations = relations or {}
    return relations.get("what") or relations.get("who") or None


def relation_label(ref):
    """What a person sees for one side: the record's name, never its id."""
    if not ref:
        return ""
    return _s(ref.get("record_name")) or _s(ref.get("object_label"))


def valid_record_id(record_id, key_prefix):
    rid = _s(record_id)
    return bool(_RECORD_ID_RE.match(rid)) and bool(key_prefix) and rid.startswith(key_prefix)


def active_picklist_values(field):
    return {v["value"] for v in (field or {}).get("picklist_values") or []}


# A MinuteX task's status/priority -> the Salesforce Task picklist value that
# means the same thing, tried in order. Only values ACTIVE in this org's
# discovered picklist are ever returned; anything else yields "" so Salesforce
# applies its own default rather than MinuteX sending a value the org rejects.
TASK_STATUS_EQUIVALENTS = {
    "open": ("Not Started", "Open"),
    "in progress": ("In Progress",),
    "completed": ("Completed",),
}
TASK_PRIORITY_EQUIVALENTS = {
    "low": ("Low",),
    "medium": ("Normal", "Medium"),
    "high": ("High",),
}


def equivalent_picklist_value(value, field, equivalents):
    """The org's active picklist value equivalent to a MinuteX `value`, or ""."""
    active = active_picklist_values(field)
    if not active:
        return ""
    by_lower = {v.lower(): v for v in active}
    for candidate in equivalents.get(_s(value).lower(), ()) + (_s(value),):
        hit = by_lower.get(candidate.lower())
        if hit:
            return hit
    return ""


# ---------------------------------------------------------------------------
# Due dates
# ---------------------------------------------------------------------------

def parse_iso_date(value, field="due_date"):
    raw = _s(value)
    if not raw:
        return ""
    if not _ISO_DATE_RE.match(raw):
        raise SchemaError("invalid_due_date", f"{field} must be YYYY-MM-DD.", field)
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise SchemaError("invalid_due_date", f"{field} is not a real date.", field)
    return raw


def meeting_started_at(recorded_at):
    """recorded_at (epoch string or ISO, both live in Recordings) -> ISO, or ""."""
    raw = _s(recorded_at)
    if not raw:
        return ""
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def compute_due_date(rule, *, meeting_started_at="", today=None):
    """The due date a mapping's rule yields, as YYYY-MM-DD, or ""."""
    rule = rule if isinstance(rule, dict) else {}
    kind = rule.get("type") or "none"
    if kind == "none":
        return ""
    days = int(rule.get("days") or 0)
    base = today or datetime.now(timezone.utc).date()
    if kind == "days_after_meeting" and meeting_started_at:
        try:
            base = datetime.fromisoformat(
                str(meeting_started_at).replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return (base + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

def _validate_extra_fields(extra, task_fields):
    if extra is None:
        return {}
    if not isinstance(extra, dict) or len(extra) > 20:
        raise SchemaError("invalid_field", "extra_fields must be an object of at most "
                          "20 Task fields.", "extra_fields")
    out = {}
    for name, value in extra.items():
        field = task_fields.get(name)
        if field is None or name.lower() in MANAGED_TASK_FIELDS:
            raise SchemaError("invalid_field", f"Task has no settable field named "
                              f"{str(name)[:80]}.", f"extra_fields.{str(name)[:80]}")
        if not field.get("createable") or field.get("type") not in EXTRA_FIELD_TYPES:
            raise SchemaError("invalid_field", f"Task.{name} cannot be set by this "
                              "integration user.", f"extra_fields.{name}")
        if field.get("type") == "picklist" and value is not None \
                and str(value) not in active_picklist_values(field):
            raise SchemaError("invalid_field", f"{str(value)[:40]} is not an active value "
                              f"of Task.{name}.", f"extra_fields.{name}")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise SchemaError("invalid_field", f"Task.{name} needs a plain value.",
                              f"extra_fields.{name}")
        out[name] = value
    return out


def validate_picklist(value, field, key):
    value = _s(value, 255)
    if value and field is not None and value not in active_picklist_values(field):
        raise SchemaError(f"invalid_{key}", f"{value[:40]} is not an active Task "
                          f"{key} value in this Salesforce org.", key)
    return value


def validate_mapping(data, *, get_object, task_fields, get_person, current=None):
    """A normalized mapping, or SchemaError.

    get_object(api_name) -> discovered OBJ# item or None
    task_fields          -> {api_name: field} for Task (discovered)
    get_person(key)      -> People Store row or None
    `current` is the stored mapping on update: omitted keys keep its values.
    """
    data = data if isinstance(data, dict) else {}
    base = dict(current or {})

    def pick(key, default=None):
        return data[key] if key in data else base.get(key, default)

    name = _s(pick("name"), 80)
    if not name:
        raise SchemaError("invalid_mapping", "name is required.", "name")

    object_name = _s(pick("object_api_name"), 80)
    obj = get_object(object_name) if is_api_name(object_name) else None
    if obj is None:
        raise SchemaError("invalid_object", f"{object_name[:80] or 'object_api_name'} is "
                          "not an object discovered in this Salesforce org.",
                          "object_api_name")
    if not obj.get("queryable"):
        raise SchemaError("invalid_object", f"The integration user cannot read "
                          f"{obj.get('label_plural') or object_name}.", "object_api_name",
                          status=403)
    if not obj.get("task_relation"):
        raise SchemaError("invalid_object", f"Tasks cannot be related to "
                          f"{obj.get('label_plural') or object_name} in this org.",
                          "object_api_name")

    subject = _s(pick("subject"), 255)
    if not subject:
        raise SchemaError("invalid_mapping", "subject is required.", "subject")
    description = _s(pick("description"), 32000)
    status = validate_picklist(pick("status"), task_fields.get("Status"), "status")
    priority = validate_picklist(pick("priority"), task_fields.get("Priority"), "priority")

    assignee_key = _s(pick("default_assignee_person_key"), 400)
    if assignee_key:
        _require_assignee(get_person(assignee_key), "default_assignee_person_key")

    due_rule = pick("due_rule") or {"type": "none"}
    if not isinstance(due_rule, dict) or due_rule.get("type") not in DUE_RULES:
        raise SchemaError("invalid_due_rule", "due_rule.type must be one of "
                          f"{', '.join(DUE_RULES)}.", "due_rule")
    days = due_rule.get("days", 0)
    if due_rule["type"] != "none" and (not isinstance(days, int) or isinstance(days, bool)
                                       or not 0 <= days <= MAX_DUE_DAYS):
        raise SchemaError("invalid_due_rule", f"due_rule.days must be 0-{MAX_DUE_DAYS}.",
                          "due_rule.days")
    due_rule = {"type": due_rule["type"], "days": days if due_rule["type"] != "none" else 0}

    record_required = pick("record_required", True)
    if not isinstance(record_required, bool):
        raise SchemaError("invalid_mapping", "record_required must be true or false.",
                          "record_required")
    default_record_id = _s(pick("default_record_id"), 18)
    if default_record_id and not valid_record_id(default_record_id, obj.get("key_prefix")):
        raise SchemaError("invalid_record", f"default_record_id is not a "
                          f"{obj.get('label') or object_name} record id.",
                          "default_record_id")
    enabled = pick("enabled", True)
    if not isinstance(enabled, bool):
        raise SchemaError("invalid_mapping", "enabled must be true or false.", "enabled")

    return {
        "name": name,
        "object_api_name": obj["api_name"],
        "subject": subject,
        "description": description,
        "status": status,
        "priority": priority,
        "default_assignee_person_key": assignee_key,
        "due_rule": due_rule,
        "record_required": record_required,
        "default_record_id": default_record_id,
        "default_record_name": _s(pick("default_record_name"), 255),
        "extra_fields": _validate_extra_fields(pick("extra_fields"), task_fields),
        "enabled": enabled,
    }


def _require_assignee(person, field):
    if not person or person.get("provider") != "salesforce" or person.get("type") != "user":
        raise SchemaError("invalid_assignee", "The assignee must be a Salesforce User "
                          "from the synced People list.", field)
    if person.get("status") != "active":
        raise SchemaError("invalid_assignee", "The assignee is not an active Salesforce "
                          "User.", field)
    if not _RECORD_ID_RE.match(_s(person.get("external_id"))) \
            or not person["external_id"].startswith("005"):
        raise SchemaError("invalid_assignee", "The assignee has no valid Salesforce User "
                          "id.", field)
    return person


def require_assignee(person, field="assignee_person_key"):
    return _require_assignee(person, field)
