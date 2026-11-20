"""
趣味功能：运势、天气、今日人品（官方 bot 只用这三个）。
签到/表情包/点赞等 NapCat 时代的功能已移到存档分支 archive/legacy-2026-11-20。
"""

import json
import random
import hashlib
import asyncio
import logging
from datetime import date

_log = logging.getLogger("QQBot")


def get_fortune() -> str:
    """每日运势/抽签"""
    # 运势等级
    fortunes = [
        ("大吉", [
            "今天运气超好，做什么都会很顺利！",
            "幸运之神降临，好事接连发生~",
            "今天是被偏爱的一天，尽情享受吧！",
        ]),
        ("中吉", [
            "运气不错，适合尝试新事物~",
            "今天会有小惊喜在等着你！",
            "心想事成的一天，加油哦~",
        ]),
        ("小吉", [
            "平稳的一天，适合好好学习和工作~",
            "虽然没有大运，但也很安心呢~",
            "踏踏实实就好，会有收获的~",
        ]),
        ("末吉", [
            "运气一般般，不要太冒险哦~",
            "今天适合低调一些，稳中求进~",
            "小心行事，也能安然度过~",
        ]),
        ("凶", [
            "今天要小心一些，注意安全哦~",
            "可能会遇到一些小麻烦，冷静处理就好~",
            "不太顺利的一天，早点休息吧~",
        ]),
    ]

    # 幸运相关
    lucky_colors = ["红色", "蓝色", "绿色", "紫色", "粉色", "白色", "金色", "橙色"]
    lucky_numbers = list(range(0, 100))
    lucky_directions = ["东", "南", "西", "北", "东南", "东北", "西南", "西北"]
    lucky_foods = ["拉面", "火锅", "寿司", "烤肉", "奶茶", "蛋糕", "炸鸡", "水果", "巧克力", "冰淇淋"]

    # 加权随机（大吉和凶的概率低一些）
    weights = [10, 25, 35, 20, 10]
    fortune_level, messages = random.choices(fortunes, weights=weights, k=1)[0]

    result = f"今日运势：【{fortune_level}】\n"
    result += f"{random.choice(messages)}\n"
    result += f"幸运颜色：{random.choice(lucky_colors)}\n"
    result += f"幸运数字：{random.choice(lucky_numbers)}\n"
    result += f"幸运方位：{random.choice(lucky_directions)}\n"
    result += f"幸运美食：{random.choice(lucky_foods)}"

    return result


async def get_weather(city: str) -> str:
    """查询天气"""
    try:
        # 使用 wttr.in 免费天气API
        url = f"https://wttr.in/{city}?format=j1&lang=zh"
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--noproxy", "*", "--max-time", "10",
            "-H", "User-Agent: Mozilla/5.0",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        raw = stdout.decode("utf-8", errors="replace").strip()

        if not raw:
            return f"获取{city}天气失败了，请稍后再试~"

        data = json.loads(raw)

        current = data.get("current_condition", [{}])[0]
        weather_desc = current.get("lang_zh", [{}])
        if weather_desc:
            desc = weather_desc[0].get("value", "未知")
        else:
            desc = current.get("weatherDesc", [{}])[0].get("value", "未知")

        temp = current.get("temp_C", "?")
        feels_like = current.get("FeelsLikeC", "?")
        humidity = current.get("humidity", "?")
        wind_speed = current.get("windspeedKmph", "?")

        # 今日预报
        forecast = data.get("weather", [{}])[0]
        max_temp = forecast.get("maxtempC", "?")
        min_temp = forecast.get("mintempC", "?")

        result = f"{city}天气\n"
        result += f"天气：{desc}\n"
        result += f"当前温度：{temp}°C（体感{feels_like}°C）\n"
        result += f"今日温度：{min_temp}°C ~ {max_temp}°C\n"
        result += f"湿度：{humidity}%\n"
        result += f"风速：{wind_speed}km/h"

        return result

    except Exception as e:
        _log.warning(f"天气查询失败: {e}")
        return f"获取{city}天气失败了，请稍后再试~"


def get_jrrp(user_id: str, nickname: str) -> str:
    """
    今日人品
    基于用户ID和日期生成每日固定的人品值 (1-100)
    不同区间返回不同的回复
    """
    today = date.today().isoformat()
    # 用 user_id + 日期做哈希，保证同一天同一用户结果一致
    seed_str = f"jrrp_{user_id}_{today}"
    hash_val = int(hashlib.md5(seed_str.encode()).hexdigest(), 16)
    rp_value = (hash_val % 100) + 1  # 1-100

    # 区间 1-40：运气不太好
    if rp_value <= 40:
        bad_comments = [
            f"呜...今天还是待在家里比较好喵~",
            f"今天可能会有点倒霉，小心行事喵~",
            f"运气不太行的一天，但是曦曦会一直陪着你喵！",
            f"今天不太适合冒险，低调一点比较好喵~",
            f"虽然运气差了点，但心态好就没问题喵！",
            f"今天出门记得带伞...以防万一喵~",
            f"别灰心！明天一定会更好的喵~",
            f"今天适合躺平摸鱼，不要给自己太大压力喵~",
        ]
        comment = random.Random(hash_val).choice(bad_comments)
        emoji = "😿"

    # 区间 41-70：运气一般
    elif rp_value <= 70:
        mid_comments = [
            f"平平淡淡才是真，今天会很安稳喵~",
            f"中规中矩的一天，适合做日常的事情喵~",
            f"不好不坏，但说不定会有小惊喜喵~",
            f"今天的运气刚刚好，不会出什么大问题喵~",
            f"稳稳当当的一天，适合学习和工作喵~",
            f"虽然不是最好的，但也绝对不差喵！",
            f"今天适合和朋友聊聊天，放松一下喵~",
            f"保持平常心，好运自然来喵~",
        ]
        comment = random.Random(hash_val).choice(mid_comments)
        emoji = "😺"

    # 区间 71-100：运气很好
    else:
        good_comments = [
            f"今天运气超好！做什么都会很顺利喵！",
            f"幸运之星降临！赶紧去买彩票喵！",
            f"哇塞！人品爆发的一天，尽情享受吧喵~",
            f"今天适合表白/告白，成功率超高喵！",
            f"好运挡都挡不住！今天是你的主场喵！",
            f"锦鲤本鲤就是你！今天万事大吉喵~",
            f"羡慕！今天的你简直是天选之人喵！",
            f"运气爆棚！今天做什么都事半功倍喵~",
        ]
        comment = random.Random(hash_val).choice(good_comments)
        emoji = "😻"

    result = (
        f"{emoji} {nickname} 的今日人品 {emoji}\n"
        f"━━━━━━━━━━━━━━\n"
        f"人品值：{rp_value} / 100\n"
        f"{'█' * (rp_value // 5)}{'░' * (20 - rp_value // 5)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{comment}"
    )
    return result

