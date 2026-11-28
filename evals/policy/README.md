# Policy questions

30 questions an employee might ask, answered from the IT policy handbook (`knowledge/handbook/it-policy.md`). They measure **routing accuracy** (each must go to policy retrieval, not troubleshooting) and **citation correctness** (the answer must cite the listed sections and state the listed facts).

| Split | Topics | Questions | Use |
| --- | --- | --- | --- |
| tuning | Devices, Printing | 8 | Free to use while building |
| heldout | Passwords and sign-in, VPN and remote work, Software, Email and phishing | 22 | **Never used for tuning** |

Each line of `questions.jsonl` has `question`, `route` (`policy`), `sections` (ids the answer must cite) and `must_mention` (facts the answer must state, quoted from those sections).

Tests check that every cited section exists, every fact appears in the cited sections, every handbook section is asked about at least once, the splits share no topic or section, and the handbook agrees with the mock world (password age, lockout threshold, mailbox quota, printers, VPN versions, approval rules).

These are eval ground truth (`AGENTS.md`): change them only in their own pull request.
