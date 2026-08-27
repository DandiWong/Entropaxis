import sys
from pathlib import Path

# Make .system/ importable so tests can do: from tools.xxx import yyy
sys.path.insert(0, str(Path(__file__).resolve().parent))
