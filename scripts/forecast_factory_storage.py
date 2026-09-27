"""Print 10k/100k/1M/12M factory storage forecasts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from app.database.postgres import SessionLocal
from app.services.factory_storage_service import forecast_factory_storage


def main() -> None:
    with SessionLocal() as session:
        result = forecast_factory_storage(session)
        session.rollback()
    raw_root = Path(os.environ.get("FIRMOTEKA_RAW_ROOT") or "/tmp")
    usage = shutil.disk_usage(raw_root if raw_root.exists() else raw_root.parent)
    result["disk"] = {
        "path": str(raw_root),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
    }
    for forecast in result["forecasts"].values():
        forecast["fits_current_free_space"] = forecast["total_bytes"] <= usage.free
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
