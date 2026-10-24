# library.culture.dev site design

The site's visual and interaction design. The operator approved it on
2026-10-24 as part of the `library-culture-dev` devague spec.

- **Reference prototype:**
  [Culture Library preview](https://claude.ai/artifact/2z94SGy76vPeWT1jUGQafz).
  It is a private Claude artifact, version 3, built from live data on spark. It
  is the visual reference; this file is the durable contract.
- **Where differences go:** when the prototype and this file disagree, this
  file wins. Fix the prototype, or record the change here.
- **The built site must look exactly like the prototype.** Two committed files
  make that testable:
  - [`prototype.html`](prototype.html) is the prototype's source, with
    `/*DATA*/` where the live data was embedded.
  - [`reference/`](reference/) holds 30 screenshots of it: desktop
    1680×1000 and phone 400×860, in light and dark. They cover home, the
    newspaper, spec and frame readers, the hidden list, the list drawer, the
    send menu and the sent state, site state and upload.
  - The operator approved these screenshots on 2026-10-24.
- **Obligations o1–o7 on claim c32** in the devague frame tie the build to
  these files.

## Look

- **Feel**: pleasant and soft, in the spirit of MongoDB, monday.com and
  Coralogix. Not an e-ink or broadsheet look.
- **Type**: Lato for everything, served from Google Fonts with a
  `"Helvetica Neue", Arial, system-ui, sans-serif` fallback.
  - Headings use 700. Page titles and big numbers use 900.
  - Body text uses 400. Ledes and quotes use 300.
  - No serif display face.
- **Themes**: light and dark, both designed. An in-page sun/moon toggle sets
  `data-theme` and is remembered per browser. Without a choice, the site
  follows `prefers-color-scheme`.

| Token | Light (cream) | Dark (blue) |
|-------|---------------|-------------|
| `--bg` | `#faf5e8` | `#172b47` |
| `--surface` | `#fffcf3` | `#1d3354` |
| `--surface-2` | `#f2ead5` | `#253f66` |
| `--ink` | `#12301f` | `#eef2f7` |
| `--ink-2` | `#4b5d50` | `#b9c6d8` |
| `--muted` | `#6f7868` | `#8fa0b8` |
| `--rule` | `#e8dfc8` | `#2c4872` |
| `--accent` (forest green) | `#00684a` | `#5fd897` |
| `--accent-soft` | `#dcefdf` | `#23485a` |

**Kind colors.** These are categorical, in a fixed order. They were checked
with the dataviz validator against both surfaces: colorblind separation
ΔE ≥ 8.4, and normal vision ΔE ≥ 19.3.

| Kind | Light | Dark |
|------|-------|------|
| Newspaper | `#2a78d6` | `#3987e5` |
| Document | `#eb6834` | `#d95926` |
| Book | `#1baf7a` | `#199e70` |
| Transcript | `#eda100` | `#c98500` |
| Spec or plan | `#e87ba4` | `#d55181` |
| Diary | `#008300` | `#008300` |

**A kind color never appears without its label.** Three light-mode kinds and
dark-mode diary are below 3:1 contrast against the surface. Status colors
(good, warning, critical) always carry an icon and a label.

## Layout

- **Width**: content runs up to 1,760px. The side gutter is 32px on wide
  screens and 16px on phones, plus safe-area insets. No page ever scrolls
  sideways at 400px.
- **Top bar** (sticky), in this order:
  1. brand
  2. *Library* / *Site state* tabs
  3. full-text search
  4. **Upload** button
  5. theme toggle

  Below 720px the search box takes its own row and Upload shrinks to an icon.
- **Home** (nothing open):
  - An intro with the shelf: one spine per item, colored by kind. Faded spines
    stand for content that has not arrived yet.
  - **Continue reading**: the last opened item as a large card with reading
    progress, plus the two newest items. On a first visit this row reads
    "Start with the newest".
  - **Latest**: a card grid (`auto-fill, minmax(270px, 1fr)`), newest first,
    with kind filter chips.
- **Reader** (an item is open):
  - On the left is a list pane (340px) with kind filters and items grouped by
    day. Search snippets appear under matching rows.
  - The pane collapses and expands with *Hide list* / *Show list*. That state
    is remembered on desktop.
  - The reader toolbar has, in order: the pane toggle, *All cards* (back to
    home), *Send to device*, and previous/next.
  - At phone width, *All cards* and *Send to device* show only their icons,
    so the toolbar fits on one row.
  - On phones (≤ 900px) the pane is a drawer over a scrim, closed by default.
    It closes on a tap outside or on Escape.
  - The reading column is 76ch, or 96ch from 1,500px up.
- **Per-kind readers**:
  - **Newspaper**: sections with item lists.
  - **Spec**: lede, then before/after, then requirements, each with its
    honesty condition.
  - **Devague frame**: a confirmed/proposed progress bar, claims grouped by
    kind with status pills, hard questions with their decisions, and
    collapsible scope and unknowns.
  - **Uploads**: EPUB sections, or pre-formatted text for Markdown, text and
    JSON.
- **Site state**: four stat tiles, a status list (icon and label), and an
  `auto-fill` chart grid.
  - Every chart has a hover tooltip, a *Table* toggle, and a source line.
  - Single-series marks use `--accent`.
  - Kind charts use the kind colors with direct labels.

## Behaviors

- **Search** matches titles and full text, highlights matches in the reader,
  and opens the first full-text hit.
- **Upload** offers drag-and-drop or a file picker for EPUB, PDF, Markdown,
  text or JSON, up to 50 MB.
  - The form sets the title (defaulting to the file name), the kind, and who
    can see it (*Everyone with access* or *Only me*).
  - It shows the matching API call:
    `POST /api/library` with `file`, `kind` and `visibility`.
  - Agents use the MCP tool `push_file`.
  - Errors name the problem and how to fix it.
- **Send to device** opens a menu of the send targets in the library's config.
  - Each target shows its name and type. Disabled targets are greyed out, for
    example *reTerminal Sticky, not enabled yet*.
  - A footer says that sending publishes an event and culture-rules delivers
    it, and that agents use the MCP tool `send_to_device`.
  - Choosing a target publishes `library.item.send_requested`. The page then
    shows a "Sent to …" toast and a line in the reader: *Sent to x3-ori at
    14:05. culture-rules takes it from here.*
  - The page says nothing about delivery, which happens outside this repo.
  - The button is disabled for items that are not in the library yet.
  - Escape or a click outside closes the menu.
- **Hebrew and mixed-direction text** (claim c39) gets `dir=auto` on titles,
  cards, list rows, search snippets and reader paragraphs.
  - The stylesheet uses logical properties (`margin-inline`,
    `border-inline-start`, `padding-inline`), so right-to-left content lays
    out correctly.
  - The prototype only had left-to-right content, so the reference
    screenshots don't show this.
- **Content is untrusted** (claim c38): the reader renders extracted text,
  never markup or script taken from an item.
- **Not-yet-ingested items** are labeled *Arriving*. Private items are labeled
  *Only me*.
- **Per-browser state** (theme, last read item, pane state, reading position)
  lives in `localStorage` and is wrapped in try/catch. Shared state never
  lives there.
