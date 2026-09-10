"""Publish local research review, archive notes, or probe an explicit account export."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.research_workbench_cli import main


if __name__=='__main__':
    main()
