"""Publish local research review, archive notes, or probe an explicit account export."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
if len(sys.argv)>1 and sys.argv[1] in ('build','update','serve','status'):
    from trade_system.v2.research_product import main
else:
    from trade_system.v2.research_workbench_cli import main


if __name__=='__main__':
    main()
