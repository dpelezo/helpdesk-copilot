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
