"""python -m app  ->  dashboard on http://localhost:8765 (PRINTER_DASH_PORT / PRINTER_DASH_HOST to change)."""
import logging
import os

import uvicorn

from .web import create_app

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("PRINTER_DASH_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=os.environ.get("PRINTER_DASH_HOST", "0.0.0.0"),
                port=int(os.environ.get("PRINTER_DASH_PORT", "8765")), log_level="warning")
