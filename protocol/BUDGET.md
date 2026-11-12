# BUDGET.md

Version 1 - observed 2026-11-12

Rate limits as reported by each provider during `te smoke`. Where a provider
returns no limit header, the engine uses the conservative default in Guard:
1 request per second per host, 4 in flight overall.

| Provider | Reported limit | Default applied |
| --- | --- | --- |
| claude-cli | none reported | subprocess, serial |
| greenhouse | none reported | 1 rps |
| lever | none reported | 1 rps |
| ashby | none reported | 1 rps |
| https (generic) | none reported | 1 rps per host |
| playwright | n/a | 1 page at a time |
| sqlite | n/a | local |

Re-run `te smoke` and update this file if a provider starts returning limit
headers or begins blocking.
