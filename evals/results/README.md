# Eval results

Root-cause accuracy on the **100 held-out tickets** (`evals/tickets/heldout.jsonl`), scored by `app/evals/score.py`. Chance (a uniform guess over each issue's causes plus escalate) is **26.9%**.

| Condition | What the model gets | Model | Root-cause accuracy | Right cause retrieved (top 5) | Date |
| --- | --- | --- | ---: | ---: | --- |
| A. Keyword search (baseline) | BM25 over causes and handbook, one call, no checks | `zai-org/GLM-5.2` | **29.0%** | 82.6% | 2026-11-28 |
| B. Graph retrieval | *week 2* | | | | |
| C. Graph + checks | *week 3* | | | | |

## A. Baseline: what it shows

The right cause was among the five documents shown to the model for 82.6% of tickets, yet accuracy is barely above chance. Retrieval finds every possible cause for an issue, but without checking the systems the model cannot tell which one is planted. The misdirected tickets (a VPN failure caused by a locked account) are the clearest case: `network--vpn-not-connecting` scored 10%.

Per issue, from `baseline-bm25-heldout.json`: overheating 66.7%, account locked 55.6%, antivirus 40.0%, phishing 36.4%, install 30.0%, Outlook 20.0%, Wi-Fi 20.0%, MFA 10.0%, VPN 10.0%, slow system 9.1%. Escalation precision 6.5%, recall 25.0%.

## Run history

| Date | Condition | Held-out accuracy | Note |
| --- | --- | ---: | --- |
| 2026-11-28 | A, first run | 25.0% | 21 of 100 replies cut off at a 1,024-token limit by reasoning Telnyx cannot switch off. Not a model result. |
| 2026-11-28 | A | **29.0%** | Limit raised to 4,096; prompt, documents and scorer unchanged. 3 replies still cut off, scored as wrong. |

Each run: one model call per ticket, temperature 0, `zai-org/GLM-5.2` on Telnyx. The JSON files hold every ticket's retrieved documents, prediction, tokens and latency.

## Extraction (Workflow A, issue #26)

GLM-5.2 read the 17 usable sources (`knowledge/sources/manifest.json`) and extracted symptoms, causes, diagnostics and solutions, each with a quote from the source. The trusted judge (100% on 24 hand labels) matched extracted causes to the 36 catalog causes. Full output: `extraction.json` (short quotes and source ids only; no page text).

| Run | Calls failed | Items kept | Evidence valid (causes) | Cause recall | Extra causes |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 (2026-11-28) | 5 of 22, cut off at 8,192 tokens | 286 | 98.7% | 25.0% | 64 |
| 2 (2026-11-28) | 2 of 22, cut off at 16,384 tokens | 438 | 99.1% | **38.9%** | 98 |
| 3 (2026-11-28) | 3 of 25, cut off at 16,384 tokens | 476 | 100.0% | **38.9%** | 99 |
| 4 (2026-11-28), new judge | 5 of 25, cut off at 16,384 tokens | 372 | 100.0% | **41.7%** | 74 |

Run 3 added three official Microsoft sources (Always On VPN troubleshooting, two Defender update articles) for the two issues at 0%. VPN rose from 0% to 25% (the expired certificate is now found) and antivirus from 0 to 9 extracted causes, but overall recall stayed at 38.9%.

Run 4 is the first with the one-call judge (#39) and the token counts (#37); sources and extraction were unchanged from run 3. Recall moved from 38.9% to 41.7% (15 of 36 causes), but the judge also changed, so the two numbers are not a clean comparison, and five calls never finished. All three calls on the run 3 VPN and Defender sources were among them, so VPN and antivirus are back at 0%: a run-to-run effect, not a source effect.

| Run 4 tokens | Calls | Prompt | Completion |
| --- | ---: | ---: | ---: |
| Extraction | 25 | 69,458 | 178,479 |
| Judge | 101 | 15,040 | 25,979 |

**The five unfinished calls used 81,920 of the 178,479 extraction completion tokens (46%) and produced nothing.** Each hit the 16,384 limit while reasoning, including on an 841-token Defender page. Capping reasoning, not the judge, is now the main cost lever. The judge dropped to 41k tokens for 101 calls.

**Run-to-run variation is large per issue.** With unchanged sources, Wi-Fi went from 12 extracted causes to 0 (a call cut off at the token limit), app updates from 66.7% to 33.3%, phishing from 100% to 50%, and printer from 33.3% to 66.7%. Reasoning varies at temperature 0 and some calls never finish. A single run is not a reliable per-issue number: the next step is to run extraction several times and report the minimum and mean, and to keep the union of what every run found as the draft for review.

Between runs 1 and 2 only the extraction changed: a 16,384-token budget and one cause per item (run 1 bundled causes, such as "low paper or ink, the cover is open, or the paper is jammed"). Scorer, judge and labels did not change.

### Why recall is 38.9%

| Reason | Catalog causes | What it means |
| --- | --- | --- |
| Misdirection by design | `vpn-account-locked`, `av-remote-vpn-expired`, `wifi-password-expired`, `wifi-device-noncompliant` | No vendor article says "your VPN fails because your account is locked". Only live checks (week 3) can find these. |
| Company-specific state | `vpn-gateway-outage`, `email-service-outage`, `install-catalog-outage`, `app-update-catalog-degraded`, `printing-service-outage`, `wifi-service-degraded` | Our own services being down is not in public docs. The status page check covers them. |
| Weak sources | All four VPN causes, all three antivirus causes | The seed's VPN link is a Wi-Fi article (1 cause extracted); the antivirus source is a general Windows security page (0 causes). Fixable with better sources. |
| Catalog states, not causes | `printer-offline`, `av-definitions-stale` | Microsoft treats "printer offline" and "definitions out of date" as symptoms and lists their causes (network, queue, spooler; blocked update URLs, stopped services, a third-party antivirus). The judge correctly did not match them. |
| Calls that never finished | `slow-*`, `install-*` partly | 2 calls hit 16,384 tokens; reasoning did not converge. |

Per-issue recall (run 2): disk space, phishing, MFA 100%; app updates 66.7%; overheating, account locked 50%; Outlook, slow system, printer 33.3%; VPN, Wi-Fi, install, antivirus 0%.

The finding for the README: public documentation explains roughly 4 in 10 of the causes this company actually sees, and none of the misdirected ones. Knowledge narrows the search; checks decide.
