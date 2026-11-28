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

Between the runs only the extraction changed: a 16,384-token budget and one cause per item (run 1 bundled causes, such as "low paper or ink, the cover is open, or the paper is jammed"). Scorer, judge and labels did not change.

### Why recall is 38.9%

| Reason | Catalog causes | What it means |
| --- | --- | --- |
| Misdirection by design | `vpn-account-locked`, `av-remote-vpn-expired`, `wifi-password-expired`, `wifi-device-noncompliant` | No vendor article says "your VPN fails because your account is locked". Only live checks (week 3) can find these. |
| Company-specific state | `vpn-gateway-outage`, `email-service-outage`, `install-catalog-outage`, `app-update-catalog-degraded`, `printing-service-outage`, `wifi-service-degraded` | Our own services being down is not in public docs. The status page check covers them. |
| Weak sources | All four VPN causes, all three antivirus causes | The seed's VPN link is a Wi-Fi article (1 cause extracted); the antivirus source is a general Windows security page (0 causes). Fixable with better sources. |
| Catalog states, not causes | `printer-offline` | Microsoft's offline-printer article treats "offline" as the symptom and lists its causes (network, queue, spooler, the Use Printer Offline setting). The judge correctly did not match them. |
| Calls that never finished | `slow-*`, `install-*` partly | 2 calls hit 16,384 tokens; reasoning did not converge. |

Per-issue recall (run 2): disk space, phishing, MFA 100%; app updates 66.7%; overheating, account locked 50%; Outlook, slow system, printer 33.3%; VPN, Wi-Fi, install, antivirus 0%.

The finding for the README: public documentation explains roughly 4 in 10 of the causes this company actually sees, and none of the misdirected ones. Knowledge narrows the search; checks decide.
