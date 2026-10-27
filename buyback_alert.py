#!/usr/bin/env python3
"""
Buyback news -> Telegram alert bot (Indonesian stocks)

Commands:
  python buyback_alert.py --setup       one-time: saves your bot token + chat id to .env
  python buyback_alert.py --test        sends a test message to your Telegram
  python buyback_alert.py --demo        sends up to 3 recent real buyback articles (full pipeline test)
  python buyback_alert.py               runs forever, scanning every 2 minutes
  python buyback_alert.py --install     makes it auto-start on boot/login (Windows/Mac/Linux)
  python buyback_alert.py --uninstall   removes auto-start
  python buyback_alert.py --once        scan one time and exit

Needs:  pip install requests feedparser
"""

import argparse
import hashlib
import html
import json
import logging
import os
import platform
import plistlib
import re
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests

SCRIPT = Path(__file__).resolve()
BASE_DIR = SCRIPT.parent
ENV_FILE = BASE_DIR / ".env"
SEEN_FILE = BASE_DIR / "seen.json"
LOG_FILE = BASE_DIR / "buyback.log"
DEFAULT_INTERVAL = 120  # seconds -> scan every 2 minutes
LOCK_PORT = 47653       # used to stop two copies running at once (= duplicate alerts)

# ---- Keywords: article must match at least one ----------------------------
KEYWORD_RE = re.compile(
    "|".join(
        [
            r"\bbuy\s*-?back\b",
            r"\bbuyback\b",
            r"pembelian kembali saham",
            r"pembelian kembali",
            r"beli kembali saham",
        ]
    ),
    re.IGNORECASE,
)

# ---- Must also look Indonesian-stock related (cuts noise from US buybacks) -
CONTEXT_RE = re.compile(
    r"\b(saham|emiten|IHSG|BEI|IDX|OJK|Tbk|bursa|RUPS|dana buyback)\b",
    re.IGNORECASE,
)

# ---- Sources ---------------------------------------------------------------
GOOGLE_QUERIES = [
    "buyback saham",
    "pembelian kembali saham",
    "buyback emiten Tbk",
    "buyback IHSG",
    "OJK buyback tanpa RUPS",
]

DIRECT_FEEDS = [
    "https://www.cnbcindonesia.com/market/rss",
    "https://www.cnbcindonesia.com/news/rss",
    # Add more RSS feeds here (test them first, sites change RSS URLs)
]


def google_news_url(q: str) -> str:
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(q + " when:2d")
        + "&hl=id&gl=ID&ceid=ID:id"
    )


FEEDS = [google_news_url(q) for q in GOOGLE_QUERIES] + DIRECT_FEEDS

# ---- Logging (file, so it works even when running hidden in background) ----
log = logging.getLogger("buyback")
log.setLevel(logging.INFO)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=2, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)
if sys.stdout is not None:
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(_sh)


# ---- Config ----------------------------------------------------------------
def load_env() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    # real environment variables override the file
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = load_env()


def send_telegram(text: str) -> bool:
    token = CFG.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = CFG.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.error("Missing token/chat id. Run: python buyback_alert.py --setup")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if not r.ok:
            log.error("Telegram error %s: %s", r.status_code, r.text)
        return r.ok
    except Exception as ex:
        log.error("Telegram send failed: %s", ex)
        return False


# ---- Scanning --------------------------------------------------------------
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)[-5000:]))


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_title_key(title: str) -> str:
    # same story appears on many sites; dedupe on normalized title
    t = re.sub(r"\s*[-–|]\s*[^-–|]+$", "", title.lower())  # strip " - Source"
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return hashlib.md5(t.encode()).hexdigest()


def matches(title: str, summary: str) -> bool:
    blob = f"{title} {summary}"
    return bool(KEYWORD_RE.search(blob)) and bool(CONTEXT_RE.search(blob))


def fetch_entries():
    for url in FEEDS:
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            feed = feedparser.parse(resp.content)
            for e in feed.entries:
                yield e
        except Exception as ex:
            log.warning("Feed failed %s: %s", url[:80], ex)


def run_once() -> None:
    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    hits = []

    for e in fetch_entries():
        title = clean(e.get("title", ""))
        summary = clean(e.get("summary", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        key = norm_title_key(title)
        if key in seen:
            continue
        seen.add(key)
        if matches(title, summary):
            hits.append((title, link))

    if first_run:
        log.info("First run: marked %d existing articles as seen, no alerts sent.", len(seen))
        save_seen(seen)
        return

    for title, link in hits:
        ok = send_telegram(f"📢 <b>Buyback news</b>\n\n{html.escape(title)}\n\n{link}")
        if not ok:
            # don't mark as seen if sending failed, so it retries next scan
            seen.discard(norm_title_key(title))
        time.sleep(1)
    log.info("Scan done. %d new alert(s).", len(hits))
    save_seen(seen)


def run_demo(limit: int = 3) -> None:
    """Send up to `limit` recent matching articles, ignoring seen.json. Proves the whole pipeline works."""
    found, dupes = [], set()
    scanned = 0
    for e in fetch_entries():
        scanned += 1
        title = clean(e.get("title", ""))
        link = e.get("link", "")
        key = norm_title_key(title) if title else None
        if not title or not link or key in dupes:
            continue
        if matches(title, clean(e.get("summary", ""))):
            dupes.add(key)
            found.append((title, link))
    print(f"Scanned {scanned} articles, {len(found)} matched the buyback filter.")
    if not found:
        print("No buyback articles in the last ~2 days. Feeds work, there's just nothing to match right now.")
        return
    for title, link in found[:limit]:
        send_telegram(f"🧪 <b>[DEMO] Buyback news</b>\n\n{html.escape(title)}\n\n{link}")
        time.sleep(1)
    print(f"Sent {min(limit, len(found))} demo alert(s) to Telegram.")


def acquire_single_instance_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log.error("Already running (another copy holds the lock). Exiting.")
        sys.exit(0)
    return s  # keep reference alive for the life of the process


def run_forever(interval: int) -> None:
    _lock = acquire_single_instance_lock()  # noqa: F841
    log.info("Started. Scanning every %d seconds.", interval)
    while True:
        try:
            run_once()
        except Exception as ex:  # never let one bad scan kill the bot
            log.exception("Scan crashed, will retry: %s", ex)
        time.sleep(interval)


# ---- Setup / test ----------------------------------------------------------
def setup() -> None:
    token = input("Paste your bot token from @BotFather: ").strip()
    input("Now open your bot in Telegram, send it any message (e.g. 'hi'), then press Enter here... ")
    chat_id = None
    try:
        data = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
        for u in reversed(data.get("result", [])):
            msg = u.get("message") or u.get("channel_post") or {}
            if msg.get("chat", {}).get("id"):
                chat_id = str(msg["chat"]["id"])
                break
    except Exception as ex:
        print("Couldn't reach Telegram:", ex)
    if not chat_id:
        chat_id = input("Couldn't auto-detect chat id. Enter it manually: ").strip()
    ENV_FILE.write_text(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        pass
    print(f"Saved to {ENV_FILE}")
    CFG.update({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    print("Sending test message..." , "OK ✅" if send_telegram("✅ Buyback alert bot connected.") else "FAILED ❌ (check token/chat id)")


# ---- Auto-start installer --------------------------------------------------
def install_autostart() -> None:
    if not (CFG.get("TELEGRAM_BOT_TOKEN") and CFG.get("TELEGRAM_CHAT_ID")):
        sys.exit("Run --setup first so the bot has your token and chat id.")

    system = platform.system()
    python = sys.executable

    if system == "Windows":
        pyw = Path(python).with_name("pythonw.exe")  # pythonw = no console window
        if pyw.exists():
            python = str(pyw)
        ps = f"""
$a = New-ScheduledTaskAction -Execute '{python}' -Argument '"{SCRIPT}"' -WorkingDirectory '{BASE_DIR}'
$t = New-ScheduledTaskTrigger -AtLogOn
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName 'BuybackAlert' -Action $a -Trigger $t -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName 'BuybackAlert'
"""
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print("Installed. Starts automatically every time you log in to Windows (and is running now).")

    elif system == "Darwin":  # macOS
        plist_path = Path.home() / "Library/LaunchAgents/com.buyback.alert.plist"
        plist = {
            "Label": "com.buyback.alert",
            "ProgramArguments": [python, str(SCRIPT)],
            "WorkingDirectory": str(BASE_DIR),
            "RunAtLoad": True,
            "KeepAlive": True,
        }
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        print("Installed. Starts automatically at login (and is running now).")

    else:  # Linux
        line = f'@reboot cd "{BASE_DIR}" && "{python}" "{SCRIPT}" >/dev/null 2>&1'
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        kept = [l for l in cur.splitlines() if str(SCRIPT) not in l]
        subprocess.run(["crontab", "-"], input="\n".join(kept + [line]) + "\n", text=True, check=True)
        subprocess.Popen(
            [python, str(SCRIPT)], cwd=str(BASE_DIR), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("Installed. Starts automatically on every boot (and is running now).")


def uninstall_autostart() -> None:
    system = platform.system()
    if system == "Windows":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Stop-ScheduledTask -TaskName 'BuybackAlert' -ErrorAction SilentlyContinue; "
             "Unregister-ScheduledTask -TaskName 'BuybackAlert' -Confirm:$false"],
        )
    elif system == "Darwin":
        p = Path.home() / "Library/LaunchAgents/com.buyback.alert.plist"
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        p.unlink(missing_ok=True)
    else:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        kept = [l for l in cur.splitlines() if str(SCRIPT) not in l]
        subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True)
    print("Auto-start removed. (If the bot is still running, restart your device or stop the process.)")


# ---- Entry point -----------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=DEFAULT_INTERVAL, help="seconds between scans (default 120)")
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--demo", action="store_true", help="send recent matching articles to test the full pipeline")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    if args.setup:
        return setup()
    if args.test:
        print("OK ✅" if send_telegram("✅ Test message from buyback alert bot.") else "FAILED ❌")
        return
    if args.demo:
        return run_demo()
    if args.install:
        return install_autostart()
    if args.uninstall:
        return uninstall_autostart()
    if args.once:
        return run_once()

    run_forever(max(args.loop, 30))


if __name__ == "__main__":
    main()
