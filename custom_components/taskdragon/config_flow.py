"""Config and options flow for TaskDragon.

Steps (shared by setup and options):

1. ``user`` / ``init`` – household ``person.*`` entities, weather entity, Assist agent.
2. ``shift`` – once per person: work calendar + shift keywords for PRN balancing.
3. ``mealie`` – optional Mealie URL + API token (validated live).
4. ``schedule`` – rollover hour, check times, meal times, leftovers, prep rules.

The Mealie URL, token and SSL setting are stored in ``ConfigEntry.data``; every
other setting is an option. The token is never shown back in a form: leaving the
field empty in the options flow keeps the stored one.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BREAKFAST_TIME,
    CONF_CONVERSATION_AGENT,
    CONF_CREATE_COOK_TASKS,
    CONF_DINNER_TIME,
    CONF_ENABLE_LEFTOVERS,
    CONF_LEFTOVER_DAYS,
    CONF_LEFTOVER_TIME,
    CONF_LUNCH_TIME,
    CONF_MEALIE_LOOKAHEAD_DAYS,
    CONF_MEALIE_SYNC_MINUTES,
    CONF_MEALIE_TOKEN,
    CONF_MEALIE_URL,
    CONF_MEALIE_VERIFY_SSL,
    CONF_PEOPLE,
    CONF_PERSON_ENTITY,
    CONF_PREP_RULES,
    CONF_ROLLOVER_HOUR,
    CONF_SHIFT_CHECK_TIME,
    CONF_SHIFT_KEYWORDS,
    CONF_SHOW_PANEL,
    CONF_WEATHER_ENTITY,
    CONF_WEEKEND_CHECK_TIME,
    CONF_WORK_CALENDAR,
    DEFAULT_BREAKFAST_TIME,
    DEFAULT_DINNER_TIME,
    DEFAULT_LEFTOVER_DAYS,
    DEFAULT_LEFTOVER_TIME,
    DEFAULT_LUNCH_TIME,
    DEFAULT_MEALIE_LOOKAHEAD_DAYS,
    DEFAULT_MEALIE_SYNC_MINUTES,
    DEFAULT_PREP_RULES,
    DEFAULT_ROLLOVER_HOUR,
    DEFAULT_SHIFT_CHECK_TIME,
    DEFAULT_SHIFT_KEYWORDS,
    DEFAULT_WEEKEND_CHECK_TIME,
    DOMAIN,
    LEGACY_CONF_MEALIE_TOKEN,
    MEALIE_DATA_KEYS,
)
from .mealie_client import InvalidMealieUrl, MealieAuthError, MealieClient, MealieError, validate_base_url
from .migration import LEGACY_DOMAIN

CONF_HOUSEHOLD = "household"


def _optional(key: str, current: dict[str, Any]) -> vol.Optional:
    """An optional field pre-filled with the current value but clearable."""
    value = current.get(key)
    return vol.Optional(key, description={"suggested_value": value} if value not in (None, "") else None)


def household_schema(current: dict[str, Any]) -> vol.Schema:
    people = [p[CONF_PERSON_ENTITY] for p in current.get(CONF_PEOPLE) or [] if p.get(CONF_PERSON_ENTITY)]
    return vol.Schema(
        {
            vol.Optional(CONF_HOUSEHOLD, default=people): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="person", multiple=True)
            ),
            _optional(CONF_WEATHER_ENTITY, current): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            _optional(CONF_CONVERSATION_AGENT, current): selector.ConversationAgentSelector(
                selector.ConversationAgentSelectorConfig()
            ),
        }
    )


def shift_schema(current_person: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            _optional(CONF_WORK_CALENDAR, current_person): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar")
            ),
            vol.Optional(
                CONF_SHIFT_KEYWORDS, default=current_person.get(CONF_SHIFT_KEYWORDS, DEFAULT_SHIFT_KEYWORDS)
            ): selector.TextSelector(),
        }
    )


def mealie_schema(current: dict[str, Any]) -> vol.Schema:
    """``current`` holds the URL and settings; the token is deliberately never pre-filled."""
    return vol.Schema(
        {
            _optional(CONF_MEALIE_URL, current): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
            vol.Optional(CONF_MEALIE_TOKEN): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_MEALIE_VERIFY_SSL, default=current.get(CONF_MEALIE_VERIFY_SSL, True)): selector.BooleanSelector(),
            vol.Optional(
                CONF_MEALIE_SYNC_MINUTES, default=current.get(CONF_MEALIE_SYNC_MINUTES, DEFAULT_MEALIE_SYNC_MINUTES)
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=240, step=1, mode=selector.NumberSelectorMode.BOX)),
            vol.Optional(
                CONF_MEALIE_LOOKAHEAD_DAYS, default=current.get(CONF_MEALIE_LOOKAHEAD_DAYS, DEFAULT_MEALIE_LOOKAHEAD_DAYS)
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=14, step=1, mode=selector.NumberSelectorMode.BOX)),
        }
    )


def schedule_schema(current: dict[str, Any]) -> vol.Schema:
    def default(key: str, fallback: Any) -> Any:
        return current.get(key, fallback)

    return vol.Schema(
        {
            vol.Optional(CONF_ROLLOVER_HOUR, default=default(CONF_ROLLOVER_HOUR, DEFAULT_ROLLOVER_HOUR)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_SHIFT_CHECK_TIME, default=default(CONF_SHIFT_CHECK_TIME, DEFAULT_SHIFT_CHECK_TIME)): selector.TimeSelector(),
            vol.Optional(CONF_WEEKEND_CHECK_TIME, default=default(CONF_WEEKEND_CHECK_TIME, DEFAULT_WEEKEND_CHECK_TIME)): selector.TimeSelector(),
            vol.Optional(CONF_BREAKFAST_TIME, default=default(CONF_BREAKFAST_TIME, DEFAULT_BREAKFAST_TIME)): selector.TimeSelector(),
            vol.Optional(CONF_LUNCH_TIME, default=default(CONF_LUNCH_TIME, DEFAULT_LUNCH_TIME)): selector.TimeSelector(),
            vol.Optional(CONF_DINNER_TIME, default=default(CONF_DINNER_TIME, DEFAULT_DINNER_TIME)): selector.TimeSelector(),
            vol.Optional(CONF_CREATE_COOK_TASKS, default=default(CONF_CREATE_COOK_TASKS, True)): selector.BooleanSelector(),
            vol.Optional(CONF_ENABLE_LEFTOVERS, default=default(CONF_ENABLE_LEFTOVERS, True)): selector.BooleanSelector(),
            vol.Optional(CONF_LEFTOVER_TIME, default=default(CONF_LEFTOVER_TIME, DEFAULT_LEFTOVER_TIME)): selector.TimeSelector(),
            vol.Optional(CONF_LEFTOVER_DAYS, default=default(CONF_LEFTOVER_DAYS, DEFAULT_LEFTOVER_DAYS)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_PREP_RULES, default=default(CONF_PREP_RULES, DEFAULT_PREP_RULES)): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Optional(CONF_SHOW_PANEL, default=default(CONF_SHOW_PANEL, True)): selector.BooleanSelector(),
        }
    )


async def validate_mealie(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> dict[str, str]:
    """Check URL and token server-side (10 s timeout, no redirects) before anything is saved."""
    if not token:
        return {CONF_MEALIE_TOKEN: "token_required"}
    try:
        client = MealieClient(async_get_clientsession(hass, verify_ssl=verify_ssl), url, token)
    except InvalidMealieUrl:
        return {CONF_MEALIE_URL: "invalid_url"}
    try:
        await client.async_get_self()
    except MealieAuthError:
        return {"base": "invalid_auth"}
    except MealieError:
        return {"base": "cannot_connect"}
    return {}


def split_legacy_mealie(data: dict[str, Any], options: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move Mealie connection settings out of options (1.0.x) into entry data."""
    data, options = dict(data), dict(options)
    url = options.pop(CONF_MEALIE_URL, None) or data.get(CONF_MEALIE_URL)
    token = (
        options.pop(LEGACY_CONF_MEALIE_TOKEN, None)
        or options.pop(CONF_MEALIE_TOKEN, None)
        or data.pop(LEGACY_CONF_MEALIE_TOKEN, None)
        or data.get(CONF_MEALIE_TOKEN)
    )
    verify = options.pop(CONF_MEALIE_VERIFY_SSL, data.get(CONF_MEALIE_VERIFY_SSL, True))
    data = {k: v for k, v in data.items() if k not in MEALIE_DATA_KEYS}
    if url and token:
        data.update({CONF_MEALIE_URL: url, CONF_MEALIE_TOKEN: token, CONF_MEALIE_VERIFY_SSL: bool(verify)})
    return data, options


class _TaskDragonSteps:
    """Steps shared by the config flow and the options flow."""

    hass: HomeAssistant
    _options: dict[str, Any]
    _data: dict[str, Any]
    _queue: list[str]
    _previous_people: dict[str, dict[str, Any]]

    def _store_household(self, user_input: dict[str, Any]) -> None:
        self._previous_people = {
            p[CONF_PERSON_ENTITY]: p for p in self._options.get(CONF_PEOPLE) or [] if p.get(CONF_PERSON_ENTITY)
        }
        self._queue = list(user_input.get(CONF_HOUSEHOLD) or [])
        self._options[CONF_PEOPLE] = []
        for key in (CONF_WEATHER_ENTITY, CONF_CONVERSATION_AGENT):
            self._options[key] = user_input.get(key) or None

    async def async_step_shift(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None and self._queue:
            person = self._queue.pop(0)
            self._options[CONF_PEOPLE].append(
                {
                    CONF_PERSON_ENTITY: person,
                    CONF_WORK_CALENDAR: user_input.get(CONF_WORK_CALENDAR) or None,
                    CONF_SHIFT_KEYWORDS: (user_input.get(CONF_SHIFT_KEYWORDS) or "").strip(),
                }
            )
        if not self._queue:
            return await self.async_step_mealie()
        person = self._queue[0]
        state = self.hass.states.get(person)
        name = state.name if state else person
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="shift",
            data_schema=shift_schema(self._previous_people.get(person, {})),
            description_placeholders={"person": name},
        )

    async def async_step_mealie(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_url = (user_input.get(CONF_MEALIE_URL) or "").strip()
            token = (user_input.get(CONF_MEALIE_TOKEN) or "").strip()
            verify = bool(user_input.get(CONF_MEALIE_VERIFY_SSL, True))
            url = ""
            if raw_url:
                try:
                    url = validate_base_url(raw_url)
                except InvalidMealieUrl:
                    errors = {CONF_MEALIE_URL: "invalid_url"}
            if url and not token and url == self._data.get(CONF_MEALIE_URL):
                token = self._data.get(CONF_MEALIE_TOKEN) or ""  # empty field keeps the stored token
            if url and not errors:
                errors = await validate_mealie(self.hass, url, token, verify)
            if not errors:
                self._data = (
                    {CONF_MEALIE_URL: url, CONF_MEALIE_TOKEN: token, CONF_MEALIE_VERIFY_SSL: verify} if url else {}
                )
                self._options.update(
                    {
                        CONF_MEALIE_SYNC_MINUTES: int(user_input.get(CONF_MEALIE_SYNC_MINUTES, DEFAULT_MEALIE_SYNC_MINUTES)),
                        CONF_MEALIE_LOOKAHEAD_DAYS: int(user_input.get(CONF_MEALIE_LOOKAHEAD_DAYS, DEFAULT_MEALIE_LOOKAHEAD_DAYS)),
                    }
                )
                return await self.async_step_schedule()
        shown = {**self._options, **self._data}
        shown.update({k: v for k, v in (user_input or {}).items() if k != CONF_MEALIE_TOKEN})
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="mealie",
            data_schema=mealie_schema(shown),
            errors=errors,
        )

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in (CONF_ROLLOVER_HOUR, CONF_LEFTOVER_DAYS):
                if key in user_input:
                    user_input[key] = int(user_input[key])
            self._options.update(user_input)
            return self._finish()
        return self.async_show_form(step_id="schedule", data_schema=schedule_schema(self._options))  # type: ignore[attr-defined]

    def _finish(self) -> ConfigFlowResult:
        raise NotImplementedError


class TaskDragonConfigFlow(_TaskDragonSteps, ConfigFlow, domain=DOMAIN):
    """Initial setup."""

    VERSION = 1
    MINOR_VERSION = 2  # 1.2: Mealie connection moved from options to data

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._data: dict[str, Any] = {}
        self._queue: list[str] = []
        self._previous_people: dict[str, dict[str, Any]] = {}
        self._legacy_declined = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None and not self._legacy_declined and self.hass.config_entries.async_entries(LEGACY_DOMAIN):
            return await self.async_step_import_legacy()
        if user_input is not None:
            self._store_household(user_input)
            return await self.async_step_shift()
        return self.async_show_form(step_id="user", data_schema=household_schema(self._options))

    async def async_step_import_legacy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The integration's previous install is still configured: offer to bring its settings over."""
        if user_input is not None:
            if user_input.get("import_settings", True):
                old = self.hass.config_entries.async_entries(LEGACY_DOMAIN)[0]
                self._data, self._options = split_legacy_mealie(dict(old.data), dict(old.options))
                return self._finish()
            self._legacy_declined = True
            return await self.async_step_user()
        return self.async_show_form(step_id="import_legacy", data_schema=vol.Schema({vol.Required("import_settings", default=True): bool}))

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title="TaskDragon", data=self._data, options=self._options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TaskDragonOptionsFlow()


class TaskDragonOptionsFlow(_TaskDragonSteps, OptionsFlow):
    """Edit any setting after setup; the entry reloads when saved."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._data: dict[str, Any] = {}
        self._queue: list[str] = []
        self._previous_people: dict[str, dict[str, Any]] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._options:
            self._data, self._options = split_legacy_mealie(dict(self.config_entry.data), dict(self.config_entry.options))
        if user_input is not None:
            self._store_household(user_input)
            return await self.async_step_shift()
        return self.async_show_form(step_id="init", data_schema=household_schema(self._options))

    def _finish(self) -> ConfigFlowResult:
        if self._data != dict(self.config_entry.data):
            self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
        return self.async_create_entry(data=self._options)
