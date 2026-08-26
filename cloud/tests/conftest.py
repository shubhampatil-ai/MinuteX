"""conftest.py — ONE set of AWS stubs, installed before any test module.

THE BUG THIS FIXES. Every offline test file has to stub boto3/botocore before
importing a Lambda, because the Lambda builds its DynamoDB resources at import
time. Each file did that independently, and each defined its OWN ClientError
look-alike, ending with:

    sys.modules["botocore.exceptions"] = <that file's stub>

Run one file and it works. Run the directory and they collide — not on import
(the stubs are interchangeable as far as importing goes), but on IDENTITY:

    test_ai_workspace  imports lambda_function  -> api.ClientError is stub A
    test_workspace_org overwrites the module    -> its tests raise stub B
    api's `except ClientError:` tests against A -> stub B sails straight past

The failure is invisible in isolation and only appears in a full run, which is
the worst possible shape for a test suite: the errors look like product bugs in
retry/idempotency paths that are actually correct. test_ai_workspace.py:2940
already carried a comment warning about exactly this hazard.

THE FIX. conftest.py is imported by pytest BEFORE any test module in this
directory, so the stubs it installs are the ones every Lambda binds at import
time. There is now exactly one ClientError class in the process, and
`except ClientError` catches what the tests raise.

fake_dynamodb.ClientError is the canonical one, chosen because it is the only
stub already SHARED by more than one test file, and because the fake tables
raise it from inside — a different class there would break the same way.

Each test file keeps its own sys.modules assignments; they are now idempotent
by construction (see _canonical below), so a file still runs standalone with
`python tests/test_x.py` exactly as before. Nothing in the product changed to
make these tests pass, and no test's intent was altered — only which class
object all of them share.
"""

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "shared", ROOT / "functions/userapi",
             Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import fake_dynamodb as fdb  # noqa: E402


def _canonical():
    """Install the shared boto3/botocore stubs, once per process.

    Idempotent on purpose: a test module that runs its own stub setup after
    this (they all still do, so each file works standalone) must not end up
    replacing the ClientError the Lambda has already bound. Returning the
    SAME module objects every time makes those later assignments no-ops.
    """
    exc = sys.modules.get("botocore.exceptions")
    if exc is not None and getattr(exc, "ClientError", None) is fdb.ClientError:
        return

    boto3_stub = sys.modules.get("boto3") or mock.MagicMock()
    sys.modules["boto3"] = boto3_stub
    sys.modules["boto3.dynamodb"] = sys.modules.get(
        "boto3.dynamodb") or mock.MagicMock()

    conditions = sys.modules.get("boto3.dynamodb.conditions") \
        or mock.MagicMock()
    # fake_dynamodb.Key composes with `&` exactly like the real condition
    # objects, which is the shape every range-key query in the code uses.
    conditions.Key = fdb.Key
    sys.modules["boto3.dynamodb.conditions"] = conditions

    exc = exc or mock.MagicMock()
    exc.ClientError = fdb.ClientError
    sys.modules["botocore"] = sys.modules.get("botocore") or mock.MagicMock()
    sys.modules["botocore.exceptions"] = exc
    # functions/transcribe also does `from botocore.config import Config`.
    sys.modules["botocore.config"] = sys.modules.get(
        "botocore.config") or mock.MagicMock()


_canonical()
