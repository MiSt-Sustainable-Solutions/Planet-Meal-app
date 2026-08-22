# Start here

Two processes. Open two terminals.

**1 — the catalogue** (the shared engine; holds no client data)

```bash
cd "../MiSt Tool Mrigank/catalogue/src"
python -m uvicorn api:app --port 8077
```

**2 — the app** (this repo; owns TU Delft's data and the screens)

```bash
cd app
python -m uvicorn main:app --port 8080 --reload
```

Then open **<http://127.0.0.1:8080>**

The database is already seeded with TU Delft's 1.5 years, so the dashboard has real numbers
immediately. If it is ever empty, run `python app/seed.py` once.

---

## What to look at, in order

1. **`/`** — the dashboard. Headline, the confidence bar, the caveats, the trend, restaurants,
   food groups, top contributors with the tier that produced each number, EAT-Lancet.
2. **`/data-health`** — the differentiator. The tier ladder, the work queue ranked by
   kilograms, and the 380 products that weigh zero.
3. **`/upload`** — drag in `December 2025.xlsx` from the catalogue repo's
   `catalogue/data_in/sligro/Sligro afname TU Delft/`. It will be **blocked**, correctly:
   every month in it is already loaded, and importing it would double-count. That is the
   guard working, not a failure.
4. **`/history`** — every analysis run, kept with the EAT profile that produced it.

---

## To show a clean upload instead of a blocked one

The blocked verdict is right but makes a poor demo. Two options:

**Start empty**, so nothing can overlap:

```bash
MIST_APP_DB=data/demo.db python app/seed.py --from /dev/null 2>/dev/null || true
MIST_APP_DB=data/demo.db python -m uvicorn app.main:app --port 8081
```
then upload `Augustus 2024.xlsx` — it goes green.

**Or** upload a file covering months you do not hold, and use *Only the new months*.

---

## Tests

```bash
cd app
python test_mass.py      # the mass formula, and that both repos agree on it
python test_app.py       # adapters, pre-flight, the double-count guard, scoring
python test_design.py    # design-system compliance (app must be running)
```

And in the catalogue repo:

```bash
cd "../MiSt Tool Mrigank/catalogue/src"
python test_api.py       # pins FY2025 to 194,389 / 329,288 / 1.694 / 0.695
python test_edge.py      # 13 robustness tests (needs the API running)
```
