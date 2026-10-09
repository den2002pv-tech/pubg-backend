# Counter recognition fixtures

Real labelled examples are added by the admin page's **Tests counter** tab after a screenshot scan.

- `cases.json` stores the manually verified quantity, detector output, app commit, card metadata, detector diagnostics, and image SHA-256.
- `images/` stores the card crop passed to the counter detector, not the entire screenshot.
- Every labelled save commits both the image and updated manifest directly to `feature/card-counter` (or `COUNTER_TEST_BRANCH` if configured).
- Do not edit `expected_quantity` to match a wrong detector result; it must reflect the actual inventory.
- A saved case that the detector gets wrong is expected to fail the regression test until the algorithm is fixed.

Run locally after installing `requirements.txt`:

```sh
python -m unittest discover -s tests
```

The manifest starts empty. The test suite reports the fixture-specific tests as skipped until real cases are saved.
