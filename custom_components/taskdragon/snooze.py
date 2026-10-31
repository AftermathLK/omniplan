""""Snooze until context" rule evaluation (pure logic).

The coordinator feeds state-change events and scheduled checks into these
functions; they decide whether a snoozed task should wake up. All rules use
*transition* semantics ("next time I am home") so a rule armed while its
condition already holds waits for the next occurrence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .const import (
    SNOOZE_ALL_HOME,
    SNOOZE_ENTITY_STATE,
    SNOOZE_PERSON_HOME,
    SNOOZE_RULE_TYPES,
    SNOOZE_TIME,
    SNOOZE_WEEKEND_DRY,
    WET_WEATHER_CONDITIONS,
)

HOME = "home"
UNAVAILABLE_STATES = ("unavailable", "unknown", "", None)


@dataclass(frozen=True)
class StateView:
    """The bits of a Home Assistant state the rules need."""

    state: str | None
    last_changed: datetime | None = None


def validate_rule(rule_type: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a rule configuration; raises ``ValueError``."""
    if rule_type not in SNOOZE_RULE_TYPES:
        raise ValueError(f"unknown snooze rule type: {rule_type}")
    clean: dict[str, Any] = {}
    if rule_type == SNOOZE_TIME:
        if not config.get("until"):
            raise ValueError("time snooze requires 'until'")
        clean["until"] = str(config["until"])
    elif rule_type == SNOOZE_PERSON_HOME:
        if config.get("person"):
            clean["person"] = str(config["person"])
    elif rule_type == SNOOZE_ALL_HOME:
        people = [str(p) for p in config.get("people") or [] if p]
        if people:
            clean["people"] = people
    elif rule_type == SNOOZE_WEEKEND_DRY:
        if config.get("weather_entity"):
            clean["weather_entity"] = str(config["weather_entity"])
    elif rule_type == SNOOZE_ENTITY_STATE:
        entity_id = str(config.get("entity_id") or "").strip()
        if "." not in entity_id:
            raise ValueError("entity snooze requires a valid 'entity_id'")
        clean["entity_id"] = entity_id
        has_condition = False
        if config.get("state") not in (None, ""):
            clean["state"] = str(config["state"]).strip()
            has_condition = True
        for bound in ("above", "below"):
            if config.get(bound) not in (None, ""):
                try:
                    clean[bound] = float(config[bound])
                except (TypeError, ValueError) as err:
                    raise ValueError(f"'{bound}' must be numeric") from err
                has_condition = True
        if not has_condition:
            raise ValueError("entity snooze requires 'state', 'above' or 'below'")
    return clean


def person_arrived(old_state: str | None, new_state: str | None) -> bool:
    """True when a person entity transitions to home."""
    return new_state == HOME and old_state != HOME


def everyone_home(states: Mapping[str, str | None], people: Iterable[str]) -> bool:
    """True when every listed person is home (an empty list is never 'everyone')."""
    people = list(people)
    return bool(people) and all(states.get(person) == HOME for person in people)


def entity_condition_met(state: str | None, config: Mapping[str, Any]) -> bool:
    """Evaluate an entity-state rule against a raw state string."""
    if state in UNAVAILABLE_STATES:
        return False
    if "state" in config and config["state"] != "":
        if str(state).lower() != str(config["state"]).lower():
            return False
    if "above" in config or "below" in config:
        try:
            value = float(state)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if "above" in config and not value > float(config["above"]):
            return False
        if "below" in config and not value < float(config["below"]):
            return False
    return True


def entity_condition_reached(old_state: str | None, new_state: str | None, config: Mapping[str, Any]) -> bool:
    """Transition semantics for entity rules: met now, not met before."""
    return entity_condition_met(new_state, config) and not entity_condition_met(old_state, config)


def is_wet(condition: str | None) -> bool:
    """True for weather conditions that rule out outdoor work."""
    return (condition or "").lower() in WET_WEATHER_CONDITIONS


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def weekend_dry_should_wake(day: date, condition: str | None) -> bool:
    """Weekend rule: wake on a Saturday/Sunday whose forecast is not wet.

    An unknown forecast counts as dry rather than leaving the task asleep forever.
    """
    if not is_weekend(day):
        return False
    if condition in UNAVAILABLE_STATES:
        return True
    return not is_wet(condition)


def caught_up_on_startup(rule_type: str, armed_at: datetime, views: Mapping[str, StateView], config: Mapping[str, Any], people: Iterable[str]) -> bool:
    """Decide whether a state rule fired while Home Assistant was not listening.

    Called at startup: if the condition holds now and the relevant state changed
    after the rule was armed, the transition happened while we were down.
    """
    if rule_type == SNOOZE_PERSON_HOME:
        view = views.get(str(config.get("person")))
        return bool(view and view.state == HOME and view.last_changed and view.last_changed > armed_at)
    if rule_type == SNOOZE_ALL_HOME:
        people = list(people)
        if not everyone_home({p: (views[p].state if p in views else None) for p in people}, people):
            return False
        changes = [views[p].last_changed for p in people if p in views and views[p].last_changed]
        return bool(changes) and max(changes) > armed_at
    if rule_type == SNOOZE_ENTITY_STATE:
        view = views.get(str(config.get("entity_id")))
        return bool(
            view
            and entity_condition_met(view.state, config)
            and view.last_changed
            and view.last_changed > armed_at
        )
    return False


def describe_rule(rule_type: str, config: Mapping[str, Any], names: Mapping[str, str] | None = None) -> str:
    """Short label shown on snoozed task badges."""
    names = names or {}
    if rule_type == SNOOZE_TIME:
        return f"Until {config.get('until')}"
    if rule_type == SNOOZE_PERSON_HOME:
        person = config.get("person")
        return f"Until {names.get(person, 'you')} get home" if person else "Until you get home"
    if rule_type == SNOOZE_ALL_HOME:
        return "Until everyone is home"
    if rule_type == SNOOZE_WEEKEND_DRY:
        return "This weekend, if it's not raining"
    if rule_type == SNOOZE_ENTITY_STATE:
        target = config.get("state")
        if target is None:
            bounds = []
            if "above" in config:
                bounds.append(f"> {config['above']}")
            if "below" in config:
                bounds.append(f"< {config['below']}")
            target = " and ".join(bounds)
        return f"Until {config.get('entity_id')} is {target}"
    return "Snoozed"
