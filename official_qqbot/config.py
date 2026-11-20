"""配置：config.yaml + 环境变量（环境变量优先，密钥只放在 root 才能读的 EnvironmentFile 里）。"""

import logging
import os
import time as _time_mod

import yaml

from handlers.ai_chat import AIChat
from handlers.image_gen import ImageGenerator

# ====== 版本 ======
BOT_VERSION = "1.3.0"
BOT_BUILD_DATE = "2026-10-25"
_start_time: float = _time_mod.time()

# ====== 日志 ======
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("OfficialBot")

# ====== 加载配置 ======
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_BOT_DIR, "config.yaml")
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

BOT_CONFIG = config.get("bot", {})
AI_CONFIG = config.get("ai", {})
IMAGE_AI_CONFIG = config.get("image_ai", {})
EMAIL_CONFIG = config.get("email", {})
BRIDGE_CONFIG = config.get("bridge", {})
CONTROL_CONFIG = config.get("control_api", {})


def _config_string_set(value) -> set[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not value:
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _config_value(section: dict, key: str, env_name: str | None = None, default=None):
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value is not None and str(env_value).strip():
            return str(env_value).strip()
    return section.get(key, default)


def _ai_config_value(section, key: str, env_name: str | None = None, default=None):
    if not isinstance(section, dict):
        section = {}
    config_value = section.get(key)
    if config_value is not None and str(config_value).strip():
        return str(config_value).strip()
    return _config_value(section, key, env_name, default)


def _ai_config_bool(
    section: dict, key: str, env_name: str | None = None, default: bool = False
) -> bool:
    value = _ai_config_value(section, key, env_name, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _ai_config_int(
    section: dict, key: str, env_name: str | None = None, default: int = 0
) -> int:
    try:
        return int(_ai_config_value(section, key, env_name, default))
    except (TypeError, ValueError):
        return int(default)


def _config_bool(
    section: dict, key: str, env_name: str | None = None, default: bool = False
) -> bool:
    value = _config_value(section, key, env_name, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _full_message_group_ids(section: dict) -> set[str]:
    return (
        _config_string_set(section.get("full_message_group_ids", []))
        | _config_string_set(section.get("non_at_group_whitelist", []))
        | _config_string_set(os.environ.get("QQ_GROUP_WHITELIST", ""))
    )


APP_ID = _config_value(BOT_CONFIG, "app_id", "QQ_APP_ID")
APP_SECRET = _config_value(BOT_CONFIG, "app_secret", "QQ_APP_SECRET")
SANDBOX = BOT_CONFIG.get("sandbox", False)
# 管理员口令只从配置或 root 才能读的环境变量文件取；没配置时 /auth 不接受任何口令。
ADMIN_SECRET = str(_config_value(config, "admin_secret", "BOT_ADMIN_SECRET", "") or "")
# NFA 管理账号同上，不写进代码（公开仓库）。
NFA_CONFIG = config.get("nfa", {}) or {}
NFA_USERNAME = str(_config_value(NFA_CONFIG, "username", "NFA_USERNAME", "") or "")
NFA_PASSWORD = str(_config_value(NFA_CONFIG, "password", "NFA_PASSWORD", "") or "")
# /bindqq 本人验证："email" = 先发验证码到 QQ号@qq.com；"off" = 不验证（旧行为）
QQ_BIND_VERIFY = str(
    _config_value(BOT_CONFIG, "qq_bind_verify", "QQ_BIND_VERIFY", "off") or "off"
).strip().lower()
API_BASE = (
    "https://sandbox.api.sgroup.qq.com" if SANDBOX else "https://api.sgroup.qq.com"
)
AUTH_URL = "https://bots.qq.com/app/getAppAccessToken"
FULL_MESSAGE_GROUP_IDS = _full_message_group_ids(BOT_CONFIG)
GATEWAY_INTENTS = int(_config_value(BOT_CONFIG, "intents", "QQ_INTENTS", 1 << 25))
OFFICIAL_WS_ENABLED = _config_bool(
    BOT_CONFIG, "official_ws_enabled", "QQ_OFFICIAL_WS_ENABLED", True
)
BRIDGE_ENABLED = _config_bool(BRIDGE_CONFIG, "enabled", "QQ_BRIDGE_ENABLED", False)
BRIDGE_HOST = str(BRIDGE_CONFIG.get("host", "127.0.0.1"))
BRIDGE_PORT = int(BRIDGE_CONFIG.get("port", 8765))
MARKDOWN_ENABLED = _config_bool(
    BOT_CONFIG, "markdown_enabled", "QQ_MARKDOWN_ENABLED", True
)
QQ_WEBHOOK_PATH = str(_config_value(BOT_CONFIG, "webhook_path", "QQ_WEBHOOK_PATH", "/qq") or "/qq")
# 回调先回包、后台处理（默认开）。设 QQ_WEBHOOK_ASYNC=0 恢复以前「处理完才回包」的行为。
QQ_WEBHOOK_ASYNC = _config_bool(BOT_CONFIG, "webhook_async", "QQ_WEBHOOK_ASYNC", True)
if not QQ_WEBHOOK_PATH.startswith("/"):
    QQ_WEBHOOK_PATH = "/" + QQ_WEBHOOK_PATH
CONTROL_API_BASE_URL = str(
    _config_value(CONTROL_CONFIG, "base_url", "CONTROL_API_BASE_URL", "") or ""
)
BOT_CONTROL_TOKEN = str(
    _config_value(CONTROL_CONFIG, "bot_token", "BOT_CONTROL_TOKEN", "") or ""
)
AI_BASE_URL = str(
    _ai_config_value(AI_CONFIG, "base_url", "AI_BASE_URL", "")
    or ""
)
AI_API_KEY = str(_ai_config_value(AI_CONFIG, "api_key", "AI_API_KEY", "") or "")
AI_MODEL = str(_ai_config_value(AI_CONFIG, "model", "AI_MODEL", "deepseek-v4-flash") or "")
AI_CONFIG["base_url"] = AI_BASE_URL
AI_CONFIG["api_key"] = AI_API_KEY
AI_CONFIG["model"] = AI_MODEL
IMAGE_AI_ENABLED = _ai_config_bool(
    IMAGE_AI_CONFIG, "enabled", "IMAGE_AI_ENABLED", False
)
IMAGE_AI_BASE_URL = str(
    _ai_config_value(IMAGE_AI_CONFIG, "base_url", "IMAGE_AI_BASE_URL", "") or ""
)
IMAGE_AI_API_KEY = str(
    _ai_config_value(IMAGE_AI_CONFIG, "api_key", "IMAGE_AI_API_KEY", "") or ""
)
IMAGE_AI_MODEL = str(
    _ai_config_value(
        IMAGE_AI_CONFIG,
        "model",
        "IMAGE_AI_MODEL",
        "grok-imagine-1.0-fast",
    )
    or ""
)
IMAGE_AI_SIZE = str(
    _ai_config_value(IMAGE_AI_CONFIG, "size", "IMAGE_AI_SIZE", "1024x1024")
    or "1024x1024"
)
IMAGE_AI_COOLDOWN_SECONDS = max(
    0,
    _ai_config_int(
        IMAGE_AI_CONFIG,
        "cooldown_seconds",
        "IMAGE_AI_COOLDOWN_SECONDS",
        60,
    ),
)

# 机器人自己的状态（积分/身份/绑定/广告/冷却）存哪里：json = data/*.json（默认），db = 数据库
BOT_STATE_STORE = str(os.environ.get("BOT_STATE_STORE", "json") or "json").strip().lower()

# ====== AI ======
system_prompt = ""
prompt_file = AI_CONFIG.get("system_prompt_file", "")
if prompt_file:
    prompt_path = os.path.join(_BOT_DIR, prompt_file)
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            system_prompt = f.read().strip()
        _log.info(f"已加载系统提示词（{len(system_prompt)} 字）")

ai_chat = AIChat(
    base_url=AI_CONFIG.get("base_url", ""),
    api_key=AI_CONFIG.get("api_key", ""),
    model=AI_CONFIG.get("model", ""),
    system_prompt=system_prompt,
    max_history=AI_CONFIG.get("max_history", 10),
)
image_generator = ImageGenerator(
    base_url=IMAGE_AI_BASE_URL,
    api_key=IMAGE_AI_API_KEY,
    model=IMAGE_AI_MODEL,
    size=IMAGE_AI_SIZE,
)
