# Counter recognition fixtures

Real labelled examples are added from the admin page's **Tests counter** tab after a screenshot scan. These are regression-test assets stored in Git; they are separate from PostgreSQL product analytics events.

- `cases.json` stores each manually verified quantity, detector output captured at scan time, app commit, card metadata, detector diagnostics, and image SHA-256.
- `images/` stores the exact card crop passed to the counter detector, not the entire screenshot.
- Saving a labelled batch appends cases to the existing manifest and commits the images plus updated manifest to `feature/card-counter` (or `COUNTER_TEST_BRANCH` if configured). The application retains the most recent 500 cases; saving does not reset earlier cases.
- Do not edit `expected_quantity` to match a wrong detector result; it must reflect the actual inventory.
- A saved case whose detector output was wrong is valuable regression data. Keep it until the current detector passes the fixture or the label/image is independently proven invalid.
- The recorded `detected_quantity` and `correct` fields describe the output at the time the case was saved. They are not proof of how the current algorithm performs; `tests/test_counter_fixtures.py` reruns the current detector against the stored image and compares it with `expected_quantity`.

## Current manifest

Check `cases.json` for the current case count and contents. The manifest may already contain labelled examples; it does not necessarily start empty. In the checked version, it contains 37 cases, including cases where the quantity recorded at capture differed from the manually verified quantity. Do not clear the manifest just to reset scan statistics.

## Run the tests

Install the development dependencies and run:

```sh
python -m pytest -v tests/test_counter_fixtures.py
python -m pytest -v tests
```

The full CI checks also compile Python files and run `python -m pytest -q` on Python 3.12. The dedicated counter workflow runs on Python 3.11. If fixture tests fail, inspect the actual failure output and fix the detector; do not change expected quantities to make the tests pass.
