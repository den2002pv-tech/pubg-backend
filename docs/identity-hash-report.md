# Screenshot identity/hash diagnostics

The JSON report returned by `POST /admin/screenshot-tests/run` uses schema version 2. Existing summary and case fields remain present; new diagnostics are emitted for each fixture card marked `identity_verified: true`.

## Per-card decisions

- `matched_ok`: the catalog match equals the expected ID.
- `wrong_id`: the catalog selected a different ID.
- `unmatched_no_candidates`: the catalog is empty or has no usable candidate hashes.
- `unmatched_low_strong`: the best candidate failed the strong/acceptable hash-vote requirements.
- `unmatched_low_margin`: the runner-up is too close to the best candidate.
- `unmatched_color_reject`: frame color was missing or failed the color gate.
- `unmatched_outlier_hash`: the candidate failed the outlier-hash gate.
- `unmatched_other`: no usable detected crop or a failure outside the matcher gates.

The diagnostics use the existing matcher ranking and thresholds; they do not introduce a second set of thresholds.

## Aggregates

- Hamming distances are better when lower. Median and p90 hash distances use correct matches only.
- A smaller correct-match margin means the best candidate is closer to its runner-up and is more ambiguous.
- `top_confusions` groups expected IDs with either the wrong selected ID (`wrong_id`) or the runner-up (`runner_up`). `count` is frequency; `avg_score` and `avg_margin` summarize candidate score and runner-up gap.
- Empty metric samples are represented by `null`, not zero.
