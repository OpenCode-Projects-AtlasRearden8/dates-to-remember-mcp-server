# dates-to-remember-mcp-server

A tiny [MCP](https://modelcontextprotocol.io) server (stdio, Python stdlib only — no pip installs) for dates that sneak up on you.

Tell it about your people and your renewals, and your AI agent can:

- **add** people with yearly events: birthdays plus renewal types (**car**, **insurance**, **warranty**, or custom) — each with its own lead time, so it can *nudge you three weeks before* instead of the night before
- **list** everyone, sorted by whose event is closest
- **query days-remaining** for one person
- **surface upcoming** obligations inside any window (default 21 days)
- **produce a season birthday-card batch list**: who gets a card next, sorted by mail date, with addresses on file

State lives in one JSON file on your disk — local-first, offline-capable, no accounts.
From **Asdale Dynamics, LLC** (see [dylanasdale.github.io](https://dylanasdale.github.io)): plain-English AI, tested tools, MIT licensed.

## How dates that repeat are handled

- Every event is an annual recurrence (month + day). Feel free to buy a real calendar too — this server only does yearlies.
- `days_remaining` uses **whole days**; today = 0.
- **Feb 29 birthdays repeat on Feb 28** in non-leap years, so leap-day humans are never skipped.
- Lead time default is **21 days** (three weeks) per renewal; birthdays use the same default in `upcoming`.

## Tools

| Tool | What it does |
| --- | --- |
| `add_person` | Add a person: `name`, optional `birthday {month, day}`, optional `address`, `notes`, and `renewals [{type, label?, month, day, lead_days?}]` |
| `list_people` | Everyone with each event's next date, days remaining, and active nudges |
| `days_remaining` | `{name}` → all of that person's events, soonest first |
| `upcoming` | `{within_days?}` → every event due in the window, with ready-to-read nudges |
| `birthday_card_batch` | `{within_days? (default 90)}` → season card list with addresses + `batch_text` you can paste straight into a task |
| `remove_person` | `{name}` → remove a person and their events |

Every tool accepts an optional `path` (relative to the storage directory) so you can keep several files — e.g. `family.json`, `business.json` — in one folder.

## Smoke-test guide (5 minutes)

1. **Check you have Python 3.10+** (built and tested on 3.13):

   ```bash
   python3 --version
   ```

2. **Run the offline test suite** (fast, no network):

   ```bash
   python3 -m unittest -v test_server      # 45 tests, < 1 second
   ```

3. **Try a real stdio round-trip** with the included sample data:

   ```bash
   mkdir -p /tmp/dates_smoke && cp samples/sample_data.json /tmp/dates_smoke/people.json
   printf '%s\n' \
     '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
     '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
     '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"birthday_card_batch","arguments":{"within_days":130}}}' \
     | python3 server.py /tmp/dates_smoke
   ```

   You should get two JSON lines back; the second contains Mom and leap-year cousin Riley's card batch.

4. **Wire it into an MCP client** (Claude Desktop, opencode, Zed, any stdio-capable one). Example config:

   ```json
   {
     "mcpServers": {
       "dates-to-remember": {
         "command": "python3",
         "args": ["/full/path/to/server.py", "/path/to/dates_data"]
       }
     }
   }
   ```

   The storage directory is created on first run; seed it by copying
   `samples/sample_data.json` inside and rename to `people.json`, or just
   start `add_person`-ing. Restart the client to load the server.

## Storage, path safety, backups

- One JSON file, default `people.json`, written atomically (temp file + rename).
- **Path safety**: every `path` argument is resolved against the storage directory. Absolute paths, `~`, backslashes, `..` escapes, null bytes, and symlinks are refused. Deep subfolders are fine (`agents/car.json` works).
- **Auto-backup**: before every write the current file is copied to `backups/`; the newest 10 are kept.
- Corrupt files fail loudly (`DatesError`) instead of silently wiping your people.
- Sample file included: [`samples/sample_data.json`](samples/sample_data.json) — fake people, real structure.

## Fits the family, no overlap

| Server | Handles |
| --- | --- |
| `checklists-mcp-server` | reusable step-by-step procedure checklists |
| `receipt-bucket-mcp-server` | receipts and spending buckets |
| **`dates-to-remember-mcp-server`** | **annual dates: birthdays + renewals with lead-time nudges** |

## Tests

`test_server.py` runs fully offline (dates are injected, nothing sleeps or waits):

- recurrence math: year boundaries, Feb 29 → Feb 28 handling, invalid months/days
- lead-time logic: default 21-day nudge, per-event lead times, outside-window silence
- security: path traversal (`../`, `....//`, mid-path escapes), absolute paths, `~`, backslashes, null bytes, symlink-ish paths, control characters in names
- storage: atomic save, auto-backup + cap pruning, corrupt-file refusal
- MCP protocol: initialize handshake, tools/list, error routing, LSP-framing rejection
- a real subprocess stdio round-trip against the sample file

## Known limits

- Annual **term** renewals that aren't a fixed month/day (e.g. "inspection due every 90 days from purchase") are out of scope; model them by their current month/day or use the checklists server.
- No email/SMS — nudges are returned through MCP; your agent decides how it tells you.
- Disaster recovery = any recent file in `backups/`.

MIT License — Copyright (c) 2026 Asdale Dynamics, LLC.
