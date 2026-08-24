#!/usr/bin/env python3
# =============================================================
# fake_dynamodb.py — an in-memory DynamoDB Table good enough to test the
# organization layer against, offline.
#
# WHY THIS EXISTS
#   tests/test_ai_workspace.py stubs the Recordings table with a MagicMock,
#   which is fine for code that only does get_item/update_item and never looks
#   at the result. The Contacts/Folders/Tasks code cannot be tested that way:
#   its correctness IS its use of DynamoDB semantics —
#     * conditional writes are how folder-name uniqueness and task idempotency
#       are enforced (a MagicMock never fails a condition, so every race test
#       would pass vacuously);
#     * GSI queries are how every ownership-scoped list works (a MagicMock
#       returns the same thing for every index);
#     * UpdateExpression SET/REMOVE with aliased names is how partial updates
#       avoid reserved-word collisions.
#   So this implements those three things faithfully enough that a test failure
#   means a real bug, and stops well short of being a DynamoDB clone.
#
# WHAT IS FAITHFUL
#   * Key schema: hash-only or hash+range, from the table definition.
#   * put_item / get_item / delete_item / update_item / query / scan.
#   * ConditionExpression: the handful of forms this codebase actually uses —
#     attribute_not_exists(x), attribute_exists(x), "#a = :v", and OR of those.
#     An unsupported expression RAISES rather than silently passing, so a new
#     condition form can never be quietly untested.
#   * ConditionalCheckFailedException / ValidationException as ClientError,
#     with the same response shape botocore produces.
#   * GSI queries, including SPARSE index behavior: an item missing an index
#     key attribute (or holding "" for it) is NOT in that index — which is the
#     exact property the Contacts email/phone dedupe relies on.
#   * ExclusiveStartKey / LastEvaluatedKey paging, ScanIndexForward, Limit.
#   * Empty-string index keys REJECTED on write, like the real service.
#
# WHAT IS NOT
#   No Decimal coercion, no item-size limits, no throughput, no streams, no
#   transactions, no FilterExpression (this codebase filters in Python), no
#   projection enforcement (ProjectionExpression is accepted and ignored —
#   every index here projects ALL anyway).
# =============================================================
import copy
import re


class ClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError.

    str() includes the message because production code matches on the message
    text to tell a missing-parent-map ValidationException apart from a genuine
    expression bug (see _save_task in the userApi). A stub whose str() dropped
    it would let that path pass a test it fails in production.
    """

    def __init__(self, code, message="", operation_name="PutItem"):
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(
            f"An error occurred ({code}) when calling the "
            f"{operation_name} operation: {message}")


class _Cond:
    """What boto3.dynamodb.conditions.Key(...).eq(...) builds.

    Supports `&` so `Key("a").eq(x) & Key("b").eq(y)` composes exactly like
    the real thing — that is the shape every range-key query in the code uses.
    """

    def __init__(self, terms):
        self.terms = terms  # [(name, op, value), ...]

    def __and__(self, other):
        return _Cond(self.terms + other.terms)


class Key:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return _Cond([(self.name, "eq", value)])

    def begins_with(self, value):
        return _Cond([(self.name, "begins_with", value)])

    def lt(self, value):
        return _Cond([(self.name, "lt", value)])

    def lte(self, value):
        return _Cond([(self.name, "lte", value)])

    def gt(self, value):
        return _Cond([(self.name, "gt", value)])

    def gte(self, value):
        return _Cond([(self.name, "gte", value)])


def _matches(item, name, op, value):
    if name not in item:
        return False
    actual = item[name]
    if op == "eq":
        return actual == value
    if op == "begins_with":
        return str(actual).startswith(str(value))
    if op == "lt":
        return actual < value
    if op == "lte":
        return actual <= value
    if op == "gt":
        return actual > value
    if op == "gte":
        return actual >= value
    raise AssertionError(f"fake_dynamodb: unsupported operator {op!r}")


class FakeTable:
    """One in-memory table.

    indexes: {index_name: (hash_key, range_key_or_None)}
    """

    def __init__(self, name, hash_key, range_key=None, indexes=None):
        self.name = name
        self.hash_key = hash_key
        self.range_key = range_key
        self.indexes = indexes or {}
        self.items = {}          # {key_tuple: item}
        # Call counters — tests use these to prove an N+1 was avoided, and to
        # prove the mirror write actually happened.
        self.calls = {"put": 0, "get": 0, "update": 0, "delete": 0,
                      "query": 0, "scan": 0}

    # -- keys ------------------------------------------------------------
    def _key_of(self, item):
        if self.range_key:
            return (item[self.hash_key], item[self.range_key])
        return (item[self.hash_key],)

    def _key_from_arg(self, key):
        if self.range_key:
            return (key[self.hash_key], key[self.range_key])
        return (key[self.hash_key],)

    # -- conditions ------------------------------------------------------
    def _check_condition(self, expr, item, names, values):
        """Evaluate the ConditionExpression forms this codebase uses.

        Anything else raises AssertionError on purpose: a condition form the
        fake does not understand must not silently evaluate to True, or the
        test would prove nothing.
        """
        if not expr:
            return True
        expr = expr.strip()

        # OR of two clauses (used by the task-seeding conditional write).
        if " OR " in expr:
            return any(self._check_condition(part, item, names, values)
                       for part in expr.split(" OR "))
        if " AND " in expr:
            return all(self._check_condition(part, item, names, values)
                       for part in expr.split(" AND "))

        m = re.fullmatch(r"attribute_not_exists\(([#\w.]+)\)", expr)
        if m:
            return self._resolve_path(m.group(1), item, names) is _MISSING
        m = re.fullmatch(r"attribute_exists\(([#\w.]+)\)", expr)
        if m:
            return self._resolve_path(m.group(1), item, names) is not _MISSING
        m = re.fullmatch(r"([#\w.]+)\s*=\s*(:\w+)", expr)
        if m:
            actual = self._resolve_path(m.group(1), item, names)
            return actual != _MISSING and actual == values[m.group(2)]
        raise AssertionError(
            f"fake_dynamodb: unsupported ConditionExpression {expr!r}")

    def _resolve_path(self, path, item, names):
        cur = item
        for part in path.split("."):
            real = names.get(part, part) if part.startswith("#") else part
            if not isinstance(cur, dict) or real not in cur:
                return _MISSING
            cur = cur[real]
        return cur

    # -- operations ------------------------------------------------------
    def put_item(self, Item, ConditionExpression=None,
                 ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        self.calls["put"] += 1
        self._reject_empty_index_keys(Item)
        k = self._key_of(Item)
        existing = self.items.get(k, {})
        if not self._check_condition(ConditionExpression, existing,
                                     ExpressionAttributeNames or {},
                                     ExpressionAttributeValues or {}):
            raise ClientError("ConditionalCheckFailedException",
                              "The conditional request failed", "PutItem")
        self.items[k] = copy.deepcopy(Item)
        return {}

    def _reject_empty_index_keys(self, item):
        """The real service refuses an empty-string GSI key. Reproducing that
        is the whole point of the sparse-index tests — code that writes "" for
        an optional index key must fail here exactly as it would in AWS."""
        for index_name, (hk, rk) in self.indexes.items():
            for attr in (hk, rk):
                if attr and attr in item and item[attr] == "":
                    raise ClientError(
                        "ValidationException",
                        f"One or more parameter values are not valid. The "
                        f"AttributeValue for a key attribute cannot contain an "
                        f"empty string value. IndexName: {index_name}, "
                        f"Key: {attr}", "PutItem")

    def get_item(self, Key, **kwargs):
        self.calls["get"] += 1
        item = self.items.get(self._key_from_arg(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def delete_item(self, Key, ConditionExpression=None,
                    ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, **kwargs):
        self.calls["delete"] += 1
        k = self._key_from_arg(Key)
        existing = self.items.get(k, {})
        if not self._check_condition(ConditionExpression, existing,
                                     ExpressionAttributeNames or {},
                                     ExpressionAttributeValues or {}):
            raise ClientError("ConditionalCheckFailedException",
                              "The conditional request failed", "DeleteItem")
        self.items.pop(k, None)
        return {}

    def update_item(self, Key, UpdateExpression="",
                    ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None,
                    ConditionExpression=None, **kwargs):
        """SET / REMOVE, including nested paths (`#tasks.#t = :task`) and
        if_not_exists(x, :v) + :n — the three forms this codebase writes."""
        self.calls["update"] += 1
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        k = self._key_from_arg(Key)
        item = self.items.get(k)
        created = False
        if item is None:
            # DynamoDB's update_item upserts.
            item = dict(zip([self.hash_key] + ([self.range_key]
                                               if self.range_key else []), k))
            created = True

        if not self._check_condition(ConditionExpression, {} if created else item,
                                     names, values):
            raise ClientError("ConditionalCheckFailedException",
                              "The conditional request failed", "UpdateItem")

        working = copy.deepcopy(item)
        expr = UpdateExpression.strip()
        set_part, remove_part = "", ""
        mset = re.search(r"\bSET\b(.*?)(?=\bREMOVE\b|$)", expr, re.S)
        mrem = re.search(r"\bREMOVE\b(.*)$", expr, re.S)
        if mset:
            set_part = mset.group(1)
        if mrem:
            remove_part = mrem.group(1)

        for clause in _split_top_level(set_part):
            if not clause.strip():
                continue
            lhs, rhs = clause.split("=", 1)
            path = lhs.strip()
            rhs = rhs.strip()
            # if_not_exists(attr, :v) + :n  — the counter-bump form, used by
            # speaker_mapping_version.
            m = re.fullmatch(r"if_not_exists\(([#\w.]+),\s*(:\w+)\)\s*\+\s*(:\w+)",
                             rhs)
            # if_not_exists(attr, :v)  — the plain insert-only form, used all
            # over request_upload ("set it if this row is new, otherwise leave
            # what's there"). Without this the fake rejected the real upload
            # path outright, which is how it hid a folder-filing test.
            m2 = None if m else re.fullmatch(
                r"if_not_exists\(([#\w.]+),\s*(:\w+)\)", rhs)
            if m:
                current = self._resolve_path(m.group(1), working, names)
                base = values[m.group(2)] if current is _MISSING else current
                new_value = base + values[m.group(3)]
            elif m2:
                current = self._resolve_path(m2.group(1), working, names)
                new_value = values[m2.group(2)] if current is _MISSING else current
            elif rhs in values:
                new_value = values[rhs]
            else:
                raise AssertionError(
                    f"fake_dynamodb: unsupported SET rhs {rhs!r}")
            self._assign_path(path, working, names, new_value)

        for clause in _split_top_level(remove_part):
            path = clause.strip()
            if path:
                self._remove_path(path, working, names)

        self._reject_empty_index_keys(working)
        self.items[k] = working
        return {"Attributes": copy.deepcopy(working)}

    def _assign_path(self, path, item, names, value):
        parts = path.split(".")
        cur = item
        for part in parts[:-1]:
            real = names.get(part, part)
            if real not in cur or not isinstance(cur[real], dict):
                # Real DynamoDB refuses to create a missing parent map — the
                # userApi's _save_task/_save_document depend on exactly this
                # error to know it must create the map first.
                raise ClientError(
                    "ValidationException",
                    f"The document path provided in the update expression is "
                    f"invalid for update", "UpdateItem")
            cur = cur[real]
        cur[names.get(parts[-1], parts[-1])] = value

    def _remove_path(self, path, item, names):
        parts = path.split(".")
        cur = item
        for part in parts[:-1]:
            real = names.get(part, part)
            if not isinstance(cur, dict) or real not in cur:
                return
            cur = cur[real]
        if isinstance(cur, dict):
            cur.pop(names.get(parts[-1], parts[-1]), None)

    def query(self, KeyConditionExpression=None, IndexName=None, Limit=None,
              ScanIndexForward=True, ExclusiveStartKey=None,
              ProjectionExpression=None, **kwargs):
        self.calls["query"] += 1
        if IndexName:
            if IndexName not in self.indexes:
                raise ClientError("ValidationException",
                                  f"The table does not have the specified "
                                  f"index: {IndexName}", "Query")
            hk, rk = self.indexes[IndexName]
        else:
            hk, rk = self.hash_key, self.range_key

        terms = KeyConditionExpression.terms if KeyConditionExpression else []
        rows = []
        for item in self.items.values():
            # SPARSE index: an item lacking the index's key attributes simply
            # is not in the index.
            if hk not in item or item.get(hk) in ("", None):
                continue
            if rk and (rk not in item or item.get(rk) in ("", None)):
                continue
            if all(_matches(item, n, o, v) for n, o, v in terms):
                rows.append(copy.deepcopy(item))

        sort_attr = rk or hk
        rows.sort(key=lambda r: str(r.get(sort_attr, "")),
                  reverse=not ScanIndexForward)

        start = 0
        if ExclusiveStartKey:
            for i, r in enumerate(rows):
                if all(str(r.get(a)) == str(v)
                       for a, v in ExclusiveStartKey.items() if a in r):
                    start = i + 1
                    break
        page = rows[start:]
        out = {"Items": page[:Limit] if Limit else page,
               "Count": len(page[:Limit] if Limit else page)}
        if Limit and len(page) > Limit:
            last = page[Limit - 1]
            lek = {hk: last.get(hk)}
            if rk:
                lek[rk] = last.get(rk)
            lek[self.hash_key] = last.get(self.hash_key)
            if self.range_key:
                lek[self.range_key] = last.get(self.range_key)
            out["LastEvaluatedKey"] = lek
        return out

    def scan(self, Limit=None, ExclusiveStartKey=None,
             ProjectionExpression=None, **kwargs):
        self.calls["scan"] += 1
        rows = [copy.deepcopy(i) for i in self.items.values()]
        rows.sort(key=lambda r: str(self._key_of(r)))
        start = 0
        if ExclusiveStartKey:
            for i, r in enumerate(rows):
                if self._key_of(r) == self._key_from_arg(ExclusiveStartKey):
                    start = i + 1
                    break
        page = rows[start:]
        out = {"Items": page[:Limit] if Limit else page}
        if Limit and len(page) > Limit:
            out["LastEvaluatedKey"] = {
                k: v for k, v in page[Limit - 1].items()
                if k in (self.hash_key, self.range_key) and v is not None}
        return out


class _Missing:
    def __repr__(self):
        return "<MISSING>"


_MISSING = _Missing()


def _split_top_level(text):
    """Split a SET/REMOVE clause list on commas that are not inside
    parentheses — `if_not_exists(a, :b)` must stay one clause."""
    out, depth, buf = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return out


# The five organization-layer tables plus Recordings, with the exact key
# schemas scripts/31_create_workspace_tables.sh creates. Kept here so a test
# and the provisioning script cannot disagree about an index name.
def build_tables():
    return {
        "recordings": FakeTable(
            "Recordings", "audio_s3_key",
            indexes={"user-index": ("user_id", "created_at"),
                     "device-index": ("device_id", "created_at")}),
        "contacts": FakeTable(
            "Contacts", "contact_id",
            indexes={"owner-index": ("owner_user_id", "created_at"),
                     "owner-email-index": ("owner_user_id", "email_lc"),
                     "owner-phone-index": ("owner_user_id", "phone_e164")}),
        "folders": FakeTable(
            "Folders", "folder_id",
            indexes={"owner-index": ("owner_user_id", "name_lc")}),
        "folder_contacts": FakeTable(
            "FolderContacts", "folder_id", "contact_id",
            indexes={"contact-index": ("contact_id", "folder_id")}),
        "participants": FakeTable(
            "MeetingParticipants", "audio_s3_key", "speaker_id",
            indexes={"contact-index": ("contact_id", "audio_s3_key")}),
        "tasks": FakeTable(
            "Tasks", "task_id",
            indexes={"owner-index": ("owner_user_id", "created_at"),
                     "meeting-index": ("source_recording_id", "created_at"),
                     "folder-index": ("folder_id", "created_at"),
                     "assignee-index": ("assignee_contact_id", "created_at"),
                     "dedupe-index": ("owner_user_id", "fingerprint")}),
        "users": FakeTable(
            "Users", "user_id",
            indexes={"email-index": ("email", None)}),
    }
