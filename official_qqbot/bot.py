"""
QQ 官方 Bot 启动入口。

代码按「会一起改的东西」分在这些文件里，这里只剩启动：
- config.py         配置读取（config.yaml + 环境变量）
- control_state.py  管理员/Staff/封禁/邮箱（控制台 API）和日志上报
- qq_api.py         QQ 接口：token、共用连接池、发消息（一份代码）
- accounts.py       积分、签到、拉群奖励、身份归一、QQ 绑定（JSON 或数据库）
- ads.py            广告（bot 和控制台共用）
- mailer.py         发邮件
- resources.py      NFA / 4399 / sauth / 163 / 库存
- commands.py       指令表；commands_admin.py 管理类指令
- messages.py       一条消息的处理顺序（指令、天气、链接、生图、AI）
- ingress.py        webhook / WebSocket 入口、事件分发、群事件

为了不大面积改测试，bot.X 仍然能读写以前在 bot.py 里的名字：读会转到定义它的文件，
写（包括 unittest.mock.patch）会同时改所有导入了它的文件。
"""

import asyncio
import signal
import sys
import types

import accounts as _m_accounts
import ads as _m_ads
import commands as _m_commands
import commands_admin as _m_commands_admin
import config as _m_config
import control_state as _m_control_state
import ingress as _m_ingress
import mailer as _m_mailer
import messages as _m_messages
import qq_api as _m_qq_api
import resources as _m_resources
import shared_cooldown as _m_shared_cd
from config import BRIDGE_ENABLED, OFFICIAL_WS_ENABLED
from ingress import run_koishi_bridge_server, run_websocket
from qq_api import refresh_access_token


# ====== 状态存储：JSON 文件（默认）或数据库（BOT_STATE_STORE=db） ======
def _init_state_store() -> None:
    if _m_config.BOT_STATE_STORE == "db":
        import state_db

        engine = state_db.open_state_engine()
        _m_accounts.use_database(state_db.SqlAccountStore(engine))
        _m_ads.use_database(state_db.SqlAdsStore(engine))
        _m_shared_cd.use_database(state_db.SqlCooldownStore(engine))
        _m_config._log.info(f"[State] bot state store=db ({engine.url.render_as_string(hide_password=True)})")
    else:
        _m_config._log.info("[State] bot state store=json")
    _m_ads.init_bot_ads()


_init_state_store()


async def token_refresh_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            await refresh_access_token()
        except Exception as e:
            _m_config._log.warning(f"[Auth] 刷新失败: {e}")


def _build_runtime_tasks(
    official_ws_enabled: bool = OFFICIAL_WS_ENABLED,
    bridge_enabled: bool = BRIDGE_ENABLED,
):
    tasks = [token_refresh_loop()]
    if official_ws_enabled:
        tasks.insert(0, run_websocket())
    if bridge_enabled:
        tasks.append(run_koishi_bridge_server())
    return tasks


async def _graceful_shutdown(runtime: asyncio.Future) -> None:
    """SIGTERM：先停监听，再等手上的消息处理完（最多 20 秒）、日志队列发完，然后退出。"""
    log = _m_config._log
    log.info(f"[退出] 收到停止信号，等待 {len(_m_ingress._inbound_tasks)} 个处理中的事件")
    runtime.cancel()
    try:
        await runtime  # 监听先关掉，不再收新回调
    except (asyncio.CancelledError, Exception):
        pass
    await _m_ingress.drain_inbound_tasks(timeout=20)
    drain = getattr(_m_control_state._state_backend, "drain", None)
    if drain:
        await drain(timeout=5)
    log.info("[退出] 完成")


async def main():
    c, log = _m_config, _m_config._log
    log.info("=" * 50)
    log.info("QQ 官方 Bot 全功能版启动")
    log.info(f"AppID: {c.APP_ID} | v{c.BOT_VERSION}")
    log.info(f"环境: {'沙箱' if c.SANDBOX else '正式'} | AI: {c.AI_CONFIG.get('model', '?')}")
    log.info(
        f"AI 生图: {'启用' if c.IMAGE_AI_ENABLED else '关闭'} | "
        f"配置: {'完整' if c.image_generator.configured else '未完成'} | "
        f"模型: {c.IMAGE_AI_MODEL or '未配置'}"
    )
    log.info(f"Gateway intents: {c.GATEWAY_INTENTS}")
    log.info(f"Full group messages: commands only; groups={sorted(c.FULL_MESSAGE_GROUP_IDS) or 'all'}")
    log.info(f"Official WS enabled: {c.OFFICIAL_WS_ENABLED}")
    log.info(f"Bridge listener enabled: {c.BRIDGE_ENABLED}")
    log.info("=" * 50)
    await refresh_access_token()
    runtime = asyncio.gather(*_build_runtime_tasks())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    stopper = asyncio.ensure_future(stop.wait())
    await asyncio.wait({runtime, stopper}, return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set():
        await _graceful_shutdown(runtime)
        return
    stopper.cancel()
    await runtime


# ====== 旧名字转发 ======
_SUBMODULES = (
    _m_config,
    _m_control_state,
    _m_mailer,
    _m_qq_api,
    _m_ads,
    _m_accounts,
    _m_resources,
    _m_commands_admin,
    _m_commands,
    _m_messages,
    _m_ingress,
)
_deleted_holders: dict[str, list] = {}


class _BotModule(types.ModuleType):
    def __getattr__(self, name):
        for module in _SUBMODULES:
            namespace = module.__dict__
            if name in namespace:
                return namespace[name]
        raise AttributeError(f"module 'bot' has no attribute {name!r}")

    def __setattr__(self, name, value):
        holders = [m for m in _SUBMODULES if name in m.__dict__] or _deleted_holders.pop(name, [])
        if name in self.__dict__ or not holders:
            super().__setattr__(name, value)
        for module in holders:
            setattr(module, name, value)

    def __delattr__(self, name):
        holders = [m for m in _SUBMODULES if name in m.__dict__]
        if name in self.__dict__:
            super().__delattr__(name)
        elif not holders:
            raise AttributeError(name)
        for module in holders:
            delattr(module, name)
        if holders:
            _deleted_holders[name] = holders


sys.modules[__name__].__class__ = _BotModule


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _m_config._log.info("Bot 已停止")
