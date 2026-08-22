# PLANETprocure — the app

A caterer drops in a purchase file and gets a validated **CO₂ + EAT-Lancet** dashboard, with
an admin view of how good the underlying data actually is.

This is one of two repos. It owns **the client**. The other one owns **the catalogue**.

```
   ┌──────────────────────────────┐         ┌────────────────────────────────┐
   │  THIS REPO — the app         │  HTTPS  │  MiSt Tool Mrigank — catalogue │
   │                              │ ──────► │                                │
   │  users · tenants             │  lines  │  NEVO · RIVM · Open Food Facts │
   │  format adapters             │  in,    │  the rules: pins, archetypes,  │
   │  uploads + pre-flight        │  scores │    routes, category maps       │
   │  the client's purchase lines │  out    │  the footprint + EAT maths     │
   │  saved analyses              │         │  provenance for every number   │
   │  the screens                 │         │                                │
   │                              │         │  HOLDS NO CLIENT DATA          │
   └──────────────────────────────┘         └────────────────────────────────┘
```

**Why split this way.** The catalogue is a shared asset that gets better with every client;
the app is one client's packaging of it. A second client is a tenant, not a rebuild. And
because the catalogue never sees whose lines it is scoring, the shared thing holds nothing
sensitive.

---

## Run it

Two processes. The catalogue first:

```bash
cd "../MiSt Tool Mrigank/catalogue/src" && python -m uvicorn api:app --port 8077
```

Then this app:

```bash
pip install -r requirements.txt
python app/seed.py                      # first time only — see below
cd app && python -m uvicorn main:app --port 8080 --reload
```

<http://127.0.0.1:8080>

If the catalogue is not running the app still starts. You can upload and validate; the pages
that need scoring say so rather than failing.

### Seeding

TU Delft's 1.5 years of history was parsed before this app existed and lives in the catalogue
repo's `analysis.db`. That is client data, so its home is here. `app/seed.py` moves it across
once. It is idempotent — re-running replaces the seeded rows rather than doubling them, which
matters because doubling purchase lines is the exact failure this product exists to prevent.

---

## Configuration

Copy `.env.example`, or just set the variables. Local and Railway differ by configuration
only.

| Variable | Default | What it does |
|---|---|---|
| `MIST_CATALOGUE_API` | `http://127.0.0.1:8077` | where the shared catalogue lives |
| `MIST_APP_DB` | `data/planetprocure.db` | this app's database |
| `MIST_TENANT` | `tudelft` | which client this instance serves |
| `MIST_CLIENT_NAME` | `TU Delft` | shown in the UI |

---

## Adapters — the only client-specific code

Today TU Delft buys from Sligro. Tomorrow a client may buy from Bidfood or Hanos, or send a
spreadsheet of their own shape. Each is **one new file** in `app/adapters/`:

```
adapters/
  sligro.py         cumulative year-to-date, year from the filename, positional columns
  mist_template.py  our standard sheet, for anyone not on a supported export
```

An adapter's whole job is *their* file in, standard purchase lines out. After that the app is
supplier-agnostic. And because the catalogue is keyed on **barcode**, the same mozzarella
bought from a different wholesaler lands on a decision already made — a new supplier costs an
adapter and nothing else.

To add one: implement `NAME`, `LABEL`, `DESCRIPTION`, `detect(path, filename)` and
`read(path, filename, year)`, then register it in `adapters/__init__.py`.

---

## The three things that will bite you

**1. Mass is `Aantal × IVP × Maat × unit_factor(Eenh)`.** IVP *must* be multiplied. Two
packaging conventions coexist and this one formula covers both — `IVP=1` means Maat is the
whole case, `IVP>1` means it is per unit. Getting it wrong moves every number by ~45%,
silently. `app/test_mass.py` pins it to the supplier's own worked examples **and** checks it
against the catalogue repo's copy.

**2. Sligro exports are cumulative year-to-date, and the last column pair is a running total,
not a month.** Two consequences: that pair is always dropped, and a December file re-supplies
January through November. Loading it on top of months you already hold counts them twice.
That is the worst bug available in this project, so the pre-flight **blocks** on overlap and
makes you choose `new_only` or `replace`.

**3. Per-piece items have no weight.** About 7% of TU Delft's spend. They contribute zero kg
and zero CO₂e, and only the wholesaler can close that. Never let it pass silently — it is
disclosed on every screen that shows a total.

---

## The pre-flight

A file is read, judged and parked. Nothing enters the client's data until someone reads the
verdict: **go**, **go with warnings**, or **blocked** (the worst finding decides).

Checks split by who can answer them:

| Answered by the app's own data | Answered by the catalogue |
|---|---|
| overlap — do we already hold these months? | is this product known? |
| volume — is this a plausible month for this client? | is this category mapped? |
| weights, barcodes, duplicate rows | how well will a new product resolve? |

The catalogue-dependent checks degrade to *unavailable* if the API is unreachable, rather
than silently passing.

`volume` measures each month against **this client's own median complete month**, because a
truncated file judged against itself looks perfectly consistent. July and August are exempt
from the softer band — a Dutch university summer is genuinely quiet, not broken.

An adapter cannot know whether a month is complete, so it does not guess: the pre-flight
judges, and `commit` records that judgement against the months it applies to.

---

## Scope discipline

**CO₂ and EAT-Lancet only. Nutrition is out of scope.** The engine computes it; roughly half
would be group averages, and publishing weak numbers beside strong ones damages trust in
both. `test_design.py` asserts no nutrition reaches any page.

**Never present a number without its confidence.** Every response carries its caveats, and
the dashboard renders them. That honesty is the product — the incumbent cannot say what share
of their number is a real match versus a group average, and we can, per product.

---

## Design

Follows `docs/design/MiSt-design-system.md` in the catalogue repo. Tokens, type scale, nav
and footer are copied, not re-implemented — which is the only way rules like *gold is for
buttons only, heading italics are teal* survive contact with a component tree.

`app/test_design.py` checks this automatically: the tokens are unaltered, no CSS rule paints
text gold, no raw hex appears inline, sections alternate light and dark, CO₂ is always a
subscript, and no page says "B.V.".

Charts are hand-rolled inline SVG (`app/charts.py`) — no external script, works offline, and
the palette is the design system's own tokens so a chart cannot drift from the brand. If the
dashboard ever needs real interactivity, swap in ECharts; the API returns clean JSON, so that
is a front-end change and nothing else.

---

## Tests

```bash
cd app
python test_mass.py      # the mass formula, and that both repos agree on it
python test_app.py       # adapters, pre-flight, the double-count guard, scoring
python test_design.py    # design-system compliance   (needs the app running)
```

`test_app.py` runs against disposable copies and never touches the real database.

---

## Screens

| Route | What it is |
|---|---|
| `/` | dashboard — headline, confidence, caveats, trend, restaurants, food groups, top contributors, EAT-Lancet |
| `/upload` | drag and drop, plus the template and the list of accepted formats |
| `/upload/{id}` | the pre-flight report and the import decision |
| `/data-health` | the tier ladder, the work queue ranked by kilograms, the zero-weight products, coverage by month |
| `/history` | every analysis run and every file received |
| `/export.xlsx` | the full analysis as a workbook |
| `/api/analysis` | the same data as JSON |

---

## Still to do

- **Accounts.** Deliberately last — there is no point logging into something not worth seeing.
- **Postgres.** The schema is written plainly so it ports: no SQLite-only types, explicit
  primary keys. `MIST_APP_DB` becomes a connection URL.
- **Google Drive backup**, weekly or fortnightly.
- **Editing the rule tables** from the data-health page. Today they are viewable through the
  catalogue API and editable only in the catalogue repo.
