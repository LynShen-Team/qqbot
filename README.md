<div align="center">

<a href="https://lynshen.org"><img src="docs/images/lynshen-icon.svg" width="88" alt="LynShen"></a>

# 曦曦官方 Bot

**基于 QQ 开放平台的群聊 / 私聊机器人，带网页管理面板。**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](official_qqbot/requirements.txt)
[![FastAPI](https://img.shields.io/badge/FastAPI-dashboard-009688.svg)](official_qqbot/control_api)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-optional-336791.svg)](#数据存储)

由 [LynShen](https://lynshen.org) 维护

</div>

## 功能

- **日常指令**：签到、积分、排行榜、运势、今日人品、天气、点歌、GitHub 搜索；输错指令会提示最接近的那条。
- **资源领取**：NFA、4399、sauth、163，按积分扣费，带频控、冷却和限额；私聊直接发，群里领取发到绑定的邮箱。
- **AI**：@机器人聊天、`/生图`、网页链接分析；B 站、抖音链接自动解析。
- **积分账户**：每日签到、邀请进群奖励；`/bindqq` 把不同群、私聊的积分合成一个账户。
- **管理**：管理员 / Staff、封禁、广告、限额规则；网页面板看用户、权限、积分与用量、日志，改 AI 设置。

## 工作方式

```text
QQ 开放平台 ──HTTPS 回调──> official-qqbot-api  /qq ──转发──> official-qqbot（127.0.0.1:8765）
                                                                │ 验签后先回包，后台处理
QQ 开放平台 <──WebSocket 网关（可选）──────────────────────────┤
                                                                ▼
                                  指令表 → 积分 / 资源 / AI / 链接解析 → QQ 发消息接口
```

两个进程，都由 systemd 管理：

| 服务 | 作用 |
|---|---|
| `official-qqbot` | 机器人：收事件、跑指令、发消息 |
| `official-qqbot-api` | 管理面板和内部接口（只绑 127.0.0.1），并把公网 `/qq` 回调转给机器人 |

`official_qqbot/` 里按「会一起改的东西」分文件：

| 文件 | 内容 |
|---|---|
| `bot.py` | 启动 |
| `config.py` | 读 `config.yaml` 和环境变量 |
| `ingress.py` | webhook、WebSocket、事件分发、群事件 |
| `messages.py` | 一条消息的处理顺序 |
| `commands.py` / `commands_admin.py` | 指令表 / 管理类指令 |
| `resources.py` | 领资源（NFA / 4399 / sauth / 163 / 库存） |
| `accounts.py` | 积分、签到、邀请奖励、身份归一、QQ 绑定 |
| `qq_api.py` | access token、连接池、发消息 |
| `ads.py`、`mailer.py`、`redact.py` | 广告、邮件、日志脱敏 |
| `control_api/` | 管理面板、数据库模型、迁移工具 |

## 部署（Ubuntu）

```bash
sudo mkdir -p /opt/official_qqbot
sudo cp -a official_qqbot/. /opt/official_qqbot/
cd /opt/official_qqbot
cp -n config.example.yaml config.yaml
bash deploy/ubuntu/install.sh
sudo systemctl enable --now official-qqbot-api official-qqbot
```

配置分两处：

- `config.yaml`：机器人开关、AI 和生图、邮件等普通配置。
- `/etc/official-qqbot/koishi-bridge.env`（只给 root 读，`chmod 600`）：**所有密钥都只放这里**，变量名见 [`koishi-bridge.env.example`](official_qqbot/deploy/ubuntu/koishi-bridge.env.example)。

常用开关：

| 变量 | 说明 |
|---|---|
| `QQ_BRIDGE_ENABLED=1` | 打开回调监听（生产入口） |
| `QQ_WEBHOOK_ASYNC=1` | 回调先回包、后台处理；设 `0` 恢复处理完才回包 |
| `QQ_BIND_VERIFY=off` | `email` 时 `/bindqq` 要先收 QQ 邮箱验证码 |
| `CONTROL_LOG_RETENTION_DAYS=30` | 命令和发送日志保留天数，`0` 不清理 |
| `BOT_STATE_STORE=db` | 积分等状态存数据库；`json` 存 `data/*.json` |

更多说明见 [部署文档](official_qqbot/deploy/ubuntu/README.md)。

## 数据存储

- 管理面板的数据（用户、封禁、邮箱、权限、用量、日志）在 `DATABASE_URL`：SQLite 或 PostgreSQL（`postgresql+psycopg://…`）。
- 机器人自己的状态（积分、身份、QQ 绑定、广告、冷却）由 `BOT_STATE_STORE` 决定：`db` 时和上面同一个库，用事务和行锁，并发扣分不会扣穿。
- 从 SQLite / JSON 搬到 PostgreSQL：

  ```bash
  python -m control_api.migrate_to_db --sqlite data/control.db --data-dir data --target "$DATABASE_URL"
  ```

  源文件只读；复制完逐表核对行数、id 和内容哈希，不一致就返回非 0。

## 隐私与安全

- 仓库里没有任何密钥、口令或内部服务地址，全部从环境变量读。
- 发送日志不保存发出去的账号、密码、Sauth、Token，只记类型和成没成功；命令日志不保存 `/auth` 口令；运行日志里的消息预览同样脱敏。
- QQ 回调逐条验签；管理面板和内部接口只绑本机，内部接口要 bot token，面板要管理密钥。

## 开发与测试

```bash
python -m pip install -r official_qqbot/requirements.txt
python official_qqbot/run_tests.py
```

测试在临时目录里用示例配置跑，不碰真实配置和数据。设 `QQBOT_TEST_DATABASE_URL` 指向一个空的 PostgreSQL 测试库，会另外在 PostgreSQL 上跑存储和迁移测试。
