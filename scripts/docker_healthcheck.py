"""Docker HEALTHCHECK: exit 0 once the API answers /api/config on ACTIVITY_PORT."""

import os
import sys
import urllib.request


def main() -> int:
    url = f"http://127.0.0.1:{os.environ.get('ACTIVITY_PORT') or '8080'}/api/config"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except OSError as e:
        print(f"{url}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
