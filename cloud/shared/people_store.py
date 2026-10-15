"""people_store.py — the generic MinuteX People Store.

WHAT THIS IS. A per-workspace directory of people known to an external
system (today: Salesforce Contacts and Users, via the Site receiver's
people_sync action). Tagging a meeting participant reads THIS table — it
never calls the external system — so tagging stays fast and works when the
customer's org is unreachable.

TABLE (scripts/77): People
  PK workspace_id   the tenant boundary, exactly like every other table
  SK person_key     "{provider}#{type}#{external_id}" (site_contract.person_key)

Provider-neutral by construction: `provider` + `external_id` are the only
place a source system appears, and nothing here knows what a Salesforce id
looks like.

WHY NOT THE Contacts TABLE. Contacts is MinuteX's own address book — people
someone deliberately added, with notes, avatars and task links, edited by
people. A mirror of an external directory has different rules: the source is
authoritative, rows disappear when the source deletes them, and a sync must
never overwrite a note someone typed. Tagging bridges the two on demand: the
first time a person is tagged, a Contact is found (by email) or created and
linked back here (linked_contact_id), so everything downstream — speaker
names, task resolution, notifications — keeps working off Contacts unchanged.

STATUS. "active"/"inactive" come from the source (a deactivated Salesforce
User is inactive). "deleted" is ours: a person absent from a COMPLETE sync
run. Deleted rows are kept (a past meeting may still reference them) but
never offered for new tagging.

This module performs I/O only through the `table` it is handed, so tests use
the offline fake and the Lambda passes the real boto3 Table.
"""
import site_contract

STATUS_DELETED = "deleted"

# Bounded partition reads. A search reads the workspace partition in pages;
# this caps one search so a very large directory cannot stall the request.
SEARCH_MAX_ROWS = 5000
# The run stamp of a person saved because a user picked them from LIVE
# search (not from a sync). A sync never "misses" such a person: it never
# fetched them, so mark_missing must leave them alone.
LIVE_RUN_ID = "live_search"
MARK_MISSING_MAX_ROWS = 100_000

_SOURCE_FIELDS = ("provider", "type", "external_id", "name", "email", "phone",
                  "title", "company", "status", "source_updated_at")


def _lc(value):
    return " ".join(str(value or "").lower().split())


def get_person(table, workspace_id, person_key):
    if not workspace_id or not person_key:
        return None
    return table.get_item(Key={"workspace_id": workspace_id,
                               "person_key": person_key}).get("Item")


def upsert_people(table, workspace_id, raw_people, run_id, now):
    """Write one page of people. Returns counts.

    Idempotent: re-running a page changes nothing but last_synced_at and the
    run stamp. Source fields are overwritten (the source is authoritative);
    MinuteX-owned fields (linked_contact_id, first_synced_at) are preserved.
    """
    # users/contacts: how many of each type this page accepted (what the
    # settings screen shows as "Users: N, Contacts: M").
    counts = {"received": 0, "created": 0, "updated": 0, "unchanged": 0,
              "skipped": 0, "users": 0, "contacts": 0}
    seen = set()
    for raw in raw_people or []:
        counts["received"] += 1
        person = site_contract.normalize_person(raw)
        if person is None or person["person_key"] in seen:
            counts["skipped"] += 1
            continue
        seen.add(person["person_key"])
        type_key = person["type"] + "s"
        if type_key in counts:
            counts[type_key] += 1

        existing = get_person(table, workspace_id, person["person_key"]) or {}
        row = dict(existing)
        row.update({k: person[k] for k in _SOURCE_FIELDS})
        row.update({
            "workspace_id": workspace_id,
            "person_key": person["person_key"],
            "name_lc": _lc(person["name"]),
            "last_synced_at": now,
            "last_sync_run": run_id,
            "first_synced_at": existing.get("first_synced_at") or now,
        })
        # Omitted when empty rather than stored as "", like every other
        # optional lookup attribute in this codebase.
        if person["email"]:
            row["email_lc"] = person["email"]
        else:
            row.pop("email_lc", None)

        changed = any(existing.get(k) != row.get(k)
                      for k in _SOURCE_FIELDS + ("email_lc",))
        table.put_item(Item=row)
        if not existing:
            counts["created"] += 1
        elif changed or existing.get("status") == STATUS_DELETED:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
    return counts


def _partition(table, key_cls, workspace_id, max_rows):
    # key_cls is boto3's conditions.Key, passed in by the caller (the same
    # seam push_delivery.user_rows uses) so this module never imports boto3.
    query = {"KeyConditionExpression": key_cls("workspace_id").eq(workspace_id)}
    rows = 0
    while True:
        res = table.query(**query)
        for row in res.get("Items", []):
            rows += 1
            yield row
            if rows >= max_rows:
                return
        last = res.get("LastEvaluatedKey")
        if not last:
            return
        query["ExclusiveStartKey"] = last


def mark_missing(table, key_cls, workspace_id, provider, run_id, now):
    """After a COMPLETE run: every person of `provider` not seen in `run_id`
    is marked deleted. Never called for a partial run — a person on a page
    not yet fetched is not missing."""
    marked = 0
    prefix = f"{provider}#"
    for row in _partition(table, key_cls, workspace_id, MARK_MISSING_MAX_ROWS):
        if not str(row.get("person_key", "")).startswith(prefix):
            continue
        if row.get("last_sync_run") in (run_id, LIVE_RUN_ID)                 or row.get("status") == STATUS_DELETED:
            continue
        table.update_item(
            Key={"workspace_id": workspace_id, "person_key": row["person_key"]},
            UpdateExpression="SET #s = :d, deleted_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": STATUS_DELETED, ":now": now},
        )
        marked += 1
    return marked


def link_contact(table, workspace_id, person_key, contact_id, now):
    table.update_item(
        Key={"workspace_id": workspace_id, "person_key": person_key},
        UpdateExpression="SET linked_contact_id = :c, linked_at = :now",
        ExpressionAttributeValues={":c": contact_id, ":now": now},
    )


def public_person(row):
    return {
        "person_key": row.get("person_key", ""),
        "provider": row.get("provider", ""),
        "type": row.get("type", ""),
        "external_id": row.get("external_id", ""),
        "name": row.get("name", ""),
        "email": row.get("email", ""),
        "phone": row.get("phone", ""),
        "title": row.get("title", ""),
        "company": row.get("company", ""),
        "status": row.get("status", ""),
        "last_synced_at": row.get("last_synced_at", ""),
        "linked_contact_id": row.get("linked_contact_id", ""),
    }


def search_people(table, key_cls, workspace_id, query="", person_type="", limit=50,
                  include_inactive=False, counts=None):
    """People matching `query` (name or email substring), best matches first.

    Returns (people, truncated). `truncated` is True when the partition had
    more rows than one search reads, so a caller can ask for a narrower
    query instead of concluding the person does not exist.

    `counts`, when a dict is passed, is filled with how many offerable people
    of each type the store holds ({"contacts": n, "users": n}) — from the SAME
    read, before the query/type filter, so a screen's "Contacts 23 · Users 4"
    tabs cost nothing extra and always agree with what a search can return.
    """
    needle = _lc(query)
    matches = []
    scanned = 0
    if counts is not None:
        counts.update({f"{t}s": 0 for t in site_contract.PEOPLE_TYPES})
    for row in _partition(table, key_cls, workspace_id, SEARCH_MAX_ROWS + 1):
        scanned += 1
        if scanned > SEARCH_MAX_ROWS:
            break
        status = row.get("status")
        if status == STATUS_DELETED:
            continue
        if status == "inactive" and not include_inactive:
            continue
        if counts is not None and f"{row.get('type')}s" in counts:
            counts[f"{row.get('type')}s"] += 1
        if person_type and row.get("type") != person_type:
            continue
        name_lc = row.get("name_lc") or ""
        email_lc = row.get("email_lc") or ""
        if needle and needle not in name_lc and needle not in email_lc:
            continue
        # Prefix matches rank above substring matches; then alphabetical.
        rank = 0 if not needle or name_lc.startswith(needle) \
            or email_lc.startswith(needle) else 1
        matches.append((rank, name_lc, row))
    matches.sort(key=lambda m: (m[0], m[1]))
    return ([public_person(m[2]) for m in matches[:max(1, int(limit))]],
            scanned > SEARCH_MAX_ROWS)
