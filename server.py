#!/usr/bin/env python3
"""dates-to-remember-mcp-server: a tiny MCP server (stdio, Python stdlib only).

Dates that sneak up on you: keep a list of people with birthdays and
renewable obligations (car, insurance, warranty, ...), each with a lead
time so your agent can nudge you weeks ahead instead of the night before.
Tools add, list, remove, query days-remaining, surface anything due soon,
and produce a season batch list for birthday cards.

Storage is a single JSON file inside a storage directory. Paths stay
inside that directory (no traversal, no absolute paths, no symlinks) and
every write is auto-backed-up first. No third-party dependencies.

Complements checklists-mcp-server and receipt-bucket-mcp-server:
different domain, shared style. Offline unit-testable without disabled
timestamps: all recurrence math takes ``today`` as a plain argument.

Copyright (c) 2026 Asdale Dynamics, LLC. MIT licensed.
"""

import json
import os
import shutil
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "dates-to-remember-mcp-server"
SERVER_VERSION = "1.0.0"

DEFAULT_DATA_FILE = "people.json"
BACKUP_DIR_NAME = "backups"
MAX_BACKUPS = 10
DEFAULT_LEAD_DAYS = 21  # "nudge me three weeks before"
MAX_LEAD_DAYS = 365
MAX_NAME_LEN = 120
MAX_TEXT_LEN = 600

RENEWAL_TYPES = ("car", "insurance", "warranty", "other")

TDAY = date.today()


class DatesError(ValueError):
    """User-facing error surfaced as a tool error response."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def safe_path(base: Path, user_path: str) -> Path:
    """Resolve user_path under base, refusing escapes and symlinks."""
    if not isinstance(user_path, str) or not user_path:
        raise DatesError("path must be a non-empty string")
    if "\x00" in user_path:
        raise DatesError("path cannot contain null bytes")
    if os.path.isabs(user_path) or user_path.startswith("~"):
        raise DatesError("absolute paths and ~ are not allowed")
    if "\\" in user_path:
        raise DatesError("path cannot contain backslashes")
    candidate = (base / user_path).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and not candidate.is_relative_to(base_resolved):
        raise DatesError(f"path escapes storage directory: {user_path!r}")
    if candidate == base_resolved:
        raise DatesError("path refers to the storage directory itself")
    if candidate.is_symlink():
        raise DatesError("symlinks are not allowed")
    return candidate


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _clean_text(value, label, max_len):
    if not isinstance(value, str):
        raise DatesError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise DatesError(f"{label} must not be empty")
    if any(ord(ch) < 32 for ch in text):
        raise DatesError(f"{label} must not contain control characters")
    if len(text) > max_len:
        raise DatesError(f"{label} too long (max {max_len} characters)")
    return text


def _optional_text(value, label, max_len):
    if value is None:
        return None
    return _clean_text(value, label, max_len)


MAX_RUNGS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _clean_date_parts(month, day):
    if not isinstance(month, int) or isinstance(month, bool):
        raise DatesError("month must be an integer")
    if not isinstance(day, int) or isinstance(day, bool):
        raise DatesError("day must be an integer")
    if not 1 <= month <= 12:
        raise DatesError(f"month must be 1-12, got {month}")
    if not 1 <= day <= MAX_RUNGS[month - 1]:
        raise DatesError(f"day {day} does not exist in month {month} (max Feb 29)")
    return month, day


def _clean_lead_days(value):
    if value is None:
        return DEFAULT_LEAD_DAYS
    if not isinstance(value, int) or isinstance(value, bool):
        raise DatesError("lead_days must be an integer")
    if not 0 <= value <= MAX_LEAD_DAYS:
        raise DatesError(f"lead_days must be 0-{MAX_LEAD_DAYS}, got {value}")
    return value


# ---------------------------------------------------------------------------
# Recurrence math (pure; takes today so tests stay offline/deterministic)
# ---------------------------------------------------------------------------


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year + (month == 12), (month % 12) + 1, 1) - date(year, month, 1)).days


def next_occurrence(month: int, day: int, today: date) -> date:
    """Next repeating yearly date on or after today.

    A Feb 29 event repeats on Feb 28 in non-leap years (last day of
    February), so invalid day numbers clamp gracefully and leap-day
    people are not skipped.
    """
    month, day = _clean_date_parts(month, day)
    for year in (today.year, today.year + 1):
        candidate = date(year, month, min(day, days_in_month(year, month)))
        if candidate >= today:
            return candidate
    raise DatesError("could not compute next occurrence")


def days_remaining(month: int, day: int, today: date = None) -> int:
    """Whole days until the next annual occurrence (0 = today)."""
    today = today if today is not None else TDAY
    return (next_occurrence(month, day, today) - today).days


def event_status(days: int, lead_days: int) -> str:
    """Human status for an event that is ``days`` away."""
    if days == 0:
        return "today"
    if days <= lead_days:
        return "nudge"
    return "later"


# ---------------------------------------------------------------------------
# Store: JSON file with auto-backup before every write
# ---------------------------------------------------------------------------


class Store:
    """One JSON document (people list) with backup and path safety."""

    def __init__(self, storage_dir: Path, data_file: str = DEFAULT_DATA_FILE):
        self.base = Path(storage_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.path = self.set_data_file(data_file)

    def set_data_file(self, data_file) -> Path:
        self.path = safe_path(self.base, data_file)
        return self.path

    def load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "people": []}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise DatesError(f"data file unreadable: {exc}") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("people"), list):
            raise DatesError("data file is not a valid people document")
        return doc

    def save(self, doc: dict) -> None:
        if not isinstance(doc, dict) or not isinstance(doc.get("people"), list):
            raise DatesError("refusing to save: invalid people document")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                backup_dir = self.base / BACKUP_DIR_NAME
                backup_dir.mkdir(exist_ok=True)
                stamp = f"{self.path.stem}-{date.today().isoformat()}.json"
                shutil.copy2(self.path, _unique_backup(backup_dir, stamp))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            tmp.replace(self.path)
            _prune_backups(self.base / BACKUP_DIR_NAME)


def _unique_backup(backup_dir: Path, stem: str) -> Path:
    for i in range(1, 101):
        candidate = backup_dir / f"{i:03d}-{stem}"
        if not candidate.exists():
            return candidate
    return backup_dir / stem


def _prune_backups(backup_dir: Path, keep: int = MAX_BACKUPS) -> None:
    backups = sorted(backup_dir.glob("*.json"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Person model + lookups
# ---------------------------------------------------------------------------


def _clean_renewal(raw) -> dict:
    if not isinstance(raw, dict):
        raise DatesError("each renewal must be an object")
    kind = _clean_text(raw.get("type"), "renewal type", 40).lower()
    if kind not in RENEWAL_TYPES and not kind.isalnum():
        raise DatesError(f"renewal type must be one of {RENEWAL_TYPES} or alphanumeric")
    label = _optional_text(raw.get("label"), "renewal label", MAX_TEXT_LEN) or kind
    month, day = _clean_date_parts(raw.get("month"), raw.get("day"))
    lead = _clean_lead_days(raw.get("lead_days"))
    return {
        "type": kind,
        "label": label,
        "month": month,
        "day": day,
        "lead_days": lead,
    }


def clean_person(source: dict, existing: dict = None) -> dict:
    """Validate/normalize a person record (new or edit of existing)."""
    existing = existing or {}
    name = _clean_text(source.get("name", existing.get("name")), "name", MAX_NAME_LEN)
    record = {"name": name}
    if "birthday" in source or "birthday" in existing:
        birthday = source.get("birthday", existing.get("birthday"))
        if birthday in (None, {}):
            record["birthday"] = None
        else:
            if not isinstance(birthday, dict):
                raise DatesError("birthday must be an object with month and day")
            bm, bd = _clean_date_parts(birthday.get("month"), birthday.get("day"))
            record["birthday"] = {"month": bm, "day": bd}
    else:
        record["birthday"] = None
    if "renewals" in source or "renewals" in existing:
        renewals = source.get("renewals", existing.get("renewals")) or []
        if not isinstance(renewals, list):
            raise DatesError("renewals must be a list")
        record["renewals"] = [_clean_renewal(r) for r in renewals]
    else:
        record["renewals"] = list(existing.get("renewals") or [])
    record["address"] = _optional_text(
        source.get("address", existing.get("address")), "address", MAX_TEXT_LEN
    )
    record["notes"] = _optional_text(
        source.get("notes", existing.get("notes")), "notes", MAX_TEXT_LEN
    )
    return record


def find_index(people: list, name: str):
    """Case-insensitive lookup by name; returns index or -1."""
    if not isinstance(name, str) or not name.strip():
        raise DatesError("name must be a non-empty string")
    wanted = name.strip().casefold()
    for i, person in enumerate(people):
        if str(person.get("name", "")).casefold() == wanted:
            return i
    return -1


def person_events(person: dict, today: date):
    """List of (event dict, next date, days remaining, status) for a person."""
    events = []
    if person.get("birthday"):
        b = person["birthday"]
        days = days_remaining(b["month"], b["day"], today)
        events.append(
            {
                "kind": "birthday",
                "label": "Birthday",
                "next": next_occurrence(b["month"], b["day"], today).isoformat(),
                "days_remaining": days,
                "lead_days": DEFAULT_LEAD_DAYS,
                "status": event_status(days, DEFAULT_LEAD_DAYS),
            }
        )
    for renewal in person.get("renewals", []):
        days = days_remaining(renewal["month"], renewal["day"], today)
        events.append(
            {
                "kind": renewal["type"],
                "label": renewal["label"],
                "next": next_occurrence(renewal["month"], renewal["day"], today).isoformat(),
                "days_remaining": days,
                "lead_days": renewal["lead_days"],
                "status": event_status(days, renewal["lead_days"]),
            }
        )
    return events


def _person_summary(person: dict, today: date) -> dict:
    events = person_events(person, today)
    events.sort(key=lambda e: e["days_remaining"])
    soonest = events[0] if events else None
    if soonest and soonest["status"] == "nudge":
        note = f"{person['name']}: {soonest['label']} in {soonest['days_remaining']} day(s)"
    elif soonest and soonest["status"] == "today":
        note = f"{person['name']}: {soonest['label']} is TODAY"
    else:
        note = f"{person['name']}: {soonest['label']} in {soonest['days_remaining']} day(s)" if soonest else f"{person['name']}"
    return {
        "name": person["name"],
        "address": person.get("address"),
        "notes": person.get("notes"),
        "birthday": person.get("birthday"),
        "renewals": person.get("renewals", []),
        "events": events,
        "soonest": soonest,
        "nudge": note,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def tool_definitions():
    return [
        {
            "name": "add_person",
            "description": (
                "Add a person to remember, with an optional birthday, optional "
                "mailing address (for cards), and optional yearly renewals "
                "(car, insurance, warranty, other) each with its own lead time. "
                "The lead time (default 21 days) is how early the agent nudges."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "birthday": {
                        "type": "object",
                        "properties": {
                            "month": {"type": "integer"},
                            "day": {"type": "integer"},
                        },
                    },
                    "renewals": {"type": "array"},
                    "address": {"type": "string"},
                    "notes": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "list_people",
            "description": (
                "List everyone on file with each yearly event, days remaining, "
                "and which ones are inside their nudge window, sorted by "
                "soonest."
            ),
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "days_remaining",
            "description": (
                "Query days remaining for one person: every event's next date, "
                "days remaining, and whether the lead-time nudge is active."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "upcoming",
            "description": (
                "Everything due within a window (default 21 days) across all "
                "people, sorted by days remaining. Use this to gather today's "
                "nudges: renewals to shop for, birthdays to mail."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "within_days": {"type": "integer", "minimum": 0, "maximum": 365},
                    "path": {"type": "string"},
                },
            },
        },
        {
            "name": "birthday_card_batch",
            "description": (
                "Season batch list for birthday cards: every birthday inside "
                "the next N days (default 90), sorted by date, with mailing "
                "address when on file and a ready-to-paste batch summary."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "within_days": {"type": "integer", "minimum": 1, "maximum": 365},
                    "path": {"type": "string"},
                },
            },
        },
        {
            "name": "remove_person",
            "description": "Remove a person (and their events) by name.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                "required": ["name"],
            },
        },
    ]


def call_tool(store: Store, name, arguments, today=None):
    """Dispatch one tools/call. Returns a plain result dict.

    Raises DatesError (or ValueError) for bad input: callers turn that
    into a tool error response.
    """
    today = today if today is not None else TDAY
    if not isinstance(arguments, dict):
        raise DatesError("arguments must be an object")
    path = arguments.get("path", DEFAULT_DATA_FILE)
    if not isinstance(path, str):
        raise DatesError("path must be a string")
    store.set_data_file(path)

    if name == "add_person":
        return add_person(store, arguments, today)
    if name == "list_people":
        return list_people(store, today)
    if name == "days_remaining":
        return days_remaining_tool(store, arguments, today)
    if name == "upcoming":
        return upcoming(store, arguments, today)
    if name == "birthday_card_batch":
        return birthday_card_batch(store, arguments, today)
    if name == "remove_person":
        return remove_person(store, arguments)
    raise DatesError(f"unknown tool: {name!r}")


def add_person(store: Store, arguments, today=None) -> dict:
    doc = store.load()
    name = arguments.get("name")
    today = today if today is not None else TDAY
    index = find_index(doc["people"], name)
    if index >= 0:
        raise DatesError(f"{name!r} is already on file; remove them first to replace")
    person = clean_person(
        {
            "name": name,
            "birthday": arguments.get("birthday"),
            "renewals": arguments.get("renewals") or [],
            "address": arguments.get("address"),
            "notes": arguments.get("notes"),
        }
    )
    events = person_events(person, today)
    if not events:
        raise DatesError("add at least one date: a birthday or a renewal")
    doc["people"].append(person)
    doc["saved_at"] = today.isoformat()
    store.save(doc)
    return {
        "added": person["name"],
        "events": events,
        "file": store.path.name,
    }


def list_people(store: Store, today: date) -> dict:
    doc = store.load()
    summaries = [_person_summary(p, today) for p in doc["people"]]
    summaries.sort(key=lambda s: (s["soonest"]["days_remaining"] if s["soonest"] else 10**9))
    nudges = [s["nudge"] for s in summaries if s["soonest"] and s["soonest"]["status"] in ("nudge", "today")]
    return {
        "count": len(summaries),
        "nudges": nudges,
        "people": summaries,
    }


def days_remaining_tool(store: Store, arguments, today: date) -> dict:
    doc = store.load()
    index = find_index(doc["people"], arguments.get("name"))
    if index < 0:
        raise DatesError(f"no person named {arguments.get('name')!r} on file")
    person = doc["people"][index]
    events = person_events(person, today)
    events.sort(key=lambda e: e["days_remaining"])
    return {"name": person["name"], "events": events}


def upcoming(store: Store, arguments, today: date) -> dict:
    within = arguments.get("within_days")
    within = _clean_lead_days(within)
    flat = []
    for person in store.load()["people"]:
        for event in person_events(person, today):
            if event["days_remaining"] <= within:
                flat.append(
                    {
                        "name": person["name"],
                        "address": person.get("address"),
                        **event,
                    }
                )
    flat.sort(key=lambda e: e["days_remaining"])
    return {
        "within_days": within,
        "count": len(flat),
        "items": flat,
        "nudges": [
            f"{e['name']}: {e['label']} {'is TODAY' if e['days_remaining'] == 0 else f'in {e['days_remaining']} day(s)'}"
            for e in flat
            if e["status"] in ("nudge", "today")
        ],
    }


def birthday_card_batch(store: Store, arguments, today: date) -> dict:
    within = arguments.get("within_days", 90)
    if not isinstance(within, int) or isinstance(within, bool) or not 1 <= within <= 365:
        raise DatesError("within_days must be 1-365")
    cards = []
    for person in store.load()["people"]:
        if person.get("birthday"):
            birthday = person["birthday"]
            days = days_remaining(birthday["month"], birthday["day"], today)
            if days <= within:
                cards.append(
                    {
                        "name": person["name"],
                        "address": person.get("address"),
                        "notes": person.get("notes"),
                        "birthday": next_occurrence(
                            birthday["month"], birthday["day"], today
                        ).isoformat(),
                        "days_remaining": days,
                    }
                )
    cards.sort(key=lambda c: c["days_remaining"])
    lines = ["Birthday card batch (mail in this order):"]
    for card in cards:
        line = f"- {card['name']} — {card['birthday']} ({card['days_remaining']}d)"
        if card["address"]:
            line += f" — ship to: {card['address']}"
        lines.append(line)
    if len(cards) == 1:
        lines.insert(1, "One card to send this season. Easy.")
    return {
        "within_days": within,
        "count": len(cards),
        "cards": cards,
        "batch_text": "\n".join(lines),
    }


def remove_person(store: Store, arguments) -> dict:
    doc = store.load()
    index = find_index(doc["people"], arguments.get("name"))
    if index < 0:
        raise DatesError(f"no person named {arguments.get('name')!r} on file")
    removed = doc["people"].pop(index)
    store.save(doc)
    return {"removed": removed.get("name"), "remaining": len(doc["people"])}


# ---------------------------------------------------------------------------
# MCP plumbing (JSON-RPC 2.0 over stdio, newline-delimited)
# ---------------------------------------------------------------------------


def error_response(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def write_line(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class LspFramingError(Exception):
    """Line received was framed with an HTTP-style Content-Length header."""


def decode_message(line: str) -> dict:
    """Accept newline-delimited JSON; reject LSP-style Content-Length framing."""
    if line[:15].lower() == "content-length:":
        raise LspFramingError(
            "LSP-style Content-Length framing detected; use newline-delimited JSON"
        )
    return json.loads(line)


class McpServer:
    def __init__(self, store: Store):
        self.store = store

    def handle(self, message: dict):
        method = message.get("method")
        msg_id = message.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method == "notifications/initialized":
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_definitions()}
            elif method == "tools/call":
                params = message.get("params", {})
                name = params.get("name")
                try:
                    value = call_tool(
                        self.store, name, params.get("arguments") or {}
                    )
                except (DatesError, ValueError) as exc:
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": f"Error: {exc}"}
                            ],
                            "isError": True,
                        },
                    }
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(value, ensure_ascii=False, indent=2),
                        }
                    ]
                }
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                return error_response(msg_id, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except (DatesError, ValueError) as exc:
            return error_response(msg_id, -32000, str(exc))
        except Exception as exc:  # keep the server alive on unexpected errors
            return error_response(msg_id, -32603, f"internal error: {exc}")


def serve(storage_dir: Path) -> None:
    store = Store(storage_dir)
    server = McpServer(store)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = decode_message(line)
        except json.JSONDecodeError as exc:
            write_line(error_response(None, -32700, f"invalid JSON: {exc}"))
            continue
        except LspFramingError as exc:
            write_line(error_response(None, -32700, str(exc)))
            continue
        response = server.handle(message)
        if response is not None:
            write_line(response)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    storage_dir = Path(argv[0] if argv else "./dates_data")
    serve(storage_dir)


if __name__ == "__main__":
    main()
