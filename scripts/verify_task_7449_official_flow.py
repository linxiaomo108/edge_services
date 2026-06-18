from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_task_7904_official_flow import main as official_flow_main  # noqa: E402


def main() -> int:
    src_dir = ROOT / "id=7449"
    work_dir = ROOT / "id=7449_official_flow_out"
    log_file = work_dir / "verify_task_7449_official_flow.latest.log"
    defaults = [
        "--src-dir",
        str(src_dir),
        "--work-dir",
        str(work_dir),
        "--output-name",
        "teacher_7449.official-flow.mp4",
        "--log-file",
        str(log_file),
    ]
    sys.argv = [sys.argv[0], *defaults, *sys.argv[1:]]
    return official_flow_main()


if __name__ == "__main__":
    raise SystemExit(main())
