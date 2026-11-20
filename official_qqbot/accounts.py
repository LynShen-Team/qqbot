"""积分账户：签到、扣分、拉群奖励、身份归一、QQ 号绑定。

三套身份键（以前散在各处，这里写清楚）：
1. 场景 openid = ctx["user_openid"]：同一个人在每个群、私聊各不相同。
   控制台的用户、封禁、邮箱绑定、管理员/Staff 都按它记。
2. limit_user_id = ctx["limit_user_id"]：平台下发的 union_openid，没有就等于场景 openid。
   NFA 的冷却/限额、「处理中」标记、QQ 号绑定（qq_binds）按它记。
3. 积分账户键 account_key = _account_key(limit_user_id)：QQ 绑定 "QQ<号>" > union 映射 > limit_user_id。
   积分、签到、拉群奖励、4399/sauth/163 的扣分、频控和用量都按它记。

存储：BOT_STATE_STORE=json（默认）读写 data/sign_official.json、identity_map.json、qq_binds.json；
=db 时走 state_db.SqlAccountStore（事务 + 行锁）。下面的业务逻辑两种存储共用一份。
"""

from __future__ import annotations

import datetime
import os
import time as _time_mod
from contextlib import contextmanager

import json_state
from config import _BOT_DIR, _log
from qq_api import send_group_msg

# ====== 积分制常量（2026-10-25 起固定积分发放/消耗） ======
SIGN_IN_POINTS = 50
EXTRACT_POINTS_COST = {"4399": 2, "sauth": 5, "163": 50}
GROUP_ADD_REWARD_POINTS = 25
GROUP_ADD_MIN_MEMBERS = 10
GROUP_ADD_DAILY_LIMIT = 10
SAUTH_RATE_LIMIT = 5  # 每分钟最多 5 次
SAUTH_RATE_WINDOW = 60
SAUTH_163_COOLDOWN = 3600  # 163 每小时最多 1 次

SIGN_FILE = os.path.join(_BOT_DIR, "data", "sign_official.json")
IDENTITY_FILE = os.path.join(_BOT_DIR, "data", "identity_map.json")
QQ_BIND_FILE = os.path.join(_BOT_DIR, "data", "qq_binds.json")


# ====== JSON 存储 ======
def _load_sign():
    data = json_state.read_json(SIGN_FILE, {}, label="sign_official.json（积分）")
    return data if isinstance(data, dict) else {}


def _save_sign(data):
    json_state.write_json(SIGN_FILE, data, indent=2, label="sign_official.json（积分）")


def _load_identity() -> dict:
    data = json_state.read_json(IDENTITY_FILE, {}, label="identity_map.json")
    return data if isinstance(data, dict) else {}


def _save_identity(data: dict):
    json_state.write_json(IDENTITY_FILE, data, indent=2, label="identity_map.json")


def _load_qq_binds() -> dict:
    data = json_state.read_json(QQ_BIND_FILE, {}, label="qq_binds.json")
    return data if isinstance(data, dict) else {}


def _save_qq_binds() -> None:
    json_state.write_json(QQ_BIND_FILE, _qq_binds, indent=2, label="qq_binds.json")


_sign_data: dict = _load_sign()
_identity_map: dict = _load_identity()
_qq_binds: dict = _load_qq_binds()


class _JsonTx:
    """Same interface as state_db.SqlAccountTx, on the in-memory dicts; dirty files are saved on exit."""

    def __init__(self):
        self.dirty: set[str] = set()

    def account(self, key):
        value = _sign_data.get(key)
        return value if isinstance(value, dict) else None

    def put_account(self, key, account):
        _sign_data[key] = account
        self.dirty.add("sign")

    def qq_bind(self, limit_user_id):
        return _qq_binds.get(limit_user_id) or None

    def put_qq_bind(self, limit_user_id, qq):
        _qq_binds[limit_user_id] = qq
        self.dirty.add("qq")

    def delete_qq_bind(self, limit_user_id):
        if _qq_binds.pop(limit_user_id, None) is None:
            return False
        self.dirty.add("qq")
        return True

    def union_of(self, scenario_openid):
        value = _identity_map.get(scenario_openid)
        return value if isinstance(value, str) and not scenario_openid.startswith("_") else None

    def put_union(self, scenario_openid, union_openid):
        _identity_map[scenario_openid] = union_openid
        self.dirty.add("identity")

    def is_known_openid(self, openid):
        return openid in _identity_map

    def pending(self):
        pending = _identity_map.get("_pending")
        if not isinstance(pending, list):
            pending = []
            _identity_map["_pending"] = pending
        return [item for item in pending if isinstance(item, dict)]

    def add_pending(self, item):
        self.pending()
        _identity_map["_pending"].append(item)
        self.dirty.add("identity")

    def mark_pending_notified(self, items):
        for item in items:
            item["notified"] = True
        self.dirty.add("identity")

    def remove_pending(self, items):
        ids = {id(item) for item in items}
        remaining = [item for item in _identity_map.get("_pending", []) if id(item) not in ids]
        if remaining:
            _identity_map["_pending"] = remaining
        else:
            _identity_map.pop("_pending", None)
        self.dirty.add("identity")

    def mentions(self):
        data = _identity_map.get("_bot_mentions")
        return dict(data) if isinstance(data, dict) else {}

    def put_mention(self, group_openid, mention_id):
        data = _identity_map.get("_bot_mentions")
        if not isinstance(data, dict):
            data = {}
        data[group_openid] = mention_id
        _identity_map["_bot_mentions"] = data
        self.dirty.add("identity")

    def top_accounts(self, limit):
        active = [
            (key, info) for key, info in _sign_data.items()
            if isinstance(info, dict) and not info.get("merged_into")
        ]
        return sorted(active, key=lambda item: item[1].get("points", 0), reverse=True)[:limit]

    def count_active(self):
        return sum(1 for info in _sign_data.values() if isinstance(info, dict) and not info.get("merged_into"))

    def save(self):
        if "qq" in self.dirty:
            _save_qq_binds()
        if "sign" in self.dirty:
            _save_sign(_sign_data)
        if "identity" in self.dirty:
            _save_identity(_identity_map)


_db_store = None  # state_db.SqlAccountStore when BOT_STATE_STORE=db


def use_database(store) -> None:
    global _db_store
    _db_store = store


@contextmanager
def _tx(readonly: bool = False):
    if _db_store is not None:
        with _db_store.transaction(readonly=readonly) as tx:
            yield tx
        return
    tx = _JsonTx()
    yield tx
    tx.save()


def _new_account() -> dict:
    return {"points": 0, "streak": 0, "last": ""}


def _today_str() -> str:
    return datetime.date.today().isoformat()


# ====== 签到 / 积分 ======
def do_sign_in(user_openid):
    today = datetime.date.today().isoformat()
    with _tx() as tx:
        user = tx.account(user_openid) or _new_account()
        if user.get("last") == today:
            return f"你今天已经签到过了喵~\n当前积分：{user['points']}\n连签：{user['streak']}天"

        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        if user.get("last") == yesterday:
            user["streak"] = user.get("streak", 0) + 1
        else:
            user["streak"] = 1

        user["points"] = user.get("points", 0) + SIGN_IN_POINTS
        user["last"] = today
        tx.put_account(user_openid, user)
    return (
        f"签到成功喵~\n"
        f"获得 {SIGN_IN_POINTS} 积分\n"
        f"当前积分：{user['points']}\n"
        f"连续签到：{user['streak']}天"
    )


def get_user_points(user_openid) -> int:
    with _tx(readonly=True) as tx:
        user = tx.account(user_openid) or {}
    return int(user.get("points", 0) or 0)


def _points_shortage_text(cost: int, balance: int) -> str:
    return (
        f"积分不足喵~提取该资源需要 {cost} 积分，你当前有 {balance} 积分\n"
        f"每日签到 +{SIGN_IN_POINTS}；邀请我进 10 人以上的群 +{GROUP_ADD_REWARD_POINTS}"
        f"（每天最多 {GROUP_ADD_DAILY_LIMIT} 次）\n"
        f"发送 /bindqq 你的QQ号，所有群的积分自动合并成一个账户喵~"
    )


def try_spend_points(user_openid, cost: int) -> tuple[bool, int]:
    """余额足够则立即扣除，返回 (是否成功, 剩余积分)。数据库模式下在行锁里扣，不会并发扣穿。"""
    with _tx() as tx:
        user = tx.account(user_openid) or _new_account()
        balance = int(user.get("points", 0) or 0)
        if balance < cost:
            return False, balance
        user["points"] = balance - cost
        tx.put_account(user_openid, user)
    return True, user["points"]


def _award_group_add_points(tx, operator_openid: str, group_openid: str) -> tuple[str, int]:
    today = _today_str()
    user = tx.account(operator_openid) or _new_account()
    rewarded = user.setdefault("rewarded_groups", [])
    if group_openid in rewarded:
        return "duplicate_group", 0
    if user.get("group_add_last") != today:
        user["group_add_last"] = today
        user["group_add_count"] = 0
    if int(user.get("group_add_count", 0) or 0) >= GROUP_ADD_DAILY_LIMIT:
        return "daily_limit", 0
    user["group_add_count"] = int(user.get("group_add_count", 0) or 0) + 1
    user["points"] = int(user.get("points", 0) or 0) + GROUP_ADD_REWARD_POINTS
    rewarded.append(group_openid)
    if len(rewarded) > 200:
        del rewarded[: len(rewarded) - 200]
    tx.put_account(operator_openid, user)
    return "ok", GROUP_ADD_REWARD_POINTS


def award_group_add_points(operator_openid: str, group_openid: str) -> tuple[str, int]:
    """
    拉群奖励结算：群人数达标且当日次数未超限时 +25 积分。
    返回 (结果原因, 本次获得积分)
    """
    with _tx() as tx:
        return _award_group_add_points(tx, operator_openid, group_openid)


def points_balance_text(user_openid) -> str:
    with _tx(readonly=True) as tx:
        user = tx.account(user_openid) or {}
    used_today = int(user.get("group_add_count", 0) or 0) \
        if user.get("group_add_last") == _today_str() else 0
    qq_hint = (
        "\n━━━ 统一账户 ━━━\n发送 /bindqq 你的QQ号，换群/私聊积分合并成一个账户"
        if not user_openid.startswith("QQ") else ""
    )
    return (
        f"当前积分：{user.get('points', 0)}\n"
        f"━━━ 积分规则 ━━━\n"
        f"签到 +{SIGN_IN_POINTS}/天\n"
        f"邀请我进 10 人以上群 +{GROUP_ADD_REWARD_POINTS}"
        f"（今日 {used_today}/{GROUP_ADD_DAILY_LIMIT} 次，拉群后在群里 @我 领取）\n"
        f"提取 4399 小号 -{EXTRACT_POINTS_COST['4399']} 积分（不限次）\n"
        f"提取 sauth -{EXTRACT_POINTS_COST['sauth']} 积分（1 分钟最多 {SAUTH_RATE_LIMIT} 次）\n"
        f"提取 163 小号 -{EXTRACT_POINTS_COST['163']} 积分（1 小时最多 1 次）"
        f"{qq_hint}"
    )


def leaderboard_text() -> str | None:
    """排行榜 TOP10；没人签到过返回 None。"""
    with _tx(readonly=True) as tx:
        top = tx.top_accounts(10)
    if not top:
        return None
    lines = ["签到排行榜 TOP10 喵~", "━━━━━━━━━━━━━━"]
    for i, (uid, info) in enumerate(top, 1):
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        label = uid if uid.startswith("QQ") and uid[2:].isdigit() else f"{uid[:8]}..."
        lines.append(
            f"{medal} {label} | {info.get('points', 0)}分 | 连签{info.get('streak', 0)}天"
        )
    lines.append("━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ====== 身份归一：union_openid 是唯一积分账户键 ======
# 同一用户在不同群/私聊拿到的 openid 不同；积分一律记到 union_openid 上，
# 场景 openid -> union 的映射随消息到达自动学习。
def _learn_identity(scenario_openid: str, union_openid: str) -> list[str]:
    """记录 场景openid -> union 映射，返回本次新学到的 openid（触发老积分合并）"""
    if not scenario_openid or not union_openid or scenario_openid == union_openid:
        return []
    with _tx() as tx:
        if tx.union_of(scenario_openid) == union_openid:
            return []
        tx.put_union(scenario_openid, union_openid)
        # QQ 绑定跟随本人迁移到 union 键（平台开始下发 union 后不丢账户）
        qq = tx.qq_bind(scenario_openid)
        if qq and not tx.qq_bind(union_openid):
            tx.put_qq_bind(union_openid, qq)
    _log.info(f"[身份] {scenario_openid[:8]}... -> union {union_openid[:8]}...")
    return [scenario_openid]


def _merge_legacy_points(union_openid: str, openids: list[str]) -> None:
    """把旧 openid 账户里剩余的积分一次性并入 union 账户"""
    gained = 0
    with _tx() as tx:
        account = tx.account(union_openid) or _new_account()
        for openid in openids:
            legacy = tx.account(openid)
            if not isinstance(legacy, dict) or legacy.get("merged_into"):
                continue
            gained += int(legacy.get("points", 0) or 0)
            legacy["points"] = 0
            legacy["merged_into"] = union_openid
            tx.put_account(openid, legacy)
        if gained > 0:
            account["points"] = int(account.get("points", 0) or 0) + gained
            tx.put_account(union_openid, account)
    if gained > 0:
        _log.info(f"[身份] 老账户积分合并 -> {union_openid[:8]}... +{gained}")


def _queue_pending_reward(group_openid: str, member_openid: str) -> None:
    """拉群时还不知道操作者的 union：先挂账，等他在群里 @我 后入账"""
    with _tx() as tx:
        exists = any(
            item.get("group") == group_openid and item.get("member") == member_openid
            for item in tx.pending()
        )
        if not exists:
            tx.add_pending(
                {
                    "group": group_openid,
                    "member": member_openid,
                    "points": GROUP_ADD_REWARD_POINTS,
                    "ts": int(_time_mod.time()),
                    "notified": False,
                }
            )
    _log.info(
        f"[拉群奖励] 待入账 group={group_openid[:8]}... op={member_openid[:8]}..."
    )


async def notify_pending_group_reward(group_openid: str, msg_id: str) -> None:
    """
    群里有人 @我 时：若该群还有未通知的挂账奖励，补发一条领取指引。
    被拉群瞬间机器人无法主动发言（主动消息无权限），只能借被动回复传达。
    """
    with _tx(readonly=True) as tx:
        unnotified = [
            item for item in tx.pending()
            if item.get("group") == group_openid and not item.get("notified")
        ]
    if not unnotified:
        return
    ok = await send_group_msg(
        group_openid,
        (
            f"有邀请奖励待领取喵~刚才拉我进群的邀请者，"
            f"在群里 @我 或发送「签到」即可激活 +{GROUP_ADD_REWARD_POINTS} 积分到账\n"
            f"（要求群人数 ≥{GROUP_ADD_MIN_MEMBERS}，每人每天最多 {GROUP_ADD_DAILY_LIMIT} 次）"
        ),
        msg_id,
    )
    if ok:
        with _tx() as tx:
            tx.mark_pending_notified(unnotified)
        _log.info(
            f"[拉群奖励] 已补发领取指引 group={group_openid[:8]}... 条数={len(unnotified)}"
        )


def _flush_pending_rewards(group_openid: str, member_openid: str, union_openid: str) -> int:
    """该成员在本群首次发言（身份可知）时，把挂账的拉群奖励入账，返回入账积分"""
    account = _account_key(union_openid or member_openid)
    awarded = 0
    with _tx() as tx:
        matched = [
            item for item in tx.pending()
            if item.get("group") == group_openid and item.get("member") == member_openid
        ]
        if not matched:
            return 0
        for item in matched:
            reason, gained = _award_group_add_points(tx, account, str(item.get("group", "")))
            if reason == "ok":
                awarded += int(gained or 0)
        tx.remove_pending(matched)
    return awarded


def resolve_message_identity(event) -> int:
    """
    每条消息到达后调用：学习 openid->union 映射、合并老积分、入账挂账奖励。
    返回本次入账的拉群奖励积分（用于提示用户）。
    """
    scenario_openid = event.user_openid
    union_openid = event.limit_user_id
    if scenario_openid and union_openid and scenario_openid != union_openid:
        learned = _learn_identity(scenario_openid, union_openid)
        if learned:
            _merge_legacy_points(union_openid, learned)
    awarded = 0
    if getattr(event, "group_openid", ""):
        awarded = _flush_pending_rewards(
            event.group_openid, scenario_openid or union_openid, union_openid
        )
        if awarded:
            balance = get_user_points(_account_key(union_openid))
            _log.info(
                f"[拉群奖励] 挂账入账 +{awarded} account={_account_key(union_openid)[:12]}... balance={balance}"
            )
    return awarded


# ====== 机器人在各群的 @ID（全量消息识别用） ======
def bot_mention_ids() -> dict[str, str]:
    with _tx(readonly=True) as tx:
        return tx.mentions()


def is_known_member_openid(openid: str) -> bool:
    with _tx(readonly=True) as tx:
        return tx.is_known_openid(openid)


def learn_bot_mention(group_openid: str, mention_id: str) -> None:
    with _tx() as tx:
        tx.put_mention(group_openid, mention_id)


# ====== QQ 号统一积分账户（平台未下发 union_openid 时的跨场景归一） ======
def _account_key_tx(tx, limit_user_id: str) -> str:
    qq = tx.qq_bind(limit_user_id)
    if qq:
        return f"QQ{qq}"
    return tx.union_of(limit_user_id) or limit_user_id


def _account_key(limit_user_id: str) -> str:
    """统一积分账户键：QQ 绑定 > union 映射 > 场景 openid"""
    with _tx(readonly=True) as tx:
        return _account_key_tx(tx, limit_user_id)


def _account_label(key: str) -> str:
    if key.startswith("QQ") and key[2:].isdigit():
        return f"QQ {key[2:]}"
    return f"{key[:8]}..."


def bind_qq_account(limit_user_id: str, qq: str) -> tuple[bool, str]:
    """
    把当前场景账户并入 QQ 号统一积分账户（余额合并）。
    同一 QQ 允许从多个场景 openid 绑定：同一人换群/私聊共用一个账户。
    """
    qq_key = f"QQ{qq}"
    merged = 0
    with _tx() as tx:
        first_claim = tx.account(qq_key) is None
        if tx.qq_bind(limit_user_id) != qq:
            current_key = _account_key_tx(tx, limit_user_id)
            legacy = tx.account(current_key)
            if isinstance(legacy, dict) and not legacy.get("merged_into"):
                account = tx.account(qq_key) or _new_account()
                merged = int(legacy.get("points", 0) or 0)
                account["points"] = int(account.get("points", 0) or 0) + merged
                account["streak"] = max(
                    int(account.get("streak", 0) or 0), int(legacy.get("streak", 0) or 0)
                )
                account["last"] = max(str(account.get("last", "")), str(legacy.get("last", "")))
                legacy["points"] = 0
                legacy["merged_into"] = qq_key
                tx.put_account(current_key, legacy)
                tx.put_account(qq_key, account)
            tx.put_qq_bind(limit_user_id, qq)
            _log.info(
                f"[QQ绑定] {limit_user_id[:8]}... -> QQ{qq} 合并积分 {merged}"
                + ("（首个绑定）" if first_claim else "（该 QQ 已有账户，本次为并入）")
            )

    balance = get_user_points(qq_key)
    message = (
        f"QQ 绑定成功喵~积分账户已统一为 QQ {qq}\n"
        + (f"原账户 {merged} 积分已并入\n" if merged else "")
        + f"当前积分：{balance}\n"
        f"换群/私聊都是同一个账户；在其他群发一次 /bindqq {qq} 即可把那个群也并进来~"
    )
    return True, message


def unbind_qq_account(target: str) -> bool:
    with _tx() as tx:
        return tx.delete_qq_bind(target)


def qq_bind_of(limit_user_id: str) -> str | None:
    with _tx(readonly=True) as tx:
        return tx.qq_bind(limit_user_id)
