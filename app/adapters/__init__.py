"""
Adapters — the ONLY part of this app that is client-specific.

Today TU Delft buys from Sligro, so `sligro.py` reads Sligro's export. Tomorrow a client
may buy from Bidfood or Hanos, or send a spreadsheet of their own shape. Each of those is
one new file in this folder. Nothing else changes — not the checks, not the dashboard, and
certainly not the shared catalogue.

An adapter's whole job is: *their* file in, standard purchase lines out.

    name      short identifier, stored against every upload
    label     what a human calls it
    detect()  does this file look like mine?
    read()    -> Reading

After that the app is supplier-agnostic. The catalogue is keyed on barcode, so the same
mozzarella bought from a different wholesaler lands on the product we already resolved —
a new supplier costs an adapter and nothing else.

To add a client format: write the module, implement the four names, register it below.
Put it BEFORE `mist_template` only if its detection is more specific.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import mist_template, sligro


class AdapterError(Exception):
    """The file cannot be read at all. Not a verdict — a failure to open the envelope."""


@dataclass
class Reading:
    """What every adapter returns, whatever shape the file arrived in."""
    adapter: str
    filename: str
    products: dict                      # artikelnr -> product tuple
    lines: list                         # standard purchase-line tuples
    year: int | None = None
    row_problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    quirks: list = field(default_factory=list)   # supplier-specific findings for pre-flight
    # source_rows[i] is the spreadsheet row that produced lines[i], so a finding can point
    # at a row the user can actually open. Empty when an adapter cannot track it.
    source_rows: list = field(default_factory=list)

    @property
    def periods(self) -> list[str]:
        return [f"{y}-{m:02d}" for y, m in sorted({(l[0], l[1]) for l in self.lines})]


# most specific first: the template is identified by its 'periode' column
REGISTRY = [mist_template, sligro]


def detect(path: str, filename: str | None = None):
    """Pick the adapter for a file. -> module. Raises AdapterError if none claims it."""
    for mod in REGISTRY:
        try:
            if mod.detect(path, filename):
                return mod
        except Exception:
            continue
    raise AdapterError(
        "no adapter recognises this file. It should be a Sligro export or the MiSt "
        "template — download the template from the upload page if you are unsure.")


def read(path: str, filename: str | None = None, year: int | None = None) -> Reading:
    """Read any supported file into standard purchase lines."""
    mod = detect(path, filename)
    return mod.read(path, filename, year)


def listing() -> list[dict]:
    return [dict(name=m.NAME, label=m.LABEL, description=m.DESCRIPTION) for m in REGISTRY]
