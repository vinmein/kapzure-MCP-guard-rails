import argparse
import logging

import uvicorn

from .app import create_app
from .config import load_settings


def main():
    parser = argparse.ArgumentParser(description="Run the MCP guardrail gateway")
    parser.add_argument("--config", default="policy.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # HTTP client INFO logs can include an operator-configured upstream URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(load_settings(args.config)), host=args.host, port=args.port,
                access_log=False)


if __name__ == "__main__":
    main()
