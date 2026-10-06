import sys
from pathlib import Path

# Allow `pytest` to run from the project root without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
