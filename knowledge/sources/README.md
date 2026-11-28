# Sources

What Workflow A extracts knowledge from (issue #25). `python -m app.knowledge.sources` builds `manifest.json`; the `sources` workflow runs it in GitHub Actions.

| Origin | What | Count |
| --- | --- | ---: |
| `seed` | Documentation links the seed dataset gives for the 11 in-scope seed issues | 7 |
| `curated` | Specific Microsoft and Apple articles from `curated.yml`, for issues the seed's links miss | 11 |
| `local` | The IT policy handbook, for the two identity issues | 1 |

The seed's links are mostly home pages (`support.apple.com/`, `support.hp.com/`) or the wrong article, so 7 of 11 seed issues had no usable source until the curated list was added. Two seed links still fail and stay in the manifest with the reason: HP needs JavaScript, and Mozilla answers with a bot challenge.

**What is committed:** only the manifest (URL, final URL, date, status, title, sha256, size, issues the page is about, origin, reason) and the curated list. Vendor pages are not ours to redistribute, so their text is cached in `/data/sources/` during a run and never committed (ADR 0005). Extraction stores short evidence spans with their URL, not pages.

**Rules:** only `https`, only the vendors the seed links to (redirects within the same vendor are followed, others refused); curated entries only from `microsoft.com` or `apple.com`, each with a reason; pages under 500 characters of text count as failed. A test fails if any in-scope issue is left without a usable source.
