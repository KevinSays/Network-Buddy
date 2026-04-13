"""
MHS: Mikrotik Homelab Scanner configuration.
Values are read from environment variables; a .env file in the project root
is loaded automatically if python-dotenv is installed.

Copy .env.example → .env and fill in your credentials.
"""

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class DeviceCreds:
    host: str
    username: str = "admin"
    password: str = ""
    verify_ssl: bool = False


@dataclass
class Settings:
    router: DeviceCreds = field(
        default_factory=lambda: DeviceCreds(
            host=os.getenv("ROUTER_HOST", "192.168.4.1"),
            username=os.getenv("ROUTER_USER", "admin"),
            password=os.getenv("ROUTER_PASS", ""),
            verify_ssl=os.getenv("ROUTER_VERIFY_SSL", "false").lower() == "true",
        )
    )
    switch: DeviceCreds = field(
        default_factory=lambda: DeviceCreds(
            host=os.getenv("SWITCH_HOST", ""),   # empty = not configured
            username=os.getenv("SWITCH_USER", "admin"),
            password=os.getenv("SWITCH_PASS", ""),
            verify_ssl=os.getenv("SWITCH_VERIFY_SSL", "false").lower() == "true",
        )
    )
    app_host: str = field(default_factory=lambda: os.getenv("APP_HOST", "0.0.0.0"))
    app_port: int = field(default_factory=lambda: int(os.getenv("APP_PORT", "8000")))

    # ── Alerts ────────────────────────────────────────────────────────────────
    alert_webhook_url: str = field(default_factory=lambda: os.getenv("ALERT_WEBHOOK_URL", ""))
    alert_ntfy_topic:  str = field(default_factory=lambda: os.getenv("ALERT_NTFY_TOPIC",  ""))

    # ── Dashboard auth (HTTP Basic — leave blank to disable) ──────────────────
    dashboard_user: str = field(default_factory=lambda: os.getenv("DASHBOARD_USER", ""))
    dashboard_pass: str = field(default_factory=lambda: os.getenv("DASHBOARD_PASS", ""))


settings = Settings()
