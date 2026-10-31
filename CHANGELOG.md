# Changelog

## 1.4.0 (2026-10-31)

**Wishlist, gifts & price tracker.** A new category type (`wishlist_hoard`) for everything that isn't groceries; existing installations get a **Wishlist & Gifts** category.

- **Sections**: Want to Buy, Christmas and Birthdays to start, plus any occasion, each with an optional budget (*Spent $420 / $1,000 budget · 6 bought · 3 watching · 2 wrapped*), a date or yearly countdown, and a recipient.
- **Three tiers**: concept tags with spec notes, a target price and a **deal radar** (Slickdeals and Reddit deal searches); products tracked at up to 12 stores, with price and stock checked every few hours, history, a 30-day sparkline, the best price in green and *% below average*; and after purchase a 14- or 30-day **price-protection** watch.
- **Price extraction** runs a four-stage cascade: schema.org JSON-LD, product meta tags, built-in layouts for 14 large retailers, then an optional custom CSS selector per store (`selector@attribute`).
- **Alerts** at the target price, a new all-time low, and a drop below what you paid (*"💰 Price Drop Alert! … Claim your price adjustment!"*). They arrive as a persistent notification, through the wishlist's notify service, and as the new `taskdragon_price_alert` event.
- **In-store scan & price check**: scan a barcode for the product (UPCitemdb, then Open Food Facts), recorded online prices and one-tap store searches. Type the shelf price for a **Buy here / Cheaper online** verdict, then add to Want to Buy, add as a stealth gift, or mark **bought here**. That last one starts price protection.
- **Anti-spoiler privacy** enforced on the server: gifts are hidden from the person they're for (*🐉 1 secret gift hidden by Sam*), stealth items and sections are seen only by whoever added them, and kiosk views cover secret gifts until tapped. Alerts about secret gifts carry no details.
- **Gift pipeline** (Idea → Watching → Purchased → Shipped → Stashed → Wrapped → Gifted) with an encrypted **hiding spot** note. Portable backups carry its key (`secrets/hoard.key`).
- **7-day impulse cooldown** for bigger Want to Buy items.
- Store links can never reach your own network: only public `http(s)` addresses, every redirect re-checked, and DNS answers pointing inside the network refused.
- Database schema 5. New requirement `beautifulsoup4` (installed automatically).

## 1.3.4 (2026-10-31)

**OmniPlan is now TaskDragon.** Same integration, new name and emblem (a monochrome ouroboros dragon).

- Domain `taskdragon`: folder `custom_components/taskdragon`, services `taskdragon.*`, events `taskdragon_*`, WebSocket commands `taskdragon/*`, card `custom:taskdragon-card` (bundle `/taskdragon_static/taskdragon-card.js`), sidebar panel `/taskdragon`.
- The repository is now [AftermathLK/taskdragon](https://github.com/AftermathLK/taskdragon); the old `AftermathLK/omniplan` address redirects.
- The card header and the sidebar show the TaskDragon emblem (the `taskdragon:dragon` icon, also usable anywhere Home Assistant takes an icon).
- **Upgrading**: the first start copies the previous installation's database and documents, the config flow imports its people, schedule and Mealie connection, dashboards that still say `custom:omniplan-card` keep working, old `/omniplan` QR links still open, and old backups and Mealie `omniplan:` prep notes are still understood. See [Coming from OmniPlan?](README.md#coming-from-omniplan).

Versions below were released as OmniPlan.

## 1.3.3 (2026-10-29)

- **Docs**: new README with screenshots and a ten-minute setup, a full [user guide](docs/GUIDE.md), a [reference](docs/REFERENCE.md), an app icon and a social preview image.
- **Fix**: the default Home Maintenance category icon (`mdi:home-wrench`) doesn't exist in Material Design Icons, so it showed blank in the card and on its `todo.*` entity. New installs use `mdi:tools`, and existing plans are updated on startup.
- **Fix**: long grocery tile names no longer run under the tile's ⋯ button.
- Buttons that showed both an icon and an emoji (Scan, Add missing ingredients, timers, backup actions) now show just the icon, matching Home Assistant.

## 1.3.2 (2026-10-29)

- **Barcode scanner** for the pantry, maintenance parts and the card header: the browser's built-in reader where available and a bundled ZXing reader elsewhere, with flashlight, camera switch and manual entry. Parts get *Use* and *Restock* actions, pantry staples are marked In Stock, and new barcodes are linked in one tap with Open Food Facts suggestions.
- **Cooking view** for Mealie recipes: keep-screen-on (wake lock), ½× to 3× batch sizes, tick-off ingredients, step tracking, timers found in the instructions, notes and nutrition.
- **Portable backups**: a downloadable `.zip` (database, JSON dump, documents, checksum manifest, users and hardware bridge maps), daily rolling snapshots, the `taskdragon.create_backup` service, and verified restore with *Merge* or *Full replace*.

## 1.3.0 (2026-10-29)

- One **category dropdown** in the header replaces the tab bar; menus stay inside the screen on phones.
- **Mealie recipe browser** inside Groceries: photos (through an authenticated proxy), filters, sorting, search, one-tap *Add ingredients* that skips seasonings and in-stock pantry items, *Review*, and *Plan meal*.
- **Pantry & Excluded Spices** drawer to edit the ignored-seasonings list and mark staples in stock.
- Removed the duration and context filters ("Quick wins").
- Fix: service-record PDFs no longer count a part's cost twice.

## 1.2.0 (2026-10-28)

First public release: Quick List with sections, time-blocked chores with streaks and shift-aware assignment, shopping grid with quantity merging, Kanban projects with bills of materials, trip packing, pantry, context snoozes, blocking dependencies, Brain Dump, home maintenance with sensor triggers, NFC and QR completion, learning intervals and the water-softener salt model, two-way Mealie sync with prep timelines and leftovers, native `todo.*` entities, and server-side privacy for private lists.
