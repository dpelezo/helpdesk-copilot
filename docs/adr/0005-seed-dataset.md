# 0005. The seed dataset is raw material, not ground truth

- Status: accepted
- Date: 2026-11-27

## Context

[UmerSajid/IT-Troubleshooting-Dataset](https://huggingface.co/datasets/UmerSajid/IT-Troubleshooting-Dataset) has 10,500 rows. Its card says it was AI-generated, no license is stated, and many rows repeat one issue under "Variant N" names. It describes how issues are usually solved, not what is wrong with a specific employee's device.

## Decision

- Download it at build time from a pinned revision. Never commit the data.
- Strip the variant suffixes, drop the Urdu column (it repeats the English text), and report the number of canonical issues, not rows.
- Use it as input to Workflow A and as the search corpus for how-to questions.
- The diagnosis answer key comes from a fake company with a fault injector that plants one known root cause per ticket. Held-out tickets are split by canonical issue, so near-duplicates never cross the split.

## Consequences

- Accuracy numbers are not inflated by duplicate rows.
- If the seed collapses to very few unique issues, gold procedures are written by hand from vendor documentation.
