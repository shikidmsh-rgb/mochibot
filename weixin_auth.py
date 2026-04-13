"""WeChat QR login script — run once to obtain bot credentials.

Usage:
    python weixin_auth.py

Flow:
    1. Fetches QR code from WeChat iLink API
    2. Displays QR as image (or prints URL as fallback)
    3. User scans with WeChat mobile app
    4. Long-polls until login confirmed
    5. Prints credentials to copy into .env
"""

import asyncio
import json
import os
import struct
import sys
import base64

try:
    import aiohttp
except ImportError:
    print("Error: aiohttp is required. Install it: pip install aiohttp")
    sys.exit(1)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_BOT_TYPE = "3"
QR_POLL_INTERVAL_S = 1
QR_TIMEOUT_S = 480  # 8 minutes
QR_MAX_REFRESH = 6


def _random_wechat_uin() -> str:
    """Generate X-WECHAT-UIN header: random uint32 -> decimal string -> base64."""
    uint32 = struct.unpack(">I", os.urandom(4))[0]
    return base64.b64encode(str(uint32).encode()).decode()


async def _fetch_qr(session: aiohttp.ClientSession, base_url: str) -> dict:
    """GET /ilink/bot/get_bot_qrcode -> {qrcode, qrcode_img_content}."""
    url = f"{base_url.rstrip('/')}/ilink/bot/get_bot_qrcode?bot_type={DEFAULT_BOT_TYPE}"
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _poll_status(session: aiohttp.ClientSession, base_url: str,
                       qrcode: str) -> dict:
    """GET /ilink/bot/get_qrcode_status -> {status, bot_token, ...}."""
    url = f"{base_url.rstrip('/')}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
    headers = {"iLink-App-ClientVersion": "1"}
    async with session.get(
        url, headers=headers,
        timeout=aiohttp.ClientTimeout(total=35),
    ) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


def _print_qr(qr_url: str) -> None:
    """Generate QR code as image file and open it."""
    print(f"\n  QR data URL: {qr_url}\n")

    try:
        import qrcode as qrcode_lib  # type: ignore[import-untyped]
        import tempfile
        import subprocess

        qr = qrcode_lib.QRCode(version=1, box_size=10, border=4)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        qr_path = os.path.join(tempfile.gettempdir(), "weixin_login_qr.png")
        img.save(qr_path)
        print(f"  QR code saved to: {qr_path}")

        # Auto-open the image
        try:
            if sys.platform == "win32":
                subprocess.Popen(["start", "", qr_path], shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", qr_path])
            else:
                subprocess.Popen(["xdg-open", qr_path])
            print("  QR image opened — scan it with WeChat!\n")
        except Exception:
            print("  Please open the file above and scan with WeChat.\n")
    except ImportError:
        print("  (pip install qrcode[pil] to auto-generate QR image)")
        print("  Copy the URL above and convert it to a QR code manually.\n")


async def login(base_url: str | None = None) -> None:
    """Run the interactive QR login flow."""
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    print(f"\n  WeChat iLink API: {base}")
    print("Fetching QR code...\n")

    async with aiohttp.ClientSession() as session:
        qr_data = await _fetch_qr(session, base)
        qrcode_id = qr_data.get("qrcode", "")
        qr_url = qr_data.get("qrcode_img_content", "")

        if not qrcode_id or not qr_url:
            print("  Failed to get QR code from API")
            print(f"Response: {json.dumps(qr_data, indent=2)}")
            return

        _print_qr(qr_url)
        print("  Scan the QR code above with WeChat, then confirm on your phone.\n")

        scanned_printed = False
        refresh_count = 1
        deadline = asyncio.get_event_loop().time() + QR_TIMEOUT_S

        while asyncio.get_event_loop().time() < deadline:
            try:
                status_data = await _poll_status(session, base, qrcode_id)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"  Poll error: {e}")
                return

            status = status_data.get("status", "")

            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned" and not scanned_printed:
                print("\n  Scanned! Confirm on your phone...")
                scanned_printed = True
            elif status == "expired":
                refresh_count += 1
                if refresh_count > QR_MAX_REFRESH:
                    print("\n  QR expired too many times. Please restart.")
                    return
                print(f"\n  QR expired, refreshing... ({refresh_count}/{QR_MAX_REFRESH})")
                qr_data = await _fetch_qr(session, base)
                qrcode_id = qr_data.get("qrcode", "")
                qr_url = qr_data.get("qrcode_img_content", "")
                _print_qr(qr_url)
                scanned_printed = False
            elif status == "confirmed":
                bot_token = status_data.get("bot_token", "")
                bot_id = status_data.get("ilink_bot_id", "")
                bot_base = status_data.get("baseurl", base)
                user_id = status_data.get("ilink_user_id", "")

                print("\n\n  Login successful!\n")
                print("Add these to your .env:\n")
                print(f"WEIXIN_ENABLED=true")
                print(f"WEIXIN_BOT_TOKEN={bot_token}")
                if bot_base and bot_base != DEFAULT_BASE_URL:
                    print(f"WEIXIN_BASE_URL={bot_base}")
                if user_id:
                    print(f"WEIXIN_ALLOWED_USERS={user_id}")
                print(f"\n# Bot ID: {bot_id}")
                print(f"# User ID: {user_id}")
                return

            await asyncio.sleep(QR_POLL_INTERVAL_S)

        print("\n  Login timed out. Please try again.")


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(login(base_url))


if __name__ == "__main__":
    main()
