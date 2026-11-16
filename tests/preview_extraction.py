"""Print rule extraction output for every fixture (dev tool)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contract_extract.pipeline import extract_document
import glob

for f in sorted(glob.glob("fixtures/contracts/*")):
    r = extract_document(f)
    print("=" * 70)
    print(f"  {Path(f).name}  [{r.format}] risk={r.risk.value} pages={r.pages} n={len(r.clauses)}")
    for c in r.clauses:
        print(f"    - {c.clause:18s} p.{c.page:2d} {c.risk.value:6s} | {c.flag or ''}")
