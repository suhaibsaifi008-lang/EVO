import os
import sys
from pathlib import Path

os.environ["JARVIS_AGENT_MODE"] = "0"

# Keep tests hermetic regardless of the developer's real .env:
# point the primary core at a dead port and disable the Ollama fallback
# so nothing accidentally reaches a live model.
os.environ["JARVIS_OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ["JARVIS_OLLAMA_ENABLED"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
