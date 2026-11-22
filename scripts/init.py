#!/usr/bin/env python3
"""Initialize Personal Memory from the bundled starter templates.

Copy starter files into place:

    examples/rules.json  ->  learning/rules.json      (capture rules)
    examples/public.md   ->  user-profile/public.md   (general memory template)
    examples/private.md  ->  user-profile/private.md  (private memory template)

Existing files are never overwritten, so repeated runs preserve existing data.
After setup, scripts/ may be removed if no longer needed. Keep playbooks/ to
retain the linked initialization and rule-learning workflows.

Usage:
    python3 scripts/init.py          # POSIX
    python  scripts\\init.py          # Windows
"""

import os
import shutil
import sys
from pathlib import Path

POSIX = os.name == "posix"
# The parent of scripts/ is the skill root.
ROOT = Path(__file__).absolute().parent.parent

# (template source, destination), relative to ROOT.
PLAN = [
    ("examples/rules.json", "learning/rules.json"),
    ("examples/public.md", "user-profile/public.md"),
    ("examples/private.md", "user-profile/private.md"),
]


def secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if POSIX:
        os.chmod(path, 0o700)


def main() -> int:
    created, skipped, missing = [], [], []
    for src_rel, dst_rel in PLAN:
        src = ROOT / src_rel
        dst = ROOT / dst_rel
        if not src.exists():
            missing.append(src_rel)
            continue
        if dst.exists():
            skipped.append(dst_rel)
            continue
        secure_mkdir(dst.parent)
        shutil.copyfile(src, dst)
        if POSIX:
            os.chmod(dst, 0o600)
        created.append(dst_rel)

    for rel in created:
        print(f"  Created  {rel}")
    for rel in skipped:
        print(f"  Skipped  {rel} (already exists; unchanged)")
    for rel in missing:
        print(f"  Missing  {rel} (template not found; skipped)")

    print()
    if created:
        print("Starter files created. Restart OpenCode after installing the plugin.")
    elif not missing:
        print("No setup needed: all destination files already exist.")
    if missing:
        print("Some templates are missing; existing data was left unchanged.")
    print("After setup, scripts/ may be removed. Keep playbooks/ for its workflows.")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
