# 鲸鱼娘 · 微信 / QQ / 企业微信多端 AI 聊天机器人

一个带"傲娇鲸鱼娘"固定人设的多平台聊天机器人，可接入 **微信（桌面版自动化）**、
**QQ 官方机器人** 与 **企业微信机器人**，支持本地语音识别、群聊监控、关键词触发、
收藏表情、定时主动发言与看门狗防崩溃，开箱即用。

> 人设为「傲娇鲸鱼娘」，已在配置、桌面界面与 Web API 层面**永久锁定**，
> 无法被改成其它角色。

---

## ✨ 功能特性

- **多端接入**：微信桌面版（UI 自动化）、QQ 官方机器人（WebSocket）、企业微信机器人，
  三端共享同一套人设与安全策略，队列 / 上下文 / 去重缓存各自独立、互不干扰。
- **本地语音识别**：内置 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  本地模型（`base`，int8），语音消息先转文字再回复，**语音不上传第三方接口**。
- **群聊监控**：在 WebUI 里增删监控群，运行中即时生效；仅对配置群响应 @/关键词。
- **关键词触发**：内置一组"傲娇鲸鱼娘"主题触发词，可自行增删。
- **收藏表情**：每 N 次成功回复后自动从微信收藏里随机发一个表情（仅限当前聊天窗口，
  不切换窗口）。
- **寒暄秒回**：常见打招呼走本地模板，省掉一轮 AI 往返，响应更快。
- **电脑排查助手**：识别蓝屏、无法联网、磁盘满、驱动异常等常见 Windows 问题，
  给出分步排查命令。
- **定时主动发言**：每天 8:00–20:00 每 2 小时向监控群主动发一次（可配置）。
- **看门狗防崩溃**：`supervisor.py` 检测进程退出 / 心跳停止 / 桥接端口异常，
  按指数退避自动重启。
- **安全机制**：限流、熔断、消息去重、发送超时隔离、错误日志永久追加。

---

## 📋 系统要求

- Windows 10 / 11（64 位）
- Python 3.12（一键脚本会自动安装）
- 已登录并**保持窗口可见**的微信桌面版（微信端功能依赖 UI 自动化）

---

## 🚀 快速开始

```powershell
# 1. 解压项目到固定目录，例如 D:\鲸鱼娘
cd D:\鲸鱼娘

# 2. 一键安装 Python 依赖（自动建 .venv 并装全部依赖）
.\一键安装所有依赖.bat

# 3. 启动（带看门狗，崩溃自动重启）
.\一键启动机器人-防崩溃.bat
```

启动后：

1. 浏览器打开 `http://127.0.0.1:8080/`（WebUI 控制台）。
2. 在页面里填写 **API 地址 / API Key / 模型名称**，点「保存配置」→「测试 API」。
3. 添加要监控的微信群名称，确认微信主窗口可见。

> 也可只启动网页控制台：`.\start-web.bat`；
> 只启动桌面机器人：`.\start.bat`。

---

## ⚙️ 配置说明

所有配置集中在 `config.yaml`（首次运行由
[`config.release.yaml`](./config.release.yaml) 自动生成，也可参考
[`config.example.yaml`](./config.example.yaml)）。
API 密钥建议通过 WebUI 填写——保存后程序会用 Fernet 加密写入，**不建议手写进文件**。

```yaml
ai:
  api_url: "https://你的接口地址/v1/chat/completions"   # 如 DeepSeek / OpenAI 兼容中转
  api_key: ""            # 留空，通过 WebUI 填写（加密存储）
  api_key_encrypted: ""  # 加密后的密钥，程序自动生成
  model: "deepseek-chat" # 接口实际支持的模型名
  temperature: 0.55
  max_tokens: 500

wechat:
  auto_start: true      # 启动后自动连接微信
  backend: auto         # auto / wechatauto / wxauto4
  target_groups: []     # 要监控的微信群名（与微信显示名一致）
  trigger_keywords: []  # 触发关键词
  sensitivity: normal   # low / normal / high（监听群内消息回复的灵敏度）
  sticker:
    enabled: true
    reply_interval: 3   # 每 N 次回复附带一个收藏表情

qq:
  enabled: false        # 需要 QQ 官方机器人时开启
  app_id: ""            # QQ 机器人 app_id（占位，自行填写）
  client_secret: ""     # 通过 WebUI 填写

wecom:
  enabled: false        # 需要企业微信机器人时开启
  bot_id: ""            # 占位，自行填写
  corpid: ""
  corpsecret: ""

asr:
  enabled: true
  model: base           # 本地 faster-whisper 模型，首次运行自动下载（约 140MB）
```

> 人设项 `style.locked_name: tsundere_whale_girl` 已锁定，无需也无法修改。

---

## 🗂 目录结构

```
.
├─ main.py                  # 机器人主程序（微信 + QQ + 企微 三端装配）
├─ supervisor.py            # 看门狗：崩溃 / 无响应自动重启
├─ web_main.py              # WebUI 控制台入口
├─ ocr_worker.py            # OCR 兼容模式子进程
├─ config.yaml              # 主配置（密钥加密存储）
├─ 一键安装所有依赖.bat       # 自动装 Python + 依赖
├─ 一键启动机器人-防崩溃.bat   # 带看门狗启动
├─ src/
│  ├─ wechat_bridge.py      # 微信桌面自动化桥接
│  ├─ qq_bridge.py          # QQ 官方机器人桥接
│  ├─ wecom_bridge.py       # 企业微信桥接
│  ├─ message_handler.py    # 消息管线：触发/去重/限流/回复
│  ├─ ai_engine.py          # AI 调用
│  ├─ asr_service.py        # 本地语音识别
│  ├─ style_manager.py      # 固定人设（锁定）
│  ├─ security.py          # 限流/熔断/去重
│  └─ web/                 # WebUI（FastAPI + WebSocket）
├─ styles/                  # 人设风格定义
├─ data/
│  └─ models/faster-whisper/base/   # 本地语音识别模型（首次运行自动下载）
└─ logs/                    # 运行 / 错误 / 看门狗日志
```

---

## 🖥 WebUI 控制台

`http://127.0.0.1:8080/` 提供：

- 保存 / 测试 AI 接口（API 地址、Key、模型、接口类型）
- 添加 / 删除监控群
- 查看微信 / QQ / 企微 连接状态、Token、队列、限流熔断
- 实时消息与回复事件流
- 清除历史对话上下文

---

## 🔒 安全与合规

- **不采集聊天记录**，上下文只保存在本地 `data/`。
- **API Key 加密存储**（Fernet），发布包默认不含真实密钥。
- 微信 / QQ / 企业微信均为**第三方平台自动化**，存在账号风控风险：
  建议使用测试账号，控制回复频率，避免短时间大量发送。
- 本项目仅面向 Windows 10/11 64 位，未验证其它系统。

---

## 📄 更多文档

- [`先看我-安装使用说明书.txt`](./先看我-安装使用说明书.txt) — 完整安装与使用
- [`AI自动部署说明.txt`](./AI自动部署说明.txt) — 供 AI / 技术人员自动部署
- [`功能与关键词.txt`](./功能与关键词.txt) — 功能与触发词总表
- [`LOCAL_DEPLOYMENT.md`](./LOCAL_DEPLOYMENT.md) — 本地部署参考
