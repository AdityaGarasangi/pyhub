import docker
import requests
import os
import sys
import time
import signal
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from threading import Lock, Thread
from typing import Optional

# ─── Timezone ─────────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))


# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("watchdog")


# ─── Config ───────────────────────────────────────────────────────────────────

def _csv(key: str, default: str = "") -> set:
    return set(filter(None, os.getenv(key, default).split(",")))


class Config:
    TOKEN             = os.getenv("TELEGRAM_WATCHDOG_TOKEN", "")
    CHAT_ID           = os.getenv("TELEGRAM_CHAT_ID", "")
    HOSTNAME          = os.getenv("HOSTNAME", os.uname()[1])

    # Container events:
    #   attach, commit, copy, create, destroy, detach, die, exec_create,
    #   exec_detach, exec_die, exec_start, export, health_status, kill, oom,
    #   pause, rename, resize, restart, start, stop, top, unpause, update
    WATCH_ACTIONS     = _csv("WATCH_ACTIONS", "start,die,stop,kill,oom,health_status")

    # Network events: connect, disconnect, create, destroy
    WATCH_NETWORK     = _csv("WATCH_NETWORK", "")

    # Volume events: create, destroy, mount, unmount
    WATCH_VOLUME      = _csv("WATCH_VOLUME", "")

    # Image events: delete, import, load, pull, push, save, tag, untag
    WATCH_IMAGE       = _csv("WATCH_IMAGE", "")

    IGNORE_CONTAINERS = _csv("IGNORE_CONTAINERS", "")
    WATCH_CONTAINERS  = _csv("WATCH_CONTAINERS", "")

    RATE_LIMIT_MAX    = int(os.getenv("RATE_LIMIT_MAX", "5"))
    RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

    RETRY_ATTEMPTS    = int(os.getenv("RETRY_ATTEMPTS", "3"))
    RETRY_BACKOFF     = float(os.getenv("RETRY_BACKOFF", "2.0"))

    NOTIFY_STARTUP    = os.getenv("NOTIFY_STARTUP", "true").lower() == "true"
    NOTIFY_SHUTDOWN   = os.getenv("NOTIFY_SHUTDOWN", "true").lower() == "true"

    @classmethod
    def validate(cls):
        errors = []
        if not cls.TOKEN:
            errors.append("TELEGRAM_WATCHDOG_TOKEN is required")
        if not cls.CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID is required")
        if errors:
            for e in errors:
                logger.critical("Config error: %s", e)
            sys.exit(1)
        logger.info(
            "Config loaded — host=%s | container=%s | network=%s | volume=%s | image=%s",
            cls.HOSTNAME, cls.WATCH_ACTIONS, cls.WATCH_NETWORK,
            cls.WATCH_VOLUME, cls.WATCH_IMAGE,
        )


# ─── Rate Limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, max_msgs: int, window: int):
        self.max_msgs = max_msgs
        self.window   = window
        self._buckets: dict = defaultdict(list)
        self._lock    = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._buckets[key] = [t for t in self._buckets[key] if now - t < self.window]
            if len(self._buckets[key]) >= self.max_msgs:
                return False
            self._buckets[key].append(now)
            return True


# ─── Telegram Sender ──────────────────────────────────────────────────────────

class TelegramSender:
    _SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
    _UPD_URL  = "https://api.telegram.org/bot{token}/getUpdates"

    def __init__(self, token: str, chat_id: str, attempts: int, backoff: float):
        self.token    = token
        self.chat_id  = chat_id
        self.attempts = attempts
        self.backoff  = backoff
        self.session  = requests.Session()

    def send(self, text: str, chat_id: str = None) -> bool:
        url     = self._SEND_URL.format(token=self.token)
        payload = {
            "chat_id":                  chat_id or self.chat_id,
            "text":                     text,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        }

        for attempt in range(1, self.attempts + 1):
            try:
                r = self.session.post(url, json=payload, timeout=10)
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning("Telegram rate-limited — waiting %ds", wait)
                    time.sleep(wait)
                    continue
                logger.warning("Telegram HTTP %d: %s", r.status_code, r.text[:200])
            except requests.exceptions.Timeout:
                logger.warning("Telegram timeout (attempt %d/%d)", attempt, self.attempts)
            except requests.exceptions.ConnectionError as e:
                logger.warning("Telegram connection error (attempt %d/%d): %s", attempt, self.attempts, e)
            except Exception as e:
                logger.error("Unexpected Telegram error: %s", e)

            if attempt < self.attempts:
                time.sleep(self.backoff * (2 ** (attempt - 1)))

        logger.error("Failed to send after %d attempts", self.attempts)
        return False

    def get_updates(self, offset: int = 0) -> list:
        url = self._UPD_URL.format(token=self.token)
        try:
            r = self.session.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            if r.status_code == 200:
                return r.json().get("result", [])
        except Exception as e:
            logger.warning("getUpdates error: %s", e)
        return []

    def close(self):
        self.session.close()


# ─── Message Formatting ───────────────────────────────────────────────────────

CONTAINER_META = {
    "attach":        ("🔗", "Attached"),
    "commit":        ("💾", "Committed"),
    "copy":          ("📋", "File Copied"),
    "create":        ("📦", "Created"),
    "destroy":       ("🗑", "Destroyed"),
    "detach":        ("🔌", "Detached"),
    "die":           ("🔴", "Died"),
    "exec_create":   ("⚙️", "Exec Created"),
    "exec_detach":   ("⚙️", "Exec Detached"),
    "exec_die":      ("⚙️", "Exec Died"),
    "exec_start":    ("⚙️", "Exec Started"),
    "export":        ("📤", "Exported"),
    "health_status": ("💊", "Health Check"),
    "kill":          ("🟠", "Killed"),
    "oom":           ("🚨", "OOM — Killed"),
    "pause":         ("⏸", "Paused"),
    "rename":        ("✏️", "Renamed"),
    "resize":        ("↔️", "Resized"),
    "restart":       ("🔁", "Restarted"),
    "start":         ("🟢", "Started"),
    "stop":          ("⚪", "Stopped"),
    "top":           ("📊", "Top Called"),
    "unpause":       ("▶️", "Resumed"),
    "update":        ("🔧", "Updated"),
}

NETWORK_META = {
    "connect":    ("🔗", "Container Connected"),
    "disconnect": ("🔌", "Container Disconnected"),
    "create":     ("🌐", "Network Created"),
    "destroy":    ("🗑", "Network Destroyed"),
}

VOLUME_META = {
    "create":  ("💿", "Volume Created"),
    "destroy": ("🗑", "Volume Destroyed"),
    "mount":   ("📌", "Volume Mounted"),
    "unmount": ("📍", "Volume Unmounted"),
}

IMAGE_META = {
    "delete": ("🗑", "Image Deleted"),
    "import": ("📥", "Image Imported"),
    "load":   ("📂", "Image Loaded"),
    "pull":   ("⬇️", "Image Pulled"),
    "push":   ("⬆️", "Image Pushed"),
    "save":   ("💾", "Image Saved"),
    "tag":    ("🏷", "Image Tagged"),
    "untag":  ("🏷", "Image Untagged"),
}

STATE_EMOJI = {
    "running":    "🟢",
    "exited":     "🔴",
    "paused":     "⏸",
    "restarting": "🔁",
    "dead":       "💀",
    "created":    "📦",
    "removing":   "🗑",
}

DEFAULT_EMOJI = "⚙️"
DIVIDER       = "―――――――――――――――――――"


def _now_ist() -> str:
    return datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")


def fmt_container(event: dict, hostname: str) -> str:
    attrs     = event.get("Actor", {}).get("Attributes", {})
    action    = event.get("Action", "unknown")
    base      = action.split(":")[0]
    name      = attrs.get("name", attrs.get("id", "?")[:12])
    image     = attrs.get("image", "unknown")
    exit_code = attrs.get("exitCode", "")
    health    = attrs.get("health_status", "")

    emoji, title = CONTAINER_META.get(base, (DEFAULT_EMOJI, base.capitalize()))

    lines = [
        f"{emoji} *{title}*",
        DIVIDER,
        f"🖥 Host       : `{hostname}`",
        f"📎 Container  : *{name}*",
        f"🐳 Image      : `{image}`",
        f"⚡ Status     : *{title}*",
    ]
    if exit_code:
        lines.append(f"🔢 Exit Code  : `{exit_code}`")
    if health:
        lines.append(f"💊 Health     : `{health}`")
    lines += [
        f"🕒 Time       : `{_now_ist()}`",
        DIVIDER,
    ]
    return "\n".join(lines)


def fmt_network(event: dict, hostname: str) -> str:
    attrs     = event.get("Actor", {}).get("Attributes", {})
    action    = event.get("Action", "unknown")
    name      = attrs.get("name", attrs.get("id", "?")[:12])
    net_type  = attrs.get("type", "")
    container = attrs.get("container", "")

    emoji, title = NETWORK_META.get(action, (DEFAULT_EMOJI, action.capitalize()))

    lines = [
        f"{emoji} *{title}*",
        DIVIDER,
        f"🖥 Host       : `{hostname}`",
        f"🌐 Network    : *{name}*",
    ]
    if net_type:
        lines.append(f"📡 Type       : `{net_type}`")
    if container:
        lines.append(f"📎 Container  : `{container}`")
    lines += [
        f"🕒 Time       : `{_now_ist()}`",
        DIVIDER,
    ]
    return "\n".join(lines)


def fmt_volume(event: dict, hostname: str) -> str:
    attrs  = event.get("Actor", {}).get("Attributes", {})
    action = event.get("Action", "unknown")
    name   = event.get("Actor", {}).get("ID", "?")
    driver = attrs.get("driver", "local")

    emoji, title = VOLUME_META.get(action, (DEFAULT_EMOJI, action.capitalize()))

    lines = [
        f"{emoji} *{title}*",
        DIVIDER,
        f"🖥 Host       : `{hostname}`",
        f"💿 Volume     : *{name}*",
        f"🔧 Driver     : `{driver}`",
        f"🕒 Time       : `{_now_ist()}`",
        DIVIDER,
    ]
    return "\n".join(lines)


def fmt_image(event: dict, hostname: str) -> str:
    action = event.get("Action", "unknown")
    name   = event.get("Actor", {}).get("ID", "?")

    emoji, title = IMAGE_META.get(action, (DEFAULT_EMOJI, action.capitalize()))

    lines = [
        f"{emoji} *{title}*",
        DIVIDER,
        f"🖥 Host       : `{hostname}`",
        f"🐳 Image      : *{name}*",
        f"🕒 Time       : `{_now_ist()}`",
        DIVIDER,
    ]
    return "\n".join(lines)


def fmt_startup(hostname: str) -> str:
    lines = [
        "🟢 *Notifier Online*",
        DIVIDER,
        f"🖥 Host       : `{hostname}`",
        f"👁 Container  : `{', '.join(sorted(Config.WATCH_ACTIONS)) or 'none'}`",
        f"🌐 Network    : `{', '.join(sorted(Config.WATCH_NETWORK))  or 'none'}`",
        f"💿 Volume     : `{', '.join(sorted(Config.WATCH_VOLUME))   or 'none'}`",
        f"🐳 Image      : `{', '.join(sorted(Config.WATCH_IMAGE))    or 'none'}`",
        f"🕒 Time       : `{_now_ist()}`",
        DIVIDER,
        "_you'll hear from me when something happens._",
    ]
    return "\n".join(lines)


def fmt_shutdown(hostname: str) -> str:
    lines = [
        "🔴 *Notifier Offline*",
        DIVIDER,
        f"🖥 Host       : `{hostname}`",
        f"🕒 Time       : `{_now_ist()}`",
        DIVIDER,
        "_no more alerts from this host until it restarts._",
    ]
    return "\n".join(lines)


def fmt_status(hostname: str, containers: list) -> str:
    lines = [
        f"📋 *{hostname} — Container Status*",
        DIVIDER,
    ]

    if not containers:
        lines.append("_no containers found_")
    else:
        for c in sorted(containers, key=lambda x: x.name):
            state     = c.status
            image     = c.image.tags[0] if c.image.tags else str(c.image.id)[:12]
            emoji     = STATE_EMOJI.get(state, "⚪")
            info      = c.attrs.get("State", {})
            started   = info.get("StartedAt", "")[:19].replace("T", " ")
            finished  = info.get("FinishedAt", "")[:19].replace("T", " ")
            exit_code = str(info.get("ExitCode", ""))

            lines.append(f"{emoji} *{c.name}*")
            lines.append(f"   🐳 Image   : `{image}`")
            lines.append(f"   📌 Status  : `{state}`")
            if state == "running":
                lines.append(f"   🕐 Started : `{started}`")
            else:
                lines.append(f"   🕐 Stopped : `{finished}`")
                if exit_code and exit_code != "0":
                    lines.append(f"   🔢 Exit    : `{exit_code}`")
            lines.append("")

    lines += [
        DIVIDER,
        f"🕒 Time       : `{_now_ist()}`",
    ]
    return "\n".join(lines)


# ─── Command Handler ──────────────────────────────────────────────────────────

class CommandHandler:
    """Polls Telegram getUpdates and responds to bot commands."""

    def __init__(self, sender: TelegramSender, running_ref):
        self.sender      = sender
        self.running_ref = running_ref  # callable → bool
        self._offset     = 0

    def run(self):
        logger.info("Command listener started")
        while self.running_ref():
            updates = self.sender.get_updates(offset=self._offset)
            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    self._handle(update)
                except Exception as e:
                    logger.error("Command handler error: %s", e)
        logger.info("Command listener stopped")

    def _handle(self, update: dict):
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        text    = msg.get("text", "").strip()
        chat_id = str(msg["chat"]["id"])

        if not text.startswith("/"):
            return

        parts   = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower()   # handle /cmd@BotName
        arg     = parts[1].strip() if len(parts) > 1 else ""

        logger.info("Command: %s  arg=%r  chat=%s", command, arg, chat_id)

        if command == "/status":
            self._cmd_status(chat_id, arg)
        elif command == "/help":
            self._cmd_help(chat_id)
        else:
            self.sender.send(
                "❓ Unknown command.\n\nSend /help to see available commands.",
                chat_id=chat_id,
            )

    # ── /status ───────────────────────────────────────────────────────────────

    def _cmd_status(self, chat_id: str, hostname_filter: str):
        """
        /status              → show this host's containers
        /status <hostname>   → only respond if hostname matches this instance
        """
        target = hostname_filter or Config.HOSTNAME

        if target.lower() != Config.HOSTNAME.lower():
            self.sender.send(
                f"⚠️ This watchdog is running on *{Config.HOSTNAME}*, not `{target}`.",
                chat_id=chat_id,
            )
            return

        try:
            client     = docker.from_env()
            containers = client.containers.list(all=True)
            client.close()
        except Exception as e:
            logger.error("Docker error in /status: %s", e)
            self.sender.send(
                f"🔴 Could not reach Docker daemon:\n`{str(e)}`",
                chat_id=chat_id,
            )
            return

        self.sender.send(fmt_status(Config.HOSTNAME, containers), chat_id=chat_id)

    # ── /help ─────────────────────────────────────────────────────────────────

    def _cmd_help(self, chat_id: str):
        lines = [
            "🤖 *Watchdog Commands*",
            DIVIDER,
            "/status  —  list all containers on this host",
            f"/status `{Config.HOSTNAME}`  —  same, scoped to hostname",
            "/help  —  show this message",
            DIVIDER,
            f"🖥 Host     : `{Config.HOSTNAME}`",
            f"👁 Watching : `{', '.join(sorted(Config.WATCH_ACTIONS)) or 'none'}`",
        ]
        self.sender.send("\n".join(lines), chat_id=chat_id)


# ─── Monitor ──────────────────────────────────────────────────────────────────

class DockerMonitor:
    def __init__(self):
        Config.validate()
        self.sender       = TelegramSender(
            Config.TOKEN, Config.CHAT_ID,
            Config.RETRY_ATTEMPTS, Config.RETRY_BACKOFF,
        )
        self.rate_limiter = RateLimiter(Config.RATE_LIMIT_MAX, Config.RATE_LIMIT_WINDOW)
        self.running      = True
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def _shutdown(self, signum, _frame):
        logger.info("Signal %s received — shutting down", signal.Signals(signum).name)
        self.running = False

    def _should_process(self, container_name: str) -> bool:
        if Config.IGNORE_CONTAINERS and any(i in container_name for i in Config.IGNORE_CONTAINERS):
            return False
        if Config.WATCH_CONTAINERS and not any(w in container_name for w in Config.WATCH_CONTAINERS):
            return False
        return True

    def _connect(self) -> Optional[docker.DockerClient]:
        backoff = 5
        while self.running:
            try:
                client = docker.from_env()
                client.ping()
                logger.info("Connected to Docker daemon")
                return client
            except Exception as e:
                logger.error("Docker connect failed (%s) — retry in %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        return None

    def run(self):
        logger.info("Watchdog starting — host=%s", Config.HOSTNAME)
        if Config.NOTIFY_STARTUP:
            self.sender.send(fmt_startup(Config.HOSTNAME))

        # Command listener runs in a background daemon thread
        cmd_thread = Thread(
            target=CommandHandler(self.sender, lambda: self.running).run,
            name="cmd-listener",
            daemon=True,
        )
        cmd_thread.start()

        try:
            while self.running:
                client = self._connect()
                if not client:
                    break
                try:
                    self._loop(client)
                except docker.errors.APIError as e:
                    logger.error("Docker API error: %s", e)
                except Exception as e:
                    logger.exception("Event loop error: %s", e)
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
                if self.running:
                    logger.info("Reconnecting in 5s…")
                    time.sleep(5)
        finally:
            if Config.NOTIFY_SHUTDOWN:
                self.sender.send(fmt_shutdown(Config.HOSTNAME))
            self.sender.close()
            logger.info("Stopped cleanly.")

    def _loop(self, client: docker.DockerClient):
        logger.info("Listening for Docker events…")

        for event in client.events(decode=True):
            if not self.running:
                break

            etype  = event.get("Type", "")
            action = event.get("Action", "").split(":")[0]
            attrs  = event.get("Actor", {}).get("Attributes", {})

            if etype == "container":
                if action not in Config.WATCH_ACTIONS:
                    continue
                name = attrs.get("name", "unknown")
                if not self._should_process(name):
                    continue
                key = f"container:{name}:{action}"
                if not self.rate_limiter.allow(key):
                    logger.warning("Rate limit hit — suppressing %s", key)
                    continue
                logger.info("container  name=%-30s action=%s", name, action)
                self.sender.send(fmt_container(event, Config.HOSTNAME))

            elif etype == "network":
                if not Config.WATCH_NETWORK or action not in Config.WATCH_NETWORK:
                    continue
                net_name = attrs.get("name", event.get("Actor", {}).get("ID", "?")[:12])
                key = f"network:{net_name}:{action}"
                if not self.rate_limiter.allow(key):
                    continue
                logger.info("network  name=%-30s action=%s", net_name, action)
                self.sender.send(fmt_network(event, Config.HOSTNAME))

            elif etype == "volume":
                if not Config.WATCH_VOLUME or action not in Config.WATCH_VOLUME:
                    continue
                vol_name = event.get("Actor", {}).get("ID", "?")
                key = f"volume:{vol_name}:{action}"
                if not self.rate_limiter.allow(key):
                    continue
                logger.info("volume  name=%-30s action=%s", vol_name, action)
                self.sender.send(fmt_volume(event, Config.HOSTNAME))

            elif etype == "image":
                if not Config.WATCH_IMAGE or action not in Config.WATCH_IMAGE:
                    continue
                img_name = event.get("Actor", {}).get("ID", "?")
                key = f"image:{img_name}:{action}"
                if not self.rate_limiter.allow(key):
                    continue
                logger.info("image  name=%-30s action=%s", img_name, action)
                self.sender.send(fmt_image(event, Config.HOSTNAME))


# ─── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    DockerMonitor().run()