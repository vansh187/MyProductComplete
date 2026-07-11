"""
Entrypoint for the margin engine's EOD sweep (see
FnO_Margin_Engine_Design.md section 6 / service/marginengine/eod_sweep_service.py).

Not wired to a scheduler - this repo has no job-scheduling infrastructure
today. Run manually after market close, or wire up under whatever cron/task
runner the team adopts next:

    python scripts/run_margin_eod_sweep.py
"""

import logging
import sys

from service.marginengine.eod_sweep_service import EodMarginSweepService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> int:
    try:
        result = EodMarginSweepService().run_daily_sweep()
        logger.info(f"Margin EOD sweep complete: {result}")
        return 0 if result.get("errors", 0) == 0 else 1
    except Exception as ex:
        logger.error(f"Margin EOD sweep failed: {str(ex)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
