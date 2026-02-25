import logging
import sys

from pocketkid import create_app


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("pocketkid")


def main() -> None:
    try:
        app = create_app()
    except Exception:
        logger.exception("Startup failed")
        sys.exit(1)

    try:
        app.run(host="0.0.0.0", port=8000, debug=False)
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        sys.exit(130)
    except Exception:
        logger.exception("Server stopped due to an unexpected error")
        sys.exit(1)


if __name__ == "__main__":
    main()
