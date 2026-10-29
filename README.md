# OmniPlan

A local-first household planner that runs **entirely inside Home Assistant Core**: no add-on, no Docker, no cloud. Data lives in `<config>/.storage/omniplan.db` (SQLite via `aiosqlite`). Every plan is also a native `todo.*` entity, so Assist, automations and the stock To-do panel keep working, and the bundled `omniplan-card` / sidebar panel adds everything native to-do lists can't do.

- **Quick List home screen**: the card opens on a zero-friction list for everyday one-offs. Switch between the **shared household** list and **your private** one, organize items into collapsible, renamable, reorderable **sections** ("Calls to Make", "House Stuff"…), add from the top bar or inside any section, and **promote** an item into a structured plan, or make it recurring, with one tap.
- **Categories you control**: switch categories from one dropdown in the card's header, hide the ones you're not using, reorder them (per user), and create new categories on the spot: name, `mdi:` icon, shared or private, and the behavior you want (quick list, time-blocked chores, shopping grid, Kanban/DIY, trip packing, home maintenance & assets, pantry).
- **Plans with archetypes**: Daily chores (time-blocked agenda, streaks, rollover), Shopping (aisle grid, dedupe, frequently-bought drawer), Projects (Kanban ⇄ dependency tree, bill of materials), Home maintenance (objects, tasks, sensor triggers, parts and documents; see below), Pantry (In stock / Low / Out with restock actions).
- **Home maintenance & asset management**: every appliance, system or vehicle is an object (`#3`) with its tasks (`#3.1`), parts inventory, manuals and invoices, and a full service history. Tasks fall due on rolling or planned intervals, seasonal windows, or sensor triggers (thresholds, counters, runtime hours, cycle counts, leak sensors), and learn a better interval from your feedback. You can complete them by scanning an NFC tag or a printed QR code, and print a PDF service record for any date range.
- **Water softener telemetry**: a Droplet (or any total-water meter) drives a live salt estimate in gallons and days remaining. OmniPlan detects regenerations and learns how much water each level of salt lasts.
- **Trip packing**: dated, collapsible trips sorted by date, with big strike-through checkboxes, grouping (Clothes, Toiletries, Tech, Kids Gear…) and a live "18 / 24 Packed" bar. Flag any item **repeatable** and every new trip starts with a fresh, unchecked copy; one-offs stay with their trip.
- **Household-aware**: assign to one or more `person.*` entities or the "up for grabs" pool, one-tap Claim, a kiosk family split view, a personal "My Day", and "Last done by Sam yesterday at 6:15 PM" on recurring tasks.
- **PRN / shift-aware chores**: every morning, chores marked *shift-aware* go to whoever is off work (or whose shift ends first), based on each person's work calendar.
- **Blocking dependencies**: Task B stays `blocked` (and optionally hidden) until Task A is done, then pops into Ready.
- **Snooze until context**: until a time, next time I'm home, when we're all home, this weekend if it's not raining, or when any entity reaches a state or threshold.
- **Mealie**: browse your recipes inside Groceries and add a recipe's ingredients in one tap, without the spice rack or what's already in the pantry; plan meals onto Mealie's meal plan; prep timelines worked back from the meal plan (thaw T-36h, smoker T-4h…), "Cook …" tasks, leftover trackers with a countdown, pantry batch recipes, and two-way shopping-list sync with unit-normalized merging (`0.5 cup + 120 ml → 1 cup / 240 ml`).
- **Brain Dump**: type or speak "pick up 2x4s at Menards Saturday morning and add garlic powder to the grocery list", and it becomes a project task plus a grocery item. It uses your Assist LLM agent when one is configured and falls back to a built-in rules parser otherwise.

## Install

1. In HACS, add `https://github.com/AftermathLK/omniplan` as a custom repository (category **Integration**) and install OmniPlan, or copy `custom_components/omniplan` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant. The `aiosqlite` and `python-dateutil` requirements install automatically.
3. **Settings → Devices & services → Add integration → OmniPlan**, then walk through:
   - **Household**: your `person.*` entities, a `weather.*` entity, and optionally an Assist agent for Brain Dump.
   - **Work shifts**: one step per person, with an optional work calendar and shift keywords (comma separated; leave empty to treat every event on that calendar as a shift).
   - **Mealie** (optional): URL and API token (Mealie → Profile → API Tokens). The flow checks them live from Home Assistant (10-second timeout, no redirects) before saving.
   - **Schedule**: rollover hour, shift check time, weekend weather check time, meal times, leftovers, prep rules, sidebar panel.

Everything can be changed later under the integration's **Configure** button. In the Mealie step, leave the token field empty to keep the stored token.

OmniPlan seeds eight starter plans: Quick List (with "Calls to Make", "House Stuff", "Weekend" and "Quick Ideas" sections), Daily Chores, Groceries, Hardware, Projects, Home Maintenance, Trip Packing and Pantry & Spices. Every user also gets a private "My Quick List" the first time they open the card. Rename, hide, archive or add categories from the card.

Requires Home Assistant 2024.11 or newer. Link each household member's HA user to their person (**Settings → People**) so "My Day", Claim and "Last done by" know who is who.

## The card

The card's JavaScript is served and registered automatically, so you don't need to add a dashboard resource. Add it from the card picker ("OmniPlan") or in YAML:

```yaml
# Opens on the Quick List (the default)
type: custom:omniplan-card
view_mode: quick       # quick | auto | personal | kiosk_split
```

```yaml
# Phone / Companion app: opens on "My Day" for whoever is logged in
type: custom:omniplan-card
view_mode: auto
```

```yaml
# Wall tablet: family split view with avatar filter chips
type: custom:omniplan-card
view_mode: kiosk_split
title: Today
show_brain_dump: true
```

```yaml
# A single plan, no category dropdown (e.g. the grocery list on the fridge tablet)
type: custom:omniplan-card
plan: Groceries        # plan name or id
```

| Option | Default | Description |
| --- | --- | --- |
| `view_mode` | `quick` | `quick` opens on the Quick List, `personal` on My Day, `kiosk_split` on the family split, and `auto` picks personal when your user is linked to a person. If that category is hidden, the first visible one opens. |
| `plan` | none | Show one plan only (name or id). |
| `plans` | all | List of plan names or ids to offer in the category dropdown. |
| `person` | your person | Force whose "My Day" is shown. |
| `title` | `OmniPlan` | Card title. |
| `show_brain_dump` | `true` | Show the Brain Dump bar and mic button. |

The sidebar panel (**OmniPlan**) is the same card, full page.

Card tips:
- **Quick List**: type in the top bar and press Enter. Start with a section name to file it there ("Calls: dentist" lands in "Calls to Make"), or use the section picker or the "+ Add task" field inside a section. Section headers collapse (remembered per user, on every device), and the pencil, arrow and trash buttons rename, reorder and delete a section; deleting one moves its tasks to the inbox, with undo.
- **Promote**: the ➜ button on a quick-list item moves it into any plan (shopping items get an aisle, packing items go to the next trip), or **Repeat it…** opens the editor with a weekly schedule pre-filled so it becomes a recurring chore.
- **Categories**: the dropdown in the header (it shows the current category and how many items are open) lists every category with its count. At the bottom, **New category / plan** creates one and **Show / hide categories** hides, shows and reorders them for you only, and lists archived categories so you can restore them.
- **Trips**: "New trip" asks for a name and dates and pre-fills every repeatable item. Tap the big box to pack (it's crossed out instantly), tap ↻ to switch an item between *repeatable* and *one-off* (that applies to the item on every trip), and type "Tech: headlamp" to add into a group. Trips become active on their start date; archive them when you're home.
- Tap a task to expand its notes and checklist. Tap the pencil icon for the full editor (recurrence, assignees, dependencies and history).
- **Groceries**: the **Shopping list | Browse Mealie Recipes | Pantry & Excluded Spices** bar at the top switches between the list, the recipe browser and the import settings (described under Mealie below). Tapping **Mealie ✓** in the header jumps straight to the recipes.
- **Maintenance**: the blue object name opens the object (tasks, documents, parts, full history); the bold task name opens the task (KPIs, timeline, triggers, recommendation, activity). ✔ **Complete** on a row logs it right away with undo; the task page's Complete asks for cost, duration, parts, feedback and a photo.
- Drag the ⋮ handle to reorder, or to move between Morning/Afternoon/Evening, Kanban columns, aisles, quick-list sections, trips and packing groups, or between people in the family view (dropping on a person's column reassigns the task).
- Every completion and delete shows a 5-second **Undo**.
- The ⋮ menu has bulk edit, category settings (sharing, and for administrators routing roles and which Mealie list a shopping plan syncs with) and new categories.
- The mic uses the browser's speech recognition where it's available (Chrome and Android). On iOS, use the keyboard's dictation button or the voice automation below.

## Recurrence

- **Calendar** (`FREQ=DAILY`, `FREQ=WEEKLY;BYDAY=TU,FR`, `FREQ=MONTHLY;BYMONTHDAY=1`, any RRULE): the next due date is the next occurrence after both today and the current due date, so a late completion never schedules the next one in the past.
- **Rolling**: N days after the day you actually finished it.

Completed recurring tasks stay checked until the rollover hour, then re-open, and their checklists reset with them. Streaks count consecutive on-time completions. Maintenance tasks have their own scheduling, described next.

## Home maintenance & assets

A maintenance plan opens on a five-number banner (**Objects · Tasks · Overdue · Due soon · Triggered**; tap one to filter), with **Today**, **Dashboard**, **Calendar** and **Settings** views and a 🔍 search across objects, tasks, parts, notes and documents.

**Objects** are the things you maintain: a furnace, the water softener, a car. Each one has a model, a Home Assistant **area**, an install date, a **warranty expiry** (with a reminder), Markdown notes, and optionally a parent ("Humidifier" inside "Furnace"). Objects are numbered `#1`, `#2`…, their tasks `#2.1`, `#2.2`…, and each logged event `#2.1-4`. Pick a **zone** on an object to hold its notifications until someone from the household is in that zone, so the furnace filter reminder arrives when you get home.

**Tasks** have a category (Replacement, Service, Cleaning, Inspection, Calibration, Refill) and a status: **OK**, **Due soon** (inside the warning window), **Overdue**, or **Triggered** (a sensor condition is met).
- **Rolling** intervals count from the day it was actually done. **Planned** intervals stay on an anchor date, so a late filter change doesn't shift the next one. Either can be in days, weeks, months or years.
- An optional **time of day** decides when a task due today turns Overdue. **Active months** limit a task to a season (a humidifier pad from October to April); due dates that fall outside are moved to the next active month.
- **Sensor triggers**, combined with AND or OR, and alongside a schedule or instead of one:
  - **Threshold**: above or below a value for N minutes (filter ΔP, battery %).
  - **Counter**: a total that increases by N (gallons through a filter, kWh, odometer miles, 3D-printer hours).
  - **Runtime**: hours spent in a state, including attributes such as a thermostat's `hvac_action: heating`.
  - **Cycles**: N state changes with a debounce, for things like dishwasher runs. Restarts don't count as changes.
  - **Problem sensor**: a leak or problem sensor turning on. It can mark the task done automatically when the sensor recovers.
- **When completed**, a task can take parts out of stock, rotate the assignee to the next person, and (if an administrator set it up) run a service such as `button.press` on a filter-reset button.
- **Adaptive interval**: after each completion, answer *Too early*, *Just right* or *Too late*. OmniPlan blends an exponentially weighted average of your real intervals, a Weibull fit and 12-month seasonal factors into a suggestion. **Apply recommended interval** adopts it, and **Re-analyze** recomputes it.
- **Complete** logs the date, who did it, duration, labor cost, the parts used, notes, the feedback and a photo. **Skip** records *Skipped* before the due date, or *Missed* once the task is overdue or triggered.

**NFC and QR**: in a task's ⋮ menu, **Bind NFC tag** listens for 60 seconds; scan a Home Assistant tag at the appliance and it's linked. From then on, scanning that tag completes the task ("Completed via NFC tag"). Tick **Only complete by scanning the tag** to require being there. **QR code** (on any object or task) prints or downloads a label that opens the item in the OmniPlan panel, or, for tasks, asks *Mark as done?* right away. QR links open the OmniPlan sidebar panel, so keep it enabled in the integration's options, and set your Home Assistant URL (Settings → System → Network) so phones can open them.

**Parts & consumables** have a part number or GTIN, a storage location, stock, a reorder threshold and quantity, a unit cost and a reorder link. When stock reaches the threshold, the part goes on your hardware (or chosen) shopping list, or becomes a "Buy …" chore. Checking that item off adds the restock quantity back to stock. In **Settings**, choose whether a part's cost counts when it's **used** by a task or when it's **bought**; the monthly, yearly and per-area spend and every total follow that choice, so a part is never counted twice.

**Documents**: manuals, invoices, warranties and photos (PDF, PNG, JPEG, WebP, GIF, HEIC, plain text; 15 MB each) or plain links. Files are stored once by SHA-256 under `<config>/.storage/omniplan_docs/`. Link a task to its manual and a page number to open the PDF at that page. **Service record (PDF)** on an object exports its history for a From/To range (optionally one task), with a total cost.

**Water softener** objects get a salt gauge. In Edit, link a **total water volume** sensor (Droplet, a `utility_meter`, any `total_increasing` sensor), optionally a **flow rate** sensor for regeneration detection (sustained flow inside the overnight window, which is subtracted from household use) and an ESPHome **distance sensor** on the brine tank. Then tell OmniPlan what you see:
- **Filled salt**: the tank is now at a level, or you added N bags. This completes the refill task and takes the bags out of stock.
- **Salt is at Level N**: a pass-by observation.
- **Tank empty / bridges checked**.

Each observation teaches it how many gallons a level of salt lasts. It learns per band, so the narrow cone at the bottom of the tank isn't treated like the top. The gauge shows the **estimated level**, **gallons until refill**, and **days until refill / empty** based on your last 14 days of water use. The refill task goes *Due soon*, then *Triggered*, as it nears your warning level. Levels can be read as marks (8 → 1), a percentage or bags.

**+ Add ▾** creates an object or task, starts **from a template** (furnace, water softener, water heater, smoke detectors, refrigerator, dryer, dishwasher, vehicle, 3D printer, gutters, humidifier, lawn mower, sump pump…), or runs **One-click battery fleet**, which adds a "Replace Battery" task for every battery sensor Home Assistant knows about (triggered below 15 % for an hour, with the battery type as a part). The dashboard's **Views** (💾 saves the current filters per user), **Filter**, **User**, **Priority**, **Sort** and **Group by** controls, plus **Select** for bulk complete, skip, assign or delete, work like you'd expect.

Each maintenance plan is also a `todo.*` entity: open tasks show as "Object: Task", and checking one off logs a completion.

Upgrading from 1.1 moves the old maintenance list into a "General" object. Rolling recurrence stays rolling, calendar rules become planned intervals, sensor thresholds become counter or problem triggers, checklists move into the notes and completion logs become history.

## Services

All services return data when called with `response_variable`.

| Service | What it does |
| --- | --- |
| `omniplan.parse_brain_dump` | Free text → tasks and shopping items (`dry_run: true` only parses). |
| `omniplan.add_task` | Create a task (`plan` takes a name, id, or the plan's `todo.*` entity). |
| `omniplan.complete_task` | Complete or re-open by `uid` or exact `summary`. |
| `omniplan.snooze_task` | `time`, `person_home`, `all_home`, `weekend_dry` or `entity_state`. |
| `omniplan.add_shopping_item` | Adds an item and merges quantities with an existing entry. |
| `omniplan.trigger_pantry_low` | Set stock and run the item's low-stock action. |
| `omniplan.run_shift_balance` | Run today's shift-aware assignment now. |
| `omniplan.sync_mealie` | Sync now. |
| `omniplan.create_trip` | Start a trip in a packing plan; repeatable items are copied onto it. |
| `omniplan.clone_plan` | Duplicate a plan (its sections and every item that isn't part of a trip). |
| `omniplan.reset_plan` | Un-check every item in a plan. |
| `omniplan.complete_maintenance` | Log a maintenance task as done (`task`: id, `#3.1`, `Furnace / Replace Filter` or a unique name; optional `cost`, `duration_minutes`, `notes`). NFC-only tasks refuse. |
| `omniplan.log_salt_level` | `fill` (with `level` or `bags`), `level` or `empty` for a water softener `object` (name, id or `#2`). |

A service call made by a signed-in user (a dashboard button, a script they run, Assist) acts as that user, so it only sees household plans and that user's private lists. Calls from automations act as OmniPlan itself.

### Voice Brain Dump through Assist

```yaml
automation:
  - alias: OmniPlan brain dump by voice
    triggers:
      - trigger: conversation
        command:
          - "brain dump {text}"
          - "remind me to {text}"
          - "add {text} to the grocery list"
    actions:
      - action: omniplan.parse_brain_dump
        data:
          text: "{{ trigger.sentence }}"
        response_variable: dump
      - set_conversation_response: >-
          Added {{ dump.results | selectattr('ok') | map(attribute='summary') | join(', ') or 'nothing' }}.
```

### Events for automations

| Event | Data |
| --- | --- |
| `omniplan_task_completed` | `uid`, `summary`, `plan_id`, `person`, `user_name`, `next_due`, `unblocked` (for private lists `summary` is `null` and `private` is `true`) |
| `omniplan_task_unsnoozed` | `uid`, `summary`, `plan_id`, `reason`, `assigned_to` |
| `omniplan_pantry_low` | `item_id`, `name`, `state`, `action` |
| `omniplan_maintenance_due` | `task_id`, `object_id`, `plan_id`, `code`, `name`, `object_name`, `status` (`due_soon`, `overdue` or `triggered`), `countdown`, `assigned_to` |
| `omniplan_maintenance_completed` | `uid` (the task id), `name`, `object`, `code`, `method` (`manual`, `nfc`, `qr`, `todo`, `service`, `auto_recovery`), `person`, `user_name`, `next_due`, `plan_id` |
| `omniplan_updated` | `plan_ids` |

For private lists, the name fields are `null` and `private` is `true`. For example, to notify the assignee when a "next time I'm home" task wakes up, trigger on `omniplan_task_unsnoozed`. For maintenance, pick a `notify.*` service in the plan's **Settings** (administrators), or build your own automation on `omniplan_maintenance_due`.

## Mealie

- **Shopping sync**: open a shopping plan's settings in the card and choose a Mealie list. Items sync both ways every N minutes and a few seconds after any local change. Adds, check-offs, edits and deletes all propagate, and an item already on both sides is linked rather than duplicated. Items OmniPlan creates in Mealie are plain note items, for example "2 lb ground beef".
- **Prep timelines**: for meals in the next N days, OmniPlan reads the recipe and creates prep tasks in your chores plan. Lead times come from:
  - recipe **tags** shaped like `thaw-36h`, `brine-24h`, `smoker-4h`, `marinate-12h`, `soak-8h`;
  - recipe **notes or description** containing `Lead time: thaw 36h, smoker 4h`, or a recipe **extra** (custom field) named `omniplan` with the same text;
  - **configured rules** in the options, one per line: `tag-or-keyword => hours | template`, where templates can use `{recipe}`, `{meat}` and `{action}`. Keyword rules (e.g. `brisket => 36 | …`) match the recipe name or notes.
- **Cook tasks and leftovers**: a "Cook Smoked Brisket" task is due at meal time minus the recipe's total time. When it's checked off, or at the leftover time on the meal day, OmniPlan adds "Eat or freeze leftover … (Cooked Tuesday)" with a countdown that turns urgent when it expires.
- **Recipe browser** (Groceries → **Browse Mealie Recipes**): every Mealie recipe with its photo, categories, total time and how often it was on your meal plan in the last year. Filter by category or tag, sort A–Z, by recently added, by most cooked or by category, and search. Each recipe has three actions:
  - **Add ingredients** adds what you need to that grocery list straight away, merging quantities with items already on it: *"Added 6 items from Chicken Sausage Orzo (Skipped 4 seasonings/pantry items)"*.
  - **Review** opens the ingredient list with seasonings and in-stock pantry items unchecked. Tick anything you're out of, untick what you have, and add.
  - **Plan meal** picks a day (the next week as one-tap chips, or any date) and a meal, puts the recipe on Mealie's meal plan and optionally adds its ingredients too. Prep and cook tasks for that meal appear after the next sync, which starts right away.
- **Pantry & Excluded Spices**: the seasoning filter is on by default. It ignores the household's list of seasonings and staples (salt, black pepper, garlic powder, paprika, cumin, oregano, olive oil, water, cooking spray…), which you can add to, prune or reset, plus any Mealie food labelled as a spice or seasoning. Matching ignores amounts and words like *ground*, *dried*, *freshly* or *to taste*, so "1 tsp freshly ground black pepper, to taste" counts as black pepper, while "coconut water" and "bell pepper" are still bought. Anything your pantry has **in stock** is skipped too (a pantry "Flour" covers "all-purpose flour"); the same drawer lists your pantry staples with a checkbox each to mark what's in the house right now.
- **Pantry batches**: link a pantry item to a Mealie recipe slug and press **Load ingredients from Mealie**. When the item goes Low or Out, OmniPlan either pushes the raw ingredients to the shopping list or creates a "Make a new batch of …" chore with the recipe link and ingredient list, depending on the item's setting (or it asks each time).

## Privacy & security

- **Private lists are filtered on the server.** A private category, including each user's "My Quick List", is returned only to its owner and the household members it's explicitly shared with. Every WebSocket command checks each plan, task, section, trip, pantry or BOM id it's given against the signed-in user before doing anything. Ids the user can't see answer `not_found`, exactly like ids that don't exist. Administrators get no back door through the API. Private lists have no `todo.*` entity, because entity state is readable by every Home Assistant user, and bus events for them carry no task text.
- **Admin-only changes**: deleting a household category (`omniplan/delete_plan`), listing Mealie shopping lists and running shift balancing use Home Assistant's `require_admin`. Changing routing roles, Mealie links, archiving, or making a household list private also need an administrator (or the list's creator). Owners delete their own private lists with `omniplan/delete_private_plan`. Tab order, hidden tabs and collapsed sections are per-user preferences.
- **Mealie credentials** live in the config entry's `data` (`.storage/core.config_entries`; protected by file permissions, not encrypted), never in options, entity attributes, WebSocket payloads or the card. The card receives a sync status and, in the recipe browser, recipe names, categories and Mealie links, but never the token. Recipe photos are fetched by Home Assistant and passed to the card through an authenticated endpoint (`/api/omniplan/mealie_image/…`), so they work away from home. That endpoint only accepts recipe ids, only passes through real image types up to 3 MB, and serves them with `nosniff` and a sandboxing CSP. All Mealie traffic goes from Home Assistant's own HTTP session, with a 10-second timeout, no redirects (so the token is never replayed to another host), percent-encoded path segments and validated recipe slugs. Error messages and logs never contain the URL, the token or response bodies. Upgrading from 1.0 moves an existing token out of options automatically.
- **Diagnostics** (Settings → Devices & services → OmniPlan → ⋮ → Download diagnostics) contain settings and counts only; the token, URLs, user ids, calendars and private list names are `**REDACTED**`.
- **SQL**: every value is a `?` parameter. The only text spliced into SQL is table and column names, which must appear in the schema allow-list. A unit test fails the build if an f-string or `%` formatting builds SQL anywhere in the integration.
- **XSS**: the card renders through an escape-by-default `html` tagged template (the same model as Lit's bindings). Every interpolated value is HTML-escaped unless it is itself trusted template output, and an allow-list sanitizer then strips any element or attribute OmniPlan doesn't use (scripts, `on*` handlers, `javascript:` links) before anything reaches the live page. Links are only ever `http(s)`, both when stored and when rendered.
- **Undo** data stays on the server behind single-use tokens tied to your user, so a client can never "restore" records into someone else's list.
- **Maintenance**: objects, tasks, parts and documents go through the same per-plan access checks as everything else, including the upload, download and PDF endpoints (`/api/omniplan/…`, Home Assistant authentication required). The card opens files through short-lived signed URLs. Uploads are accepted by their content (magic bytes), not their name or claimed type. Anything outside the allowed types is refused, filenames are sanitized, and files are served with `nosniff` and a sandboxing CSP. Only administrators can set a task's on-complete service call or a plan's notify service, because both run with Home Assistant's privileges. Reorder and document links must be `http(s)`.

## How it fits together

```
custom_components/omniplan/
├── __init__.py        setup/unload, static path + extra JS module, sidebar panel
├── config_flow.py     household → per-person shifts → Mealie → schedule (also the options flow)
├── coordinator.py     timers, state listeners, shift balancing, weekend weather, Mealie sync, leftovers
├── planner.py         all task rules: completion + audit log, recurrence, dependencies, snoozes, pantry, BOM,
│                      quick-list sections, trips, access control, undo tokens, per-user preferences
├── database.py        aiosqlite schema + migrations (plans, tasks, sections, trips, task_dependencies,
│                      completion_logs, pantry_items, snooze_rules, bom_items, item_history, user_prefs, …)
├── todo.py            one native TodoListEntity per household plan
├── websocket_api.py   omniplan/* commands (each with an access guard) + per-user omniplan/subscribe
├── diagnostics.py     redacted diagnostics download
├── services.py        omniplan.* services
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
├── maintenance_ws.py  omniplan/maint_* WebSocket commands
├── http_views.py      document upload/download, the service-record PDF and the Mealie image proxy
├── qr.py              QR-code encoder (Reed–Solomon, byte mode)
├── pdf.py             minimal PDF table writer
└── frontend/omniplan-card.js   zero-dependency card, panel and card editor (escape-by-default rendering)
```

The modules that hold the logic (`planner`, `database`, `units`, `dependencies`, `recurrence`, `shift_balance`, `snooze`, `kitchen`, `brain_dump`, `maintenance`, `maintenance_manager`, `qr`, `pdf`, `recipes`, `recipe_browser`) don't import Home Assistant, which is why most of the behaviour can be unit-tested without it.

The database file and the `omniplan_docs` folder are included in Home Assistant backups because they live in `.storage`. Deleting the integration leaves them in place.

## Development

```bash
pip install -r requirements_test.txt
pytest tests          # pure logic, no Home Assistant needed
pytest tests_ha       # end-to-end against a real HA core (pytest-homeassistant-custom-component)
```

`tests_ha/conftest.py` contains two Windows-only shims (the proactor event loop's socket pair, and aiodns). On Linux CI the Home Assistant test plugin's socket guard stays fully active.
