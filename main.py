"""BROKEN on purpose until restored — see MAIN_RESTORE.md

This branch has identity_diagnostics.py and identity-diagnostics-main.patch.
Restore main from feature/card-counter and apply the patch (commands in MAIN_RESTORE.md).
"""
raise SystemExit(
    "main.py is not restored on feature/identity-match-diagnostics. "
    "See MAIN_RESTORE.md: git show origin/feature/card-counter:main.py > main.py "
    "&& git apply identity-diagnostics-main.patch"
)
