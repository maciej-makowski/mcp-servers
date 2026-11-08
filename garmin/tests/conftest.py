"""Make garmin/ (server.py, garmin_mcp/) importable no matter where pytest runs."""
import sys
from pathlib import Path

GARMIN_DIR = Path(__file__).resolve().parent.parent
if str(GARMIN_DIR) not in sys.path:
    sys.path.insert(0, str(GARMIN_DIR))
