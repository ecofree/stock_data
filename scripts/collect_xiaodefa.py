"""Run the retained provider's explicit collection use case."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collectors.xiaodefa import main

if __name__ == "__main__":
    raise SystemExit(main())
