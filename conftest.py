import os
import sys
from pathlib import Path

os.environ["JARVIS_AGENT_MODE"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
