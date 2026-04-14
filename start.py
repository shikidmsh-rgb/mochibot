"""MochiBot launcher with automatic restart support.

Usage:
    python start.py

When the bot requests a restart (e.g. via the admin portal's restart button),
it exits with code 42. This wrapper detects that and relaunches automatically.

For Docker or systemd deployments, use ``python -m mochi.main`` directly —
those environments already handle process restarts.
"""

import subprocess
import sys
import time

# Must match mochi.shutdown.RESTART_EXIT_CODE (mochi/shutdown.py:10)
_RESTART_EXIT_CODE = 42


def main():
    while True:
        result = subprocess.run([sys.executable, "-m", "mochi.main"])
        if result.returncode == _RESTART_EXIT_CODE:
            print()
            print("  [start.py] Restart requested — restarting in 2s...")
            print()
            time.sleep(2)
            continue
        sys.exit(result.returncode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
