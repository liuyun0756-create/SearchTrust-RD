import os
from pathlib import Path
import sys


TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))


# Set before test modules import application settings. Network access is separately
# denied by pytest-socket, so configured provider credentials can never reach a live API.
os.environ.setdefault("SEARCHTRUST_TESTING", "1")
