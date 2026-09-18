# A blind challenge to the oracle

A separate worker received only the public backend source: no tests, oracle,
original mutation list, or parent conversation. It proposed twelve small
faults. Both existing suites were frozen before execution.

The added seven-check oracle catches **3/12**, not the 7/8 seen on its initial
author-shared challenges. The upstream suite catches five, has three
setup/error-contaminated outcomes, and misses four. Together they catch seven;
three remain unresolved and two survive both. H09 independently repeats the
original count-filter fault, so only eleven challenges are novel.

The consequential survivor is **H10: public signup creates administrators**.
H12, dropping `exclude_unset` during partial update, also survives. These are
seeded defects, not vulnerabilities claimed to exist in the upstream release.
The added oracle constructs users directly and signs setup credentials; that
boundary bypasses the signup and credential lifecycle it does not specify.

[The matrix](results/holdout.json) retains every result, including errors;
[faults and witnesses](holdout-faults.json) show exactly what changed.
No observed error is relabeled a clean assertion kill. This is a challenge set,
not a random sample or an estimate of production defect recall.

```sh
uv run experiments/fullstack/holdout.py --subject "$ORIGINAL_PINNED_SUBJECT"
```

The original seven-check oracle remains unchanged so this result is replayable.
