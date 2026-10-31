# TaskDragon reference

Services, events, the security model and how the code is organised. For day-to-day use see the [user guide](GUIDE.md).

- [Services](#services)
- [Privacy & security](#privacy--security)
- [How it fits together](#how-it-fits-together)
- [Development](#development)

## Services

All services return data when called with `response_variable`.

| Service | What it does |
| --- | --- |
| `taskdragon.parse_brain_dump` | Free text → tasks and shopping items (`dry_run: true` only parses). |
| `taskdragon.add_task` | Create a task (`plan` takes a name, id, or the plan's `todo.*` entity). |
| `taskdragon.complete_task` | Complete or re-open by `uid` or exact `summary`. |
| `taskdragon.snooze_task` | `time`, `person_home`, `all_home`, `weekend_dry` or `entity_state`. |
| `taskdragon.add_shopping_item` | Adds an item and merges quantities with an existing entry. |
| `taskdragon.trigger_pantry_low` | Set stock and run the item's low-stock action. |
| `taskdragon.run_shift_balance` | Run today's shift-aware assignment now. |
| `taskdragon.sync_mealie` | Sync now. |
| `taskdragon.create_trip` | Start a trip in a packing plan; repeatable items are copied onto it. |
| `taskdragon.clone_plan` | Duplicate a plan (its sections and every item that isn't part of a trip). |
| `taskdragon.reset_plan` | Un-check every item in a plan. |
| `taskdragon.complete_maintenance` | Log a maintenance task as done (`task`: id, `#3.1`, `Furnace / Replace Filter` or a unique name; optional `cost`, `duration_minutes`, `notes`). NFC-only tasks refuse. |
| `taskdragon.log_salt_level` | `fill` (with `level` or `bags`), `level` or `empty` for a water softener `object` (name, id or `#2`). |
| `taskdragon.create_backup` | Write a portable snapshot to `/config/backups/taskdragon/` and prune old ones (administrators; automations may call it, e.g. before an update). Returns the file name, size and row count. |

A service call made by a signed-in user (a dashboard button, a script they run, Assist) acts as that user, so it only sees household plans and that user's private lists. Calls from automations act as TaskDragon itself.

### Voice Brain Dump through Assist

```yaml
automation:
  - alias: TaskDragon brain dump by voice
    triggers:
      - trigger: conversation
        command:
          - "brain dump {text}"
          - "remind me to {text}"
          - "add {text} to the grocery list"
    actions:
      - action: taskdragon.parse_brain_dump
        data:
          text: "{{ trigger.sentence }}"
        response_variable: dump
      - set_conversation_response: >-
          Added {{ dump.results | selectattr('ok') | map(attribute='summary') | join(', ') or 'nothing' }}.
```

### Events for automations

| Event | Data |
| --- | --- |
| `taskdragon_task_completed` | `uid`, `summary`, `plan_id`, `person`, `user_name`, `next_due`, `unblocked` (for private lists `summary` is `null` and `private` is `true`) |
| `taskdragon_task_unsnoozed` | `uid`, `summary`, `plan_id`, `reason`, `assigned_to` |
| `taskdragon_pantry_low` | `item_id`, `name`, `state`, `action` |
| `taskdragon_maintenance_due` | `task_id`, `object_id`, `plan_id`, `code`, `name`, `object_name`, `status` (`due_soon`, `overdue` or `triggered`), `countdown`, `assigned_to` |
| `taskdragon_maintenance_completed` | `uid` (the task id), `name`, `object`, `code`, `method` (`manual`, `nfc`, `qr`, `todo`, `service`, `auto_recovery`), `person`, `user_name`, `next_due`, `plan_id` |
| `taskdragon_barcode_action` | `barcode`, `target_type` (`pantry_item` or `maintenance_part`), `target_id`, `name`, `action` (`use_one`, `restock_qty`, `set_pantry_status`), `quantity` or `status`, `user_id` |
| `taskdragon_updated` | `plan_ids` |

For private lists, the name fields are `null` and `private` is `true`. For example, to notify the assignee when a "next time I'm home" task wakes up, trigger on `taskdragon_task_unsnoozed`. For maintenance, pick a `notify.*` service in the plan's **Settings** (administrators), or build your own automation on `taskdragon_maintenance_due`.

## Privacy & security

- **Private lists are filtered on the server.** A private category, including each user's "My Quick List", is returned only to its owner and the household members it's explicitly shared with. Every WebSocket command checks each plan, task, section, trip, pantry or BOM id it's given against the signed-in user before doing anything. Ids the user can't see answer `not_found`, exactly like ids that don't exist. Administrators get no back door through the API. Private lists have no `todo.*` entity, because entity state is readable by every Home Assistant user, and bus events for them carry no task text.
- **Admin-only changes**: deleting a household category (`taskdragon/delete_plan`), listing Mealie shopping lists and running shift balancing use Home Assistant's `require_admin`. Changing routing roles, Mealie links, archiving, or making a household list private also need an administrator (or the list's creator). Owners delete their own private lists with `taskdragon/delete_private_plan`. Tab order, hidden tabs and collapsed sections are per-user preferences.
- **Mealie credentials** live in the config entry's `data` (`.storage/core.config_entries`; protected by file permissions, not encrypted), never in options, entity attributes, WebSocket payloads or the card. The card receives a sync status and, in the recipe browser, recipe names, categories and Mealie links, but never the token. Recipe photos are fetched by Home Assistant and passed to the card through an authenticated endpoint (`/api/taskdragon/mealie_image/…`), so they work away from home. That endpoint only accepts recipe ids, only passes through real image types up to 3 MB, and serves them with `nosniff` and a sandboxing CSP. All Mealie traffic goes from Home Assistant's own HTTP session, with a 10-second timeout, no redirects (so the token is never replayed to another host), percent-encoded path segments and validated recipe slugs. Error messages and logs never contain the URL, the token or response bodies. Upgrading from 1.0 moves an existing token out of options automatically.
- **Diagnostics** (Settings → Devices & services → TaskDragon → ⋮ → Download diagnostics) contain settings and counts only; the token, URLs, user ids, calendars and private list names are `**REDACTED**`.
- **SQL**: every value is a `?` parameter. The only text spliced into SQL is table and column names, which must appear in the schema allow-list (for a backup restore, in the live database's own schema). A unit test fails the build if an f-string or `%` formatting builds SQL anywhere in the integration.
- **XSS**: the card renders through an escape-by-default `html` tagged template (the same model as Lit's bindings). Every interpolated value is HTML-escaped unless it is itself trusted template output, and an allow-list sanitizer then strips any element or attribute TaskDragon doesn't use (scripts, `on*` handlers, `javascript:` links) before anything reaches the live page. Links are only ever `http(s)`, both when stored and when rendered.
- **Undo** data stays on the server behind single-use tokens tied to your user, so a client can never "restore" records into someone else's list.
- **Barcodes**: lookups, links and scan actions go through the same per-plan checks. A barcode linked to something in a list you can't see behaves like an unknown code, the pickers only list what you can see, and you can't re-link someone else's private item. The camera feed never leaves the phone; only the decoded number goes to Home Assistant. For unknown retail codes Home Assistant asks Open Food Facts about that number only (no account, no household data, 4-second timeout, no redirects, 512 KB cap, answers cached). An administrator can switch that lookup off in Settings.
- **Backups**: listing, creating, downloading, deleting and restoring backups is administrator-only, over WebSocket (`require_admin`) and HTTP (`/api/taskdragon/backup/…`, which also accepts short-lived signed paths so downloads save natively in the Companion apps). Uploads stream to disk with a 1 GB cap. Zip members are read by exact name with size limits, so nothing is ever extracted to a path taken from the archive. Restored table and column names are checked against the live schema before they reach SQL, and values must be plain scalars. A backup holds the whole household's data, including private lists and people's names, so keep the file somewhere safe.
- **Maintenance**: objects, tasks, parts and documents go through the same per-plan access checks as everything else, including the upload, download and PDF endpoints (`/api/taskdragon/…`, Home Assistant authentication required). The card opens files through short-lived signed URLs. Uploads are accepted by their content (magic bytes), not their name or claimed type. Anything outside the allowed types is refused, filenames are sanitized, and files are served with `nosniff` and a sandboxing CSP. Only administrators can set a task's on-complete service call or a plan's notify service, because both run with Home Assistant's privileges. Reorder and document links must be `http(s)`.

## How it fits together

```
custom_components/taskdragon/
├── __init__.py        setup/unload, static path + extra JS module, sidebar panel
├── config_flow.py     household → per-person shifts → Mealie → schedule (also the options flow)
├── coordinator.py     timers, state listeners, shift balancing, weekend weather, Mealie sync, leftovers
├── planner.py         all task rules: completion + audit log, recurrence, dependencies, snoozes, pantry, BOM,
│                      quick-list sections, trips, access control, undo tokens, per-user preferences
├── database.py        aiosqlite schema + migrations (plans, tasks, sections, trips, task_dependencies,
│                      completion_logs, pantry_items, snooze_rules, bom_items, item_history, user_prefs, barcode_mappings, …)
├── todo.py            one native TodoListEntity per household plan
├── websocket_api.py   taskdragon/* commands (each with an access guard) + per-user taskdragon/subscribe
├── diagnostics.py     redacted diagnostics download
├── services.py        taskdragon.* services
├── brain_dump.py      LLM prompt + JSON validation, deterministic rules fallback
├── mealie_client.py   async Mealie REST client (v2 household routes, v1 group fallback)
├── recipes.py         recipe summaries and the seasoning / pantry import filter
├── recipe_browser.py  cached recipe list, image cache, recipe → groceries and meal planning
├── units.py           quantity parsing, unit normalization/merging, aisle categorization
├── kitchen.py         prep-rule discovery, leftover naming, ingredient lines
├── dependencies.py    blocking-dependency resolution and cycle detection
├── recurrence.py      RRULE and rolling recurrence
├── shift_balance.py   PRN / shift-aware assignment
├── snooze.py          context-snooze rule evaluation
├── maintenance.py     intervals, seasons, trigger state machines, status, Weibull/EWA learning,
│                      salt scales and estimates, spend reports, templates, battery fleet
├── maintenance_manager.py   objects, tasks, parts, documents, completions, NFC, salt learning, board views
├── maintenance_runtime.py   HA glue: state + tag events, zone-gated notifications, document files
├── maintenance_ws.py  taskdragon/maint_* WebSocket commands
├── http_views.py      document upload/download, service-record PDF, Mealie image proxy, backup download/restore
├── barcodes.py        GTIN normalization (UPC-A/E, EAN), Open Food Facts response parsing, name matching
├── barcode_manager.py barcode → pantry item / part links, lookups, use / restock / stock actions with undo
├── barcode_ws.py      taskdragon/barcode_* WebSocket commands
├── openfoodfacts.py   cached, size-capped Open Food Facts product lookups
├── backup_manager.py  portable zip export, users / hardware bridge maps, verified replace / merge restore, snapshots
├── backup_ws.py       taskdragon/backup_* WebSocket commands (administrators)
├── migration.py       carries an installation over from the integration's former name (OmniPlan)
├── qr.py              QR-code encoder (Reed–Solomon, byte mode)
├── pdf.py             minimal PDF table writer
└── frontend/
    ├── taskdragon-card.js   zero-dependency card, panel and card editor (escape-by-default rendering)
    └── vendor/            ZXing barcode reader (Apache-2.0), loaded only where the browser has no BarcodeDetector
```

The modules that hold the logic (`planner`, `database`, `units`, `dependencies`, `recurrence`, `shift_balance`, `snooze`, `kitchen`, `brain_dump`, `maintenance`, `maintenance_manager`, `qr`, `pdf`, `recipes`, `recipe_browser`, `barcodes`, `barcode_manager`, `backup_manager`, `migration`) don't import Home Assistant, which is why most of the behaviour can be unit-tested without it.

The database file and the `taskdragon_docs` folder are included in Home Assistant backups because they live in `.storage`, and so are the portable snapshots in `backups/taskdragon/`. Deleting the integration leaves them all in place.

`frontend/vendor/zxing-library-0.23.0.min.js` is the unmodified UMD build of [`@zxing/library`](https://github.com/zxing-js/library) 0.23.0, © ZXing authors, under the Apache License 2.0 (`frontend/vendor/zxing-library-LICENSE.txt`).

## Development

```bash
pip install -r requirements_test.txt
pytest tests          # pure logic, no Home Assistant needed
pytest tests_ha       # end-to-end against a real HA core (pytest-homeassistant-custom-component)
```

`tests_ha/conftest.py` contains two Windows-only shims (the proactor event loop's socket pair, and aiodns). On Linux CI the Home Assistant test plugin's socket guard stays fully active.
