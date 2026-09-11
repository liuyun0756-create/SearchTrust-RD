import os
from pathlib import Path
import sys

from hypothesis import HealthCheck, settings


TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))


# Set before test modules import application settings. Network access is separately
# denied by pytest-socket, so configured provider credentials can never reach a live API.
os.environ.setdefault("SEARCHTRUST_TESTING", "1")


settings.register_profile(
    "v22_local",
    max_examples=40,
    deadline=750,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "v22_ci",
    max_examples=80,
    deadline=1_500,
    database=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("v22_ci" if os.environ.get("CI") else "v22_local")
