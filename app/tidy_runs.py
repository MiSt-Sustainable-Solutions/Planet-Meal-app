"""
One-off tidy: collapse the duplicate analysis runs the old dashboard left behind.

Before caching, every page load recalculated and saved a row. Thirty-six identical runs of
FY2025 in a row is not history, it is noise — and it makes the History page useless for
the thing it is actually for: seeing what was reported, and when.

For each distinct answer it keeps two rows and drops the rest:

  * the FIRST, because that is when the answer was genuinely first produced;
  * the LAST, because that is the row the cache is currently serving from — it carries the
    window key, catalogue version and data fingerprint that make it valid. Delete it and
    the next page load recalculates for no reason.

Two runs with different numbers are never touched, whatever produced them.

    python tidy_runs.py            # show what would go
    python tidy_runs.py --apply    # do it

It deletes rows from your own database. It prints everything first, and it is not wired
into anything — it has to be run on purpose.
"""
from __future__ import annotations

import sys

import db

# What makes two runs "the same answer". Deliberately the reported figures rather than the
# result blob: two runs with identical headlines are the same answer even if a caveat was
# worded differently.
SAME = ("tenant", "label", "period_from", "period_to", "eat_profile",
        "lines", "food_kg", "co2_kg", "intensity", "eat_score", "specific_pct")


def main(apply: bool) -> int:
    db.init()
    con = db.connect()
    rows = con.execute(
        f"SELECT id, ran_at, {', '.join(SAME)} FROM analysis_run "
        "ORDER BY ran_at ASC, id ASC").fetchall()

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault(tuple(r[c] for c in SAME), []).append(r)

    doomed, report = [], []
    for key, rs in groups.items():
        if len(rs) < 3:                      # first and last already, nothing in between
            continue
        doomed.extend(rs[1:-1])
        report.append((key, rs))

    print(f"{len(rows):,} saved runs, {len(groups):,} distinct answers, "
          f"{len(doomed):,} redundant middles.\n")
    for key, rs in sorted(report, key=lambda kv: -len(kv[1])):
        print(f"  {key[0]} · {key[1]} · {key[7]:,.0f} kg CO2e   ({len(rs)} runs)")
        print(f"      keeping first  {rs[0]['id']}  {rs[0]['ran_at']}")
        print(f"      keeping last   {rs[-1]['id']}  {rs[-1]['ran_at']}  (the cache row)")
        print(f"      dropping {len(rs) - 2} in between, all saying the same thing")

    if not doomed:
        print("nothing to tidy.")
        con.close()
        return 0
    if not apply:
        print("\nnothing changed. run again with --apply to delete them.")
        con.close()
        return 0

    con.executemany("DELETE FROM analysis_run WHERE id=?", [(r["id"],) for r in doomed])
    con.commit()
    left = con.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]
    con.close()
    print(f"\ndeleted {len(doomed):,}. {left:,} runs left — the first and the last of each.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
