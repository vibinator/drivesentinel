"""DriveSentinel entry point."""
import logging
import os
import threading
import time

import mock
import smart
from config import Config
from db import DB
from monitor import Monitor
from server import serve


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("main")
    cfg, db = Config(), DB()
    use_mock = os.environ.get("MOCK", "").lower() in ("1", "true", "yes")
    backend = mock if use_mock else smart
    mon = Monitor(cfg, db, backend)

    if use_mock:
        t0 = db.kv_get("mock_t0")
        if t0 is None:
            t0 = time.time() - 30 * 86400
            db.kv_set("mock_t0", t0)
            mock.T0 = t0
            log.info("Mock mode: backfilling 30 days of simulated history")
            now = time.time()
            last_bench = 0
            for ts in range(int(t0), int(now) - 3600, 3 * 3600):
                mon.scan(now=ts, quiet=True)
                if ts - last_bench >= 7 * 86400:
                    last_bench = ts
                    for d in db.list_drives():
                        mon.run_benchmark(d["serial"], now=ts, quiet=True)
        mock.T0 = t0

    port = int(os.environ.get("PORT", "8090"))
    threading.Thread(target=mon.loop, daemon=True, name="monitor").start()
    log.info("DriveSentinel listening on http://0.0.0.0:%d (mock=%s)", port, use_mock)
    serve({"cfg": cfg, "db": db, "monitor": mon}, port=port)


if __name__ == "__main__":
    main()
