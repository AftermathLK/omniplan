# Changelog

## 1.3.3 (2026-10-29)

- **Docs**: new README with screenshots and a ten-minute setup, a full [user guide](docs/GUIDE.md), a [reference](docs/REFERENCE.md), an app icon and a social preview image.
- **Fix**: the default Home Maintenance category icon (`mdi:home-wrench`) doesn't exist in Material Design Icons, so it showed blank in the card and on its `todo.*` entity. New installs use `mdi:tools`, and existing plans are updated on startup.
- **Fix**: long grocery tile names no longer run under the tile's ⋯ button.
- Buttons that showed both an icon and an emoji (Scan, Add missing ingredients, timers, backup actions) now show just the icon, matching Home Assistant.

## 1.3.2 (2026-10-29)

- **Barcode scanner** for the pantry, maintenance parts and the card header: the browser's built-in reader where available and a bundled ZXing reader elsewhere, with flashlight, camera switch and manual entry. Parts get *Use* and *Restock* actions, pantry staples are marked In Stock, and new barcodes are linked in one tap with Open Food Facts suggestions.
- **Cooking view** for Mealie recipes: keep-screen-on (wake lock), ½× to 3× batch sizes, tick-off ingredients, step tracking, timers found in the instructions, notes and nutrition.
- **Portable backups**: a downloadable `.zip` (database, JSON dump, documents, checksum manifest, users and hardware bridge maps), daily rolling snapshots, the `omniplan.create_backup` service, and verified restore with *Merge* or *Full replace*.

## 1.3.0 (2026-10-29)

- One **category dropdown** in the header replaces the tab bar; menus stay inside the screen on phones.
- **Mealie recipe browser** inside Groceries: photos (through an authenticated proxy), filters, sorting, search, one-tap *Add ingredients* that skips seasonings and in-stock pantry items, *Review*, and *Plan meal*.
- **Pantry & Excluded Spices** drawer to edit the ignored-seasonings list and mark staples in stock.
- Removed the duration and context filters ("Quick wins").
- Fix: service-record PDFs no longer count a part's cost twice.

## 1.2.0 (2026-10-28)

First public release: Quick List with sections, time-blocked chores with streaks and shift-aware assignment, shopping grid with quantity merging, Kanban projects with bills of materials, trip packing, pantry, context snoozes, blocking dependencies, Brain Dump, home maintenance with sensor triggers, NFC and QR completion, learning intervals and the water-softener salt model, two-way Mealie sync with prep timelines and leftovers, native `todo.*` entities, and server-side privacy for private lists.
