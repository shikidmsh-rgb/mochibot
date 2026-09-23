"""Launch Main; handle ordinary restarts and prepared stable-release updates."""

import argparse
import os
import subprocess
import sys
import time

_RESTART_EXIT_CODE = 42
_UPDATE_EXIT_CODE = 44


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    child_env = dict(os.environ, MOCHIBOT_UPDATE_LAUNCHER="1")
    open_browser = args.open_browser
    while True:
        command = [sys.executable, "-m", "mochi.main"]
        if open_browser:
            command.append("--open-browser")
            open_browser = False
        result = subprocess.run(command, env=child_env)
        if result.returncode == _UPDATE_EXIT_CODE:
            update = subprocess.run(
                [sys.executable, "-m", "mochi.update_service"], env=child_env,
            )
            if update.returncode:
                print(f"[start.py] Update helper exited with code {update.returncode}.", flush=True)
            continue
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
