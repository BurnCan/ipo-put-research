import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import engine
from app.services.schema_upgrade import upgrade_milestone_2

if __name__ == "__main__":
    changed = upgrade_milestone_2(engine)
    print({"columns_added": changed, "changed": bool(changed)})
