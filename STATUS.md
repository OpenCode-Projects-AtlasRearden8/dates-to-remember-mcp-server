# STATUS — dates-to-remember-mcp-server

Built and verified 2026-10-26. All work by Asdale Dynamics, LLC.

## What was built

A small MCP server (JSON-RPC 2.0 over stdio, Python 3.10+ stdlib only, zero
dependencies) for dates that sneak up: birthdays and yearly renewals
(car, insurance, warranty, custom) with per-event lead times so an agent
can nudge three weeks ahead. Complements `checklists-mcp-server` and
`receipt-bucket-mcp-server` with no domain overlap.

Files:

- `server.py` — full server: validation, recurrence math, storage, 6 tools, MCP plumbing
- `test_server.py` — 45 offline unit tests (no network, no sleeps, injected dates)
- `samples/sample_data.json` — fictional sample people; copy into a data dir as `people.json` to seed
- `README.md` — beginner-friendly guide + 5-minute smoke-test
- `LICENSE` — MIT, Copyright (c) 2026 Asdale Dynamics, LLC

Tools: `add_person`, `list_people`, `days_remaining`, `upcoming`,
`birthday_card_batch` (season card list with addresses + paste-ready
`batch_text`), `remove_person`.

## How to run it

```bash
python3 server.py ./dates_data          # stdio; storage dir created if missing
python3 -m unittest -v test_server      # offline tests
```

Point any stdio MCP client (e.g. Claude Desktop) at:

```json
{ "mcpServers": { "dates-to-remember": {
  "command": "python3", "args": ["/path/to/server.py", "/path/to/dates_data"] } } }
```

## Verification results

- `python3 -m py_compile server.py` — clean
- `python3 -m unittest test_server` — **Ran 45 tests ... OK** (< 0.1 s)
- Manual stdio smoke test against the sample file: initialize,
  tools/list, upcoming / birthday_card_batch / days_remaining / add_person
  all returned well-formed JSON; sample batch correctly listed Mom and
  leap-year cousin Riley (Feb 29 → Feb 28 in off years)
- Security cases exercised by tests: `../` traversal, mid-path escapes,
  absolute paths, `~`, backslashes, null bytes, control chars in names,
  symlink refusal, corrupt-file refusal, LSP-framing rejection

## Known gaps

- Only fixed month/day annual recurrences; variable-term things
  ("every 90 days from purchase") should be modeled as their annual
  month/day or handled by the checklists server.
- No notification channel: reminders travel as MCP tool results; the
  agent decides how (and whether) to ping you.
- Backups are timestamp-ordered copies (10 kept), not a diff history.
- `list_people`/`upcoming` numbers depend on the server process's local
  time zone (standard for local-first tools).
