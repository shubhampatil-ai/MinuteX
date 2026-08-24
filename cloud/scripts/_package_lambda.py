#!/usr/bin/env python3
"""_package_lambda.py — zip one Lambda's handler plus the shared AI core.

Used by scripts/28_deploy_async_stt.sh. A separate file rather than an inline
heredoc for the same reason as _merge_env.py: `( cd ... && python - <<'PY' )`
is mis-parsed by the bash build on this machine, which reports the resulting
syntax error at an unrelated line much further down the script.

The shared modules are written FLAT at the archive root (ai_schema.py,
groq_client.py, prompts.py, stt_result.py, transcript_store.py) because that is
how `import ai_schema` resolves inside the Lambda runtime — the handler's own
directory is on sys.path, subdirectories are not. Editors flag those imports as
unresolved locally; that is expected and does not affect the deployed function.

    argv[1]  the source dir, relative to the repo root (e.g. "functions/userapi")
    argv[2]  the repo root (optional; defaults to this file's parent's parent)
"""
import sys
import zipfile
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: _package_lambda.py <src_dir> [project_root]")
    src_dir = sys.argv[1]
    root = (Path(sys.argv[2]) if len(sys.argv) > 2
            else Path(__file__).resolve().parents[1])

    handler = root / src_dir / "lambda_function.py"
    if not handler.is_file():
        sys.exit(f"no handler at {handler}")
    shared = root / "shared"
    out = root / src_dir / "function.zip"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(handler, "lambda_function.py")
        for mod in sorted(shared.glob("*.py")):
            z.write(mod, mod.name)
            print(f"   + {mod.name}")
    print(f">> packaged {out.relative_to(root)} "
          f"({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
