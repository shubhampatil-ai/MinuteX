#!/usr/bin/env python3
"""_package_release.py — build every Lambda zip from ONE source tree, and
compare a built zip with the code that is live. Used by
scripts/94_redeploy_from_tag.sh, which hands it a tree exported from a git
tag so production never again runs code that exists only on one laptop.

A separate file rather than a heredoc for the same reason as
_package_lambda.py: `( cd ... && python - <<'PY' )` is mis-parsed by the bash
build on this machine.

Each zip is built exactly as that function's own deploy script builds it:

    userApi              handler + every shared/*.py             (_package_lambda.py)
    transcribeRecording  handler + every shared/*.py             (_package_lambda.py)
    crmSyncWorker        handler + userapi as userapi_worker.py
                         + every shared/*.py                     (65_deploy_crm_sync_worker.sh)
    getUploadUrl         handler + the shared modules it imports,
                         transitively (today: none)              (20_deploy_device_mgmt.sh)

Usage:
    _package_release.py build <cloud_dir> <out_dir>
        -> <out_dir>/<function dir>.zip for each function, one line each:
           "<function dir> <base64 sha256>"  (the form Lambda's CodeSha256 uses)
    _package_release.py diff <live_zip> <built_zip>
        -> the files whose content differs (line endings ignored), or "same"
"""
import base64
import hashlib
import re
import sys
import zipfile
from pathlib import Path

# function dir -> how its zip is assembled
FUNCTIONS = ("userapi", "transcribe", "crm-sync-worker", "device-presign")


def _imports_of(path):
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r"^(?:import|from)[ \t]+([A-Za-z_][A-Za-z0-9_]*)",
                          src, re.MULTILINE))


def _needed_shared(handler, shared):
    """The shared modules `handler` imports, followed transitively."""
    available = {m.stem: m for m in shared.glob("*.py")}
    needed, queue = {}, list(_imports_of(handler))
    while queue:
        name = queue.pop()
        if name in needed or name not in available:
            continue
        needed[name] = available[name]
        queue.extend(_imports_of(available[name]))
    return [needed[k] for k in sorted(needed)]


def _entries(cloud, fn):
    """(source path, name inside the zip) for one function."""
    shared = cloud / "shared"
    handler = cloud / "functions" / fn / "lambda_function.py"
    if not handler.is_file():
        sys.exit(f"no handler at {handler}")
    out = [(handler, "lambda_function.py")]
    if fn == "device-presign":
        mods = _needed_shared(handler, shared)
    else:
        mods = sorted(shared.glob("*.py"))
    if fn == "crm-sync-worker":
        out.append((cloud / "functions/userapi/lambda_function.py",
                    "userapi_worker.py"))
    out += [(m, m.name) for m in mods]
    return out


def _sha(path):
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode()


def build(cloud, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for fn in FUNCTIONS:
        out = out_dir / f"{fn}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for src, name in _entries(cloud, fn):
                z.write(src, name)
        print(f"{fn} {_sha(out)}")


def diff(live, built):
    a, b = zipfile.ZipFile(live), zipfile.ZipFile(built)
    names = sorted(set(a.namelist()) | set(b.namelist()))
    changed = []
    for n in names:
        if n not in a.namelist():
            changed.append(f"+ {n} (new)")
        elif n not in b.namelist():
            changed.append(f"- {n} (removed)")
        elif (a.read(n).replace(b"\r\n", b"\n")
              != b.read(n).replace(b"\r\n", b"\n")):
            changed.append(f"~ {n}")
    print("\n".join(changed) if changed else "same")


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 4 and sys.argv[1] == "diff":
        diff(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
