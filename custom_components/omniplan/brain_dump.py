"""Brain-dump parser: free text / voice -> structured planner actions.

``async_parse`` first tries the configured Assist conversation agent (normally
a local LLM) with :data:`PROMPT_TEMPLATE`; if no agent is configured, the call
fails, or the reply is not valid JSON, the deterministic :func:`parse_with_rules`
parser is used instead.

Actions have one of two shapes::

    {"type": "task", "summary": ..., "plan_id": ..., "due_date": "YYYY-MM-DD"|None,
     "due_time": "HH:MM"|None, "time_of_day_bucket": ..., "context_tags": [...],
     "assigned_to": [...], "priority": ..., "estimated_minutes": int|None}
    {"type": "shopping", "name": ..., "quantity": str|None, "category": ..., "plan_id": ...}

The module does not import Home Assistant; ``hass`` is only used for its
service registry, so the parsing logic is unit-testable on its own.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .const import (
    AISLE_HARDWARE,
    AISLES,
    ARCHETYPE_DAILY,
    ARCHETYPE_MAINTENANCE,
    ARCHETYPE_PROJECTS,
    ARCHETYPE_SHOPPING,
    BUCKETS,
    CONTEXT_TAGS,
    PRIORITIES,
    ROLE_CHORES,
    ROLE_GROCERIES,
    ROLE_HARDWARE,
)
from .units import categorize_parsed, clean_name, parse_item

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class BrainDumpContext:
    """What the parser needs to know about the household."""

    now: datetime  # local, aware
    plans: Sequence[Mapping[str, Any]]
    people: Sequence[Mapping[str, Any]] = field(default_factory=list)  # {entity_id, name}
    speaker: str | None = None  # person entity of whoever is talking

    @property
    def today(self) -> date:
        return self.now.date()

    def plan_for_role(self, role: str) -> Mapping[str, Any] | None:
        for plan in self.plans:
            if (plan.get("settings") or {}).get("role") == role and not plan.get("is_template"):
                return plan
        return None

    def first_plan(self, archetype: str) -> Mapping[str, Any] | None:
        for plan in self.plans:
            if plan.get("archetype") == archetype and not plan.get("is_template"):
                return plan
        return None

    def plan_by_name(self, text: str) -> Mapping[str, Any] | None:
        needle = text.strip().lower()
        if not needle:
            return None
        for plan in self.plans:
            if str(plan.get("name", "")).lower() == needle or plan.get("id") == text:
                return plan
        for plan in self.plans:
            name = str(plan.get("name", "")).lower()
            if needle in name or name in needle:
                return plan
        return None

    def grocery_plan(self) -> Mapping[str, Any] | None:
        return self.plan_for_role(ROLE_GROCERIES) or self.first_plan(ARCHETYPE_SHOPPING)

    def hardware_plan(self) -> Mapping[str, Any] | None:
        return self.plan_for_role(ROLE_HARDWARE)

    def chores_plan(self) -> Mapping[str, Any] | None:
        return (
            self.plan_for_role(ROLE_CHORES)
            or self.first_plan(ARCHETYPE_DAILY)
            or self.first_plan(ARCHETYPE_PROJECTS)
            or self.first_plan(ARCHETYPE_MAINTENANCE)
        )


# ---------------------------------------------------------------------------
# Deterministic parser
# ---------------------------------------------------------------------------
_VERBS = (
    "remind", "add", "put", "buy", "get", "grab", "pick", "schedule", "call", "text", "email",
    "clean", "fix", "make", "book", "order", "pay", "take", "wash", "mow", "water", "feed",
    "change", "replace", "set up", "we need", "need to", "i need", "don't forget", "dont forget",
    "return", "drop off", "move", "start", "finish", "vacuum", "sweep", "mop", "organize",
    "install", "paint", "repair", "bring", "check", "cancel", "renew", "send", "write", "prep",
    "cook", "bake", "plan", "ask", "tell", "remember", "we're out", "we are out", "i have to",
    "empty", "fold", "unload", "load", "trim", "rake", "shovel", "sign up", "register",
)
_VERB_ALT = "|".join(re.escape(v) for v in sorted(_VERBS, key=len, reverse=True))
_SPLIT_RE = re.compile(
    rf"\s*(?:[;\n]+|(?<=[a-z0-9])\.\s+|,?\s+and\s+(?:then\s+|also\s+)?(?=(?:{_VERB_ALT})\b)|,?\s+(?:and\s+)?then\s+|,?\s+also\s+)",
    re.IGNORECASE,
)

_COMPOUND_ITEMS = (
    "salt and pepper", "mac and cheese", "macaroni and cheese", "half and half", "peanut butter and jelly",
    "nuts and bolts", "sour cream and onion", "bread and butter", "oil and vinegar", "pork and beans",
    "cookies and cream", "franks and beans",
)

_LIST_RE = re.compile(
    r"^(?:please\s+)?(?:add|put)\s+(?P<items>.+?)\s+(?:to|on|onto|in)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?P<list>[\w'&/\- ]+?)\s+list\s*$",
    re.IGNORECASE,
)
_TODO_LIST_RE = re.compile(
    r"^(?:please\s+)?(?:add|put)\s+(?P<task>.+?)\s+(?:to|on|onto)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?:to-?do|todo|task|chore)s?(?:\s+list)?\s*$",
    re.IGNORECASE,
)
_NEED_RE = re.compile(
    r"^(?:we\s+|i\s+)?(?:need(?!\s+to\b)(?:\s+more)?|are out of|'re out of|ran out of|re out of|are low on|'re low on|are running low on)\s+(?P<items>.+)$",
    re.IGNORECASE,
)
_WERE_OUT_RE = re.compile(r"^we(?:'re| are)\s+(?:out of|low on|running low on)\s+(?P<items>.+)$", re.IGNORECASE)
_BUY_RE = re.compile(r"^(?:please\s+)?(?:buy|purchase)\s+(?P<items>.+?)(?:\s+(?:at|from)\s+the\s+store)?$", re.IGNORECASE)
_GET_STORE_RE = re.compile(
    r"^(?:please\s+)?(?:get|grab|pick up)\s+(?P<items>.+?)\s+(?:at|from)\s+the\s+(?:grocery\s+)?store$",
    re.IGNORECASE,
)
_BARE_ADD_RE = re.compile(r"^(?:please\s+)?add\s+(?P<items>.+)$", re.IGNORECASE)

_LEAD_INS = re.compile(
    r"^(?:please\s+|hey\s+|ok(?:ay)?\s+|so\s+)*(?:"
    r"(?P<self>remind me to|remind me|don't forget to|dont forget to|do not forget to|i need to|i have to|"
    r"i've got to|i gotta|i should|i must|remember to|i want to|make sure i)"
    r"|(?:we need to|we have to|we should|need to|todo:?|to-do:?|to do:?|task:?)"
    r")\s+",
    re.IGNORECASE,
)
_ASSIGN_LEAD_RE = re.compile(r"^(?:remind|ask|tell|have|get)\s+(?P<name>[A-Z][\w'-]*)\s+(?:to\s+)?(?P<rest>.+)$")
_FOR_NAME_RE = re.compile(r"\s+for\s+(?P<name>[A-Z][\w'-]*)\s*$")

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_ALT = r"mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
_PART_ALT = r"morning|afternoon|evening|night|tonight"
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "couple": 2, "a couple": 2, "few": 3, "a few": 3,
}

_CONTEXT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Errand": (
        "pick up", "drop off", "return", "errand", "store", "menards", "home depot", "lowe's", "lowes",
        "costco", "target", "walmart", "aldi", "post office", "bank", "pharmacy", "dry cleaner",
        "hardware store", "grocery", "mall", "dmv", "library",
    ),
    "Garage/Yard": (
        "garage", "yard", "lawn", "mow", "garden", "gutter", "driveway", "deck", "shed", "rake",
        "leaves", "snow", "weed", "hedge", "patio", "fence", "sprinkler", "grill", "smoker", "car",
        "tire", "workbench", "compost", "trash cans", "bins",
    ),
    "Computer": (
        "email", "online", "order", "website", "pay the", "pay bill", "bill", "computer", "laptop",
        "print", "renew", "spreadsheet", "zoom", "amazon", "password", "backup", "update the",
        "register", "sign up", "book a", "reservation", "invoice", "taxes",
    ),
    "Indoor": (
        "clean", "vacuum", "dust", "laundry", "dishes", "dishwasher", "kitchen", "bathroom", "bedroom",
        "closet", "organize", "fold", "mop", "sweep", "toilet", "shower", "fridge", "pantry",
        "sheets", "bed", "living room", "basement", "attic", "windows", "wipe",
    ),
}
_DIY_WORDS = (
    "2x4", "2x6", "lumber", "plywood", "menards", "home depot", "lowe's", "lowes", "drywall", "build",
    "install", "shelf", "shelves", "deck", "framing", "paint the", "stain", "tile", "workbench",
    "project", "screws", "sand the", "caulk",
)
_MAINTENANCE_WORDS = (
    "furnace", "filter", "hvac", "smoke detector", "co detector", "water heater", "oil change",
    "gutters", "dryer vent", "septic", "water softener", "salt the softener", "chimney", "ac unit",
    "air conditioner", "sump pump", "batteries in",
)


def _protect_compounds(text: str) -> str:
    for phrase in _COMPOUND_ITEMS:
        text = re.sub(re.escape(phrase), phrase.replace(" and ", " ∧ "), text, flags=re.IGNORECASE)
    return text


def _restore_compounds(text: str) -> str:
    return text.replace(" ∧ ", " and ")


def split_clauses(text: str) -> list[str]:
    """Split a multi-intent utterance into clauses."""
    protected = _protect_compounds(text.strip())
    parts = [_restore_compounds(p).strip(" ,.") for p in _SPLIT_RE.split(protected)]
    return [p for p in parts if p]


def split_items(text: str) -> list[str]:
    """Split ``"milk, eggs and bread"`` into separate items."""
    protected = _protect_compounds(text)
    pieces = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*|\s+plus\s+", protected)
    items = []
    for piece in pieces:
        piece = _restore_compounds(piece).strip(" .")
        piece = re.sub(r"^(?:some|more|a|an|the)\s+", "", piece, flags=re.IGNORECASE)
        if piece:
            items.append(piece)
    return items


def _next_weekday(today: date, weekday: int, *, allow_today: bool) -> date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return today + timedelta(days=delta)


def _weekday_index(token: str) -> int:
    token = token.lower()[:3]
    return [w[:3] for w in WEEKDAYS].index(token)


def _bucket_from_part(part: str | None) -> str | None:
    if not part:
        return None
    part = part.lower()
    if part in ("night", "tonight", "evening"):
        return "evening"
    return part if part in BUCKETS else None


@dataclass
class _DateInfo:
    due_date: date | None = None
    due_time: str | None = None
    bucket: str | None = None


def extract_when(text: str, today: date) -> tuple[str, _DateInfo]:
    """Pull date/time phrases out of ``text``; returns (remaining text, info)."""
    info = _DateInfo()

    def cut(pattern: str, handler) -> None:
        nonlocal text
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            handler(match)
            text = (text[: match.start()] + " " + text[match.end():]).strip()

    def on_today(m: re.Match) -> None:
        info.due_date = today
        info.bucket = _bucket_from_part(m.group("part")) or ("evening" if m.group(0).lower().strip().endswith("tonight") else info.bucket)

    def on_tomorrow(m: re.Match) -> None:
        info.due_date = today + timedelta(days=1)
        info.bucket = _bucket_from_part(m.group("part")) or info.bucket

    def on_weekday(m: re.Match) -> None:
        index = _weekday_index(m.group("day"))
        allow_today = (m.group("rel") or "").lower().strip() == "this"
        day = _next_weekday(today, index, allow_today=allow_today)
        # "next Friday" said on a Tuesday means Friday of next week, not this one.
        if (m.group("rel") or "").lower().strip() == "next" and day.isocalendar()[:2] == today.isocalendar()[:2]:
            day += timedelta(days=7)
        info.due_date = day
        info.bucket = _bucket_from_part(m.group("part")) or info.bucket

    def on_weekend(m: re.Match) -> None:
        info.due_date = today if today.weekday() >= 5 else _next_weekday(today, 5, allow_today=True)
        if (m.group("rel") or "").lower() == "next" and today.weekday() >= 5:
            info.due_date = _next_weekday(today, 5, allow_today=False)

    def on_next_week(_m: re.Match) -> None:
        info.due_date = _next_weekday(today, 0, allow_today=False)

    def on_in(m: re.Match) -> None:
        raw = m.group("n").lower()
        count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 1)
        days = count * (7 if m.group("unit").lower().startswith("week") else 1)
        info.due_date = today + timedelta(days=days)

    def on_numeric_date(m: re.Match) -> None:
        month, day = int(m.group("m")), int(m.group("d"))
        year = int(m.group("y")) if m.group("y") else today.year
        if year < 100:
            year += 2000
        try:
            candidate = date(year, month, day)
        except ValueError:
            return
        if not m.group("y") and candidate < today:
            candidate = candidate.replace(year=year + 1)
        info.due_date = candidate

    def on_month_date(m: re.Match) -> None:
        month = MONTHS.index(m.group("mon").lower()[:3]) + 1
        try:
            candidate = date(today.year, month, int(m.group("d")))
        except ValueError:
            return
        if candidate < today:
            candidate = candidate.replace(year=today.year + 1)
        info.due_date = candidate

    def on_time(m: re.Match) -> None:
        hour = int(m.group("h"))
        minute = int(m.group("min") or 0)
        meridiem = (m.group("ampm") or "").lower().replace(".", "")
        if meridiem.startswith("p") and hour < 12:
            hour += 12
        elif meridiem.startswith("a") and hour == 12:
            hour = 0
        elif not meridiem and 1 <= hour <= 7:
            hour += 12  # "at 5" almost always means 5 PM
        if hour > 23 or minute > 59:
            return
        info.due_time = f"{hour:02d}:{minute:02d}"
        info.bucket = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"

    def on_noon(m: re.Match) -> None:
        word = m.group(0).lower()
        info.due_time = "12:00" if "noon" in word else "00:00"
        info.bucket = "afternoon" if "noon" in word else info.bucket

    def on_part(m: re.Match) -> None:
        info.bucket = _bucket_from_part(m.group("part"))
        if m.group("part").lower() == "tonight" and info.due_date is None:
            info.due_date = today

    cut(rf"\b(?:by\s+|on\s+)?(?:today|this\s+(?P<part>{_PART_ALT}))\b|\btonight\b(?P<x>)", on_today)
    cut(rf"\b(?:by\s+)?tomorrow(?:\s+(?P<part>{_PART_ALT}))?\b", on_tomorrow)
    cut(r"\b(?:on\s+|by\s+)?(?P<rel>this|next)?\s*weekend\b", on_weekend)
    cut(r"\bnext\s+week\b", on_next_week)
    cut(
        rf"\b(?:on\s+|by\s+)?(?:(?P<rel>this|next|coming)\s+)?(?P<day>{_WEEKDAY_ALT})\b(?:\s+(?P<part>{_PART_ALT}))?",
        on_weekday,
    )
    cut(r"\bin\s+(?P<n>\d+|a|an|one|two|three|four|five|six|seven|a couple|a few)\s+(?P<unit>days?|weeks?)\b", on_in)
    cut(r"\b(?:on\s+|by\s+)?(?P<m>1[0-2]|0?[1-9])/(?P<d>3[01]|[12]\d|0?[1-9])(?:/(?P<y>\d{2,4}))?\b", on_numeric_date)
    cut(
        r"\b(?:on\s+|by\s+)?(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?P<d>3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?\b",
        on_month_date,
    )
    cut(r"\b(?:at|by|around)\s+(?P<h>1[0-2]|0?\d|1\d|2[0-3])(?::(?P<min>[0-5]\d))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?(?=\s|$|[,.!?])", on_time)
    cut(r"\b(?:at\s+)?(?:noon|midnight)\b", on_noon)
    cut(rf"\b(?:in\s+the\s+|this\s+)?(?P<part>{_PART_ALT})\b", on_part)
    return re.sub(r"\s{2,}", " ", text).strip(" ,."), info


def _detect_tags(text: str) -> list[str]:
    lowered = f" {text.lower()} "
    tags = []
    for tag in CONTEXT_TAGS:
        for keyword in _CONTEXT_KEYWORDS.get(tag, ()):
            if re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", lowered):
                tags.append(tag)
                break
    if "Garage/Yard" in tags and "Indoor" in tags:
        tags.remove("Indoor")
    return tags


def _detect_priority(text: str) -> tuple[str, str]:
    priority = "none"
    patterns = (
        (r"\b(?:urgent(?:ly)?|asap|right away|immediately)\b|!{2,}", "urgent"),
        (r"\b(?:high priority|important)\b", "high"),
        (r"\blow priority\b|\bwhenever\b|\bsomeday\b", "low"),
    )
    for pattern, value in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            priority = value
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
            break
    return re.sub(r"\s{2,}", " ", text).strip(" ,.!"), priority


def _detect_estimate(text: str) -> tuple[str, int | None]:
    match = re.search(
        r"\(?\b(?:takes?\s+|about\s+|~)?(?P<n>\d+)\s*(?P<unit>m|min|mins|minutes?|h|hr|hrs|hours?)\b\)?",
        text,
        re.IGNORECASE,
    )
    if match and not re.search(r"\bin\s+$", text[: match.start()], re.IGNORECASE):
        value = int(match.group("n"))
        minutes = value * 60 if match.group("unit").lower().startswith("h") else value
        text = (text[: match.start()] + " " + text[match.end():]).strip()
        return re.sub(r"\s{2,}", " ", text).strip(" ,."), minutes
    if re.search(r"\b(?:quick|quickly|real quick)\b", text, re.IGNORECASE):
        return re.sub(r"\b(?:real\s+)?quick(?:ly)?\b", " ", text, flags=re.IGNORECASE).strip(" ,."), 5
    return text, None


def _match_person(name: str, ctx: BrainDumpContext) -> str | None:
    lowered = name.lower()
    for person in ctx.people:
        full = str(person.get("name") or "").lower()
        if full == lowered or full.split(" ")[0] == lowered or person.get("entity_id") == f"person.{lowered}":
            return str(person["entity_id"])
    return None


def _route_task(summary: str, ctx: BrainDumpContext) -> Mapping[str, Any] | None:
    lowered = summary.lower()
    if any(word in lowered for word in _DIY_WORDS):
        plan = ctx.first_plan(ARCHETYPE_PROJECTS) or ctx.hardware_plan()
        if plan:
            return plan
    if any(word in lowered for word in _MAINTENANCE_WORDS):
        plan = ctx.first_plan(ARCHETYPE_MAINTENANCE)
        if plan:
            return plan
    return ctx.chores_plan()


def _shopping_plan_for(list_hint: str | None, category: str, ctx: BrainDumpContext) -> Mapping[str, Any] | None:
    if list_hint:
        hint = list_hint.lower().strip()
        if re.search(r"hardware|menards|home depot|lowe", hint):
            plan = ctx.hardware_plan() or ctx.plan_by_name(hint)
            if plan:
                return plan
        if re.search(r"grocer|shopping|food|store", hint):
            plan = ctx.grocery_plan()
            if plan:
                return plan
        plan = ctx.plan_by_name(hint)
        if plan and plan.get("archetype") == ARCHETYPE_SHOPPING:
            return plan
    if category == AISLE_HARDWARE and ctx.hardware_plan():
        return ctx.hardware_plan()
    return ctx.grocery_plan()


def _shopping_actions(items_text: str, list_hint: str | None, ctx: BrainDumpContext) -> list[dict[str, Any]]:
    actions = []
    for raw in split_items(items_text):
        parsed = parse_item(raw)
        if not parsed.name:
            continue
        category = categorize_parsed(parsed)
        plan = _shopping_plan_for(list_hint, category, ctx)
        actions.append(
            {
                "type": "shopping",
                "name": parsed.name,
                "quantity": parsed.raw_quantity if parsed.quantity is not None else None,
                "category": category,
                "plan_id": plan.get("id") if plan else None,
            }
        )
    return actions


def _task_action(clause: str, ctx: BrainDumpContext) -> dict[str, Any] | None:
    assigned: list[str] = []
    text = clause.strip()
    assign_match = _ASSIGN_LEAD_RE.match(text)
    if assign_match and assign_match.group("name").lower() not in ("me", "us", "the", "a", "my"):
        person = _match_person(assign_match.group("name"), ctx)
        if person:
            assigned.append(person)
            text = assign_match.group("rest")
    lead = _LEAD_INS.match(text)
    if lead:
        if lead.group("self") and ctx.speaker and not assigned:
            assigned.append(ctx.speaker)
        text = text[lead.end():]
    for_match = _FOR_NAME_RE.search(text)
    if for_match:
        person = _match_person(for_match.group("name"), ctx)
        if person:
            assigned = [person]
            text = text[: for_match.start()]

    text, priority = _detect_priority(text)
    text, estimate = _detect_estimate(text)
    text, when = extract_when(text, ctx.today)
    summary = clean_name(re.sub(r"\s+(?:on|by|at)$", "", text.strip(" ,.")))
    if not summary:
        return None
    plan = _route_task(summary, ctx)
    return {
        "type": "task",
        "summary": summary,
        "plan_id": plan.get("id") if plan else None,
        "due_date": when.due_date.isoformat() if when.due_date else None,
        "due_time": when.due_time,
        "time_of_day_bucket": when.bucket or "anytime",
        "context_tags": _detect_tags(clause),
        "assigned_to": assigned,
        "priority": priority,
        "estimated_minutes": estimate,
    }


def parse_with_rules(text: str, ctx: BrainDumpContext) -> list[dict[str, Any]]:
    """Deterministic multi-intent parser used when no LLM agent is available."""
    actions: list[dict[str, Any]] = []
    for clause in split_clauses(text):
        clause = clause.strip()
        todo = _TODO_LIST_RE.match(clause)
        if todo:
            action = _task_action(todo.group("task"), ctx)
            if action:
                actions.append(action)
            continue
        list_match = _LIST_RE.match(clause)
        if list_match:
            hint = list_match.group("list")
            target = ctx.plan_by_name(hint)
            if target is not None and target.get("archetype") not in (ARCHETYPE_SHOPPING, None):
                # "add mow the lawn to the chores list" -> a task in that plan
                action = _task_action(list_match.group("items"), ctx)
                if action:
                    action["plan_id"] = target.get("id")
                    actions.append(action)
                continue
            actions.extend(_shopping_actions(list_match.group("items"), hint, ctx))
            continue
        for pattern in (_WERE_OUT_RE, _NEED_RE, _GET_STORE_RE, _BUY_RE):
            match = pattern.match(clause)
            if match:
                actions.extend(_shopping_actions(match.group("items"), None, ctx))
                break
        else:
            bare = _BARE_ADD_RE.match(clause)
            if bare and not _LEAD_INS.match(clause):
                items = bare.group("items")
                parsed = parse_item(items)
                stripped, when = extract_when(items, ctx.today)
                if when.due_date is None and when.due_time is None and len(items.split()) <= 6:
                    actions.extend(_shopping_actions(items, None, ctx))
                    continue
                clause = stripped if parsed.name else clause
            action = _task_action(clause, ctx)
            if action:
                actions.append(action)
    return actions


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = """You are the parser for a household planner. Convert the user's brain dump into JSON.
Respond with ONLY a JSON object, no prose, no code fences, of this exact shape:
{{"actions": [ ... ]}}

Each action is one of:
1. A task:
{{"type": "task", "summary": "<short imperative title without date words>", "plan_id": "<one of the plan ids below>",
 "due_date": "YYYY-MM-DD" or null, "due_time": "HH:MM" (24h) or null,
 "time_of_day_bucket": "anytime"|"morning"|"afternoon"|"evening",
 "context_tags": subset of ["Indoor", "Garage/Yard", "Computer", "Errand"],
 "assigned_to": list of person entity ids from the people below (use the speaker for "remind me"/"I need to"),
 "priority": "none"|"low"|"medium"|"high"|"urgent", "estimated_minutes": integer or null}}
2. A shopping item:
{{"type": "shopping", "name": "<item name>", "quantity": "<amount and unit>" or null,
 "category": one of {aisles}, "plan_id": "<a shopping plan id below>"}}

Rules:
- Split multi-intent sentences into separate actions; split "milk, eggs and bread" into separate shopping items.
- Hardware / lumber / DIY errands go to the projects plan if one exists; hardware shopping items go to the hardware list.
- Resolve relative dates against today: {today} ({weekday}). "Saturday" means the next upcoming Saturday.
- Never invent plan ids or person ids.

Plans:
{plans}

People (speaker: {speaker}):
{people}

Brain dump:
{text}
"""


def build_prompt(text: str, ctx: BrainDumpContext) -> str:
    plans = "\n".join(
        f"- id={plan.get('id')} name={plan.get('name')!r} archetype={plan.get('archetype')}"
        + (f" role={(plan.get('settings') or {}).get('role')}" if (plan.get("settings") or {}).get("role") else "")
        for plan in ctx.plans
        if not plan.get("is_template")
    )
    people = "\n".join(f"- {person.get('entity_id')} ({person.get('name')})" for person in ctx.people) or "- none"
    return PROMPT_TEMPLATE.format(
        aisles=json.dumps(list(AISLES)),
        today=ctx.today.isoformat(),
        weekday=ctx.today.strftime("%A"),
        plans=plans or "- none",
        speaker=ctx.speaker or "unknown",
        people=people,
        text=text.strip(),
    )


def extract_json(reply: str) -> Any:
    """Find and decode the first JSON object/array in an LLM reply."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", reply, re.DOTALL)
    if fenced:
        reply = fenced.group(1)
    start_candidates = [i for i in (reply.find("{"), reply.find("[")) if i >= 0]
    if not start_candidates:
        raise ValueError("no JSON found in reply")
    start = min(start_candidates)
    end = max(reply.rfind("}"), reply.rfind("]"))
    if end <= start:
        raise ValueError("no JSON found in reply")
    return json.loads(reply[start : end + 1])


def _valid_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _valid_time(value: Any) -> str | None:
    if not value:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})", str(value))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def normalize_llm_actions(payload: Any, ctx: BrainDumpContext) -> list[dict[str, Any]]:
    """Validate LLM output against the schema, repairing what can be repaired."""
    raw_actions = payload.get("actions") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_actions, list):
        raise ValueError("'actions' must be a list")
    plan_ids = {str(plan.get("id")): plan for plan in ctx.plans}
    people = {str(person.get("entity_id")) for person in ctx.people}
    actions: list[dict[str, Any]] = []
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("type") or "").lower()
        if kind in ("shopping", "shopping_item", "add_shopping_item", "grocery"):
            name = clean_name(str(raw.get("name") or raw.get("summary") or ""))
            if not name:
                continue
            parsed = parse_item(name)
            category = raw.get("category") if raw.get("category") in AISLES else categorize_parsed(parsed)
            plan = plan_ids.get(str(raw.get("plan_id")))
            if plan is None or plan.get("archetype") != ARCHETYPE_SHOPPING:
                plan = _shopping_plan_for(None, str(category), ctx)
            quantity = raw.get("quantity")
            actions.append(
                {
                    "type": "shopping",
                    "name": parsed.name or name,
                    "quantity": str(quantity).strip() if quantity not in (None, "", 0) else parsed.raw_quantity,
                    "category": category,
                    "plan_id": plan.get("id") if plan else None,
                }
            )
        elif kind in ("task", "create_task", "todo", "reminder"):
            summary = clean_name(str(raw.get("summary") or raw.get("title") or ""))
            if not summary:
                continue
            plan = plan_ids.get(str(raw.get("plan_id")))
            if plan is None or plan.get("archetype") == ARCHETYPE_SHOPPING:
                plan = _route_task(summary, ctx)
            estimate = raw.get("estimated_minutes")
            try:
                estimate = int(estimate) if estimate not in (None, "") else None
            except (TypeError, ValueError):
                estimate = None
            actions.append(
                {
                    "type": "task",
                    "summary": summary,
                    "plan_id": plan.get("id") if plan else None,
                    "due_date": _valid_date(raw.get("due_date")),
                    "due_time": _valid_time(raw.get("due_time")),
                    "time_of_day_bucket": raw.get("time_of_day_bucket") if raw.get("time_of_day_bucket") in BUCKETS else "anytime",
                    "context_tags": [t for t in raw.get("context_tags") or [] if t in CONTEXT_TAGS],
                    "assigned_to": [p for p in raw.get("assigned_to") or [] if p in people],
                    "priority": raw.get("priority") if raw.get("priority") in PRIORITIES else "none",
                    "estimated_minutes": estimate,
                }
            )
    return actions


def _speech_from_response(response: Any) -> str:
    if not isinstance(response, Mapping):
        return ""
    inner = response.get("response", response)
    speech = (inner or {}).get("speech") or {}
    plain = speech.get("plain") or {}
    return str(plain.get("speech") or "")


async def async_parse(
    hass: HomeAssistant,
    text: str,
    ctx: BrainDumpContext,
    agent_id: str | None,
) -> tuple[list[dict[str, Any]], str]:
    """Parse a brain dump. Returns ``(actions, engine)`` where engine is ``llm`` or ``rules``."""
    text = (text or "").strip()
    if not text:
        return [], "rules"
    if agent_id:
        try:
            response = await hass.services.async_call(
                "conversation",
                "process",
                {"text": build_prompt(text, ctx), "agent_id": agent_id},
                blocking=True,
                return_response=True,
            )
            reply = _speech_from_response(response)
            actions = normalize_llm_actions(extract_json(reply), ctx)
            if actions:
                return actions, "llm"
            _LOGGER.debug("LLM brain dump returned no actions; falling back to rules: %s", reply)
        except Exception as err:  # noqa: BLE001 - any agent failure falls back to the rules parser
            _LOGGER.warning("Brain dump via %s failed (%s); using the rules parser", agent_id, err)
    return parse_with_rules(text, ctx), "rules"
