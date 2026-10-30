# Restore main.py on feature/identity-match-diagnostics

`main.py` could not be uploaded whole via the chat GitHub connector (size limit).
Helpers and the patch are already on this branch.

## Fix (local, ~30 seconds)

```bash
git fetch origin
git checkout feature/identity-match-diagnostics
git show origin/feature/card-counter:main.py > main.py
git apply identity-diagnostics-main.patch
git add main.py
git commit -m "fix: restore main.py and wire identity diagnostics"
git push origin feature/identity-match-diagnostics
```

Optional cleanup after restore:

```bash
rm -rf _main_parts MAIN_RESTORE.md identity-diagnostics-main.patch
# keep identity_diagnostics.py and tests
```

Verify:

```bash
python -m unittest tests.test_identity_match_diagnostics -v
```
