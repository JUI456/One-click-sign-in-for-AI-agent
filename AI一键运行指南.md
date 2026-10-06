# AI 一键运行指南（运维 Runbook）

> 本文档面向 **AI 助手 / 自动化脚本**，目标：无需人工介入，按步骤把 checkin-panel 跑起来并完成一次签到。
> 凡是「AI 应执行」的动作一律用代码块给出**可直接执行**的命令；凡是「判定条件」都给了**预期输出**便于自动分支。

**项目路径不固定**：不同用户的用户名、zcode 工作区位置都可能不同，本指南一律用变量 `$PROJECT_DIR` 引用项目根目录，并优先通过下面的「自动定位」拿到真实路径，不写死任何用户名。

---

## 0. 前置约定

- 仅 macOS 运行；凭据来自本机 WorkBuddy 客户端与 ZCode 内置浏览器，本机已登录过则无需录入。
- 所有操作都基于 `$PROJECT_DIR` 变量，不依赖当前工作目录，也不写死用户名。
- 端口默认 `8787`；若被占用改用 `--port <新端口>`。

### 0.1 定位项目路径（第一步，必须）

```bash
# 默认约定位置（绝大多数情况）
PROJECT_DIR="$HOME/.zcode/workspace/default/checkin-panel"

# 如果默认位置不存在，自动在整个 Home 下查找名为 checkin-panel 的项目目录
if [ ! -d "$PROJECT_DIR" ]; then
  PROJECT_DIR=$(find "$HOME" -type d -name checkin-panel -path "*zcode*" 2>/dev/null | head -1)
fi

# 仍找不到就报错退出，避免后续在错误目录乱跑
if [ -z "$PROJECT_DIR" ] || [ ! -d "$PROJECT_DIR" ]; then
  echo "未找到 checkin-panel 项目，请先确认项目已克隆/放置到本机" >&2
  exit 1
fi
echo "项目路径: $PROJECT_DIR"
```

> 后续所有命令里的 `$PROJECT_DIR` 都来自这一步；若你已 `cd` 进项目目录，也可直接 `PROJECT_DIR="$(pwd)"`。

---

## 1. 环境自检（必做）

```bash
python3 --version
test -f "$PROJECT_DIR"/server.py && echo "project OK" || echo "project MISSING"
```

判定：
- `python3 --version` 需 ≥ 3.8（标准库即可，无需 pip 安装任何包）。
- 输出 `project OK` 才算路径正确。

---

## 2. 启动服务

```bash
cd "$PROJECT_DIR"
nohup python3 server.py > server.log 2>&1 &
echo $! > server.pid
sleep 1.5
```

> 默认常驻。若要**轻量模式**（空闲自动关、平时不占资源、不耗电），改用：
> `nohup python3 server.py --idle-shutdown 30 > server.log 2>&1 &`
> 轻量模式下，浏览器关闭约 30 分钟后服务自动退出；也可在网页点「⏻ 停止服务」立即关。

判定服务已起：

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/
# 期望输出: 200
```

端口占用时（curl 非 200）：

```bash
# 换端口重跑
nohup python3 server.py --port 9000 > server.log 2>&1 &
echo $! > server.pid
```

之后所有 URL 中的 `8787` 改为实际端口。

---

## 3. 一键跑通：立即签到（核心动作）

方式 A — 命令行直接跑（最简单，推荐）：

```bash
cd "$PROJECT_DIR"
python3 server.py --checkin-now
```

方式 B — 通过已启动的 Web 服务触发（适合服务已常驻时）：

```bash
curl -s -X POST http://127.0.0.1:8787/api/checkin \
  -H "Content-Type: application/json" \
  -d '{"source":"manual"}'
```

只签某个平台：`{"platform":"workbuddy"}` / `"trae"` / `"qoder"`。

判定：
- 命令行方式：终端打印的 JSON 中，每个平台的 `"ok": true` 即成功；`"status": "already"` 表示当天已签过（也算成功）。
- 接口方式：返回 JSON 含 `"results"`，逐平台看 `"ok"`。

---

## 4. 验证状态 / 读结果

```bash
curl -s http://127.0.0.1:8787/api/status | python3 -m json.tool
curl -s http://127.0.0.1:8787/api/history | python3 -m json.tool
```

`/api/status` 各平台字段说明（AI 据此判断是否需要人工）：

| 字段 | 含义 |
|---|---|
| `configured` | `false` 表示该平台还需配置（WB 看客户端登录，Qoder 看 PAT） |
| `needs_relogin` | `true` 表示 TRAE 会话过期，需走第 5 节自愈 |
| `needs_pat` | `true` 表示 Qoder 缺 PAT |
| `checked_in_today` | 当天是否已签 |
| `ok` | 本次拉取是否成功 |
| `mode` | 运行模式：`{"idle_timeout": 秒, "resident": true/false}`。`idle_timeout>0` 为轻量模式（空闲自动关），`resident:true` 为常驻 |

---

## 5. 凭据缺失 / 过期的自动处理

### 5.1 TRAE 会话过期（needs_relogin=true）
面板内置「无感自愈」：从 ZCode 内置浏览器 Cookie 自动拿新会话。

```bash
curl -s -X POST http://127.0.0.1:8787/api/grab_trae
# 期望: {"ok": true, "message": "已自动获取 TRAE 最新登录 ✓"}
```

- 若返回 `ok:false`：说明本机 ZCode 内置浏览器从未登录过 trae.cn。此时**无法全自动**，需提示用户在面板 TRAE 卡片点「🔄 重新登录（自动获取）」手动登录一次。
- 自愈成功后，再执行第 3 节重新签到即可。

### 5.2 Qoder 缺 PAT（needs_pat=true）
PAT 必须人工创建（安全令牌，AI 不能代填）：提示用户打开 `https://qoder.cn/account/integrations` → 新建令牌 → 在「⚙️ 设置」粘贴保存。
如需用 API 写入（令牌已拿到时）：

```bash
curl -s -X POST http://127.0.0.1:8787/api/config \
  -H "Content-Type: application/json" \
  -d '{"qoder_pat":"<粘贴的PAT>","qoder_region":"china"}'
```

### 5.3 WorkBuddy 未配置
无需填令牌。检查本机客户端是否登录：

```bash
test -f "$HOME/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info" \
  && echo "WB client logged in" || echo "WB client NOT logged in"
```

显示 `NOT logged in` → 提示用户先登录 WorkBuddy 客户端再刷新面板。

---

## 6. 配置写入（API 速查）

`POST /api/config` 接受字段（均为字符串）：

| 字段 | 说明 |
|---|---|
| `trae_session` | TRAE 会话串（备用，正常走自动获取） |
| `qoder_pat` | Qoder 个人访问令牌 |
| `qoder_region` | `china`（默认）或 `global` |
| `auto_checkin_on_open` | `true`/`false`，打开页面是否自动补签 |
| `clear_trae_session` | 传 `true` 清空 TRAE 会话 |
| `clear_qoder_pat` | 传 `true` 清空 Qoder PAT |

示例（设置 region 并关闭自动补签）：

```bash
curl -s -X POST http://127.0.0.1:8787/api/config \
  -H "Content-Type: application/json" \
  -d '{"qoder_region":"china","auto_checkin_on_open":false}'
```

新增端点（与轻量模式相关）：

| 端点 | 说明 |
|---|---|
| `POST /api/shutdown` | 优雅停止服务（轻量模式的“关”开关；停止后需重新启动才能访问） |
| `GET /api/info` | 返回 `{"idle_timeout": 秒, "resident": bool, "uptime": 秒}` |
| `/api/status` 的 `mode` 字段 | 同上 `idle_timeout` / `resident`，便于前端展示当前模式 |

---

## 7. 安装自动签到 + 开机自启服务（推荐，设一次彻底免操作）

安装两个 launchd 任务（**默认轻量模式，不常驻**）：

1. **每日 09:30 自动签到**：到点自动跑 `server.py --checkin-now`，连网页都不用开。
2. **服务每天 09:32 轻量自启**：自动起一次服务（`--idle-shutdown 60`，约 1 小时后自动关），方便早上看一眼当天结果；平时不在后台常驻。

> ⚠️ 项目自带的 `com.user.checkin-panel.plist` / `com.user.checkin-panel.server.plist` 写死了原作者的用户名和 python 路径，**不要直接 `cp`**。下面脚本按当前机器动态生成。
> **激活方式（实测）**：在本机 macOS 上，`launchctl bootstrap` 与 `launchctl load` 注册到 `gui/$UID` 都会报 `Bootstrap failed: 5: Input/output error`（即使 plist 经 `plutil -lint` 校验合法）。**不要依赖 bootstrap**——把 plist 放进 `~/Library/LaunchAgents/` 后，**注销并重新登录（或重启）一次**，macOS 会自动加载，最可靠。下面的 bootstrap 命令仅在你本机不报该错误时才需要。

```bash
PY=$(command -v python3)
AGENTS="$HOME/Library/LaunchAgents"
PLIST_SRC="$PROJECT_DIR/com.user.checkin-panel.plist"
SERVER_SRC="$PROJECT_DIR/com.user.checkin-panel.server.plist"
CHECKIN_DST="$AGENTS/com.user.checkin-panel.plist"
SERVER_DST="$AGENTS/com.user.checkin-panel.server.plist"

# 1) 每日自动签到 plist（基于项目模板替换本机路径）
sed -e "s#/Users/honghonghuan/.zcode/workspace/default/checkin-panel#$PROJECT_DIR#g" \
    -e "s#/opt/homebrew/bin/python3#$PY#g" \
    "$PLIST_SRC" > "$CHECKIN_DST"

# 2) 轻量服务 plist（基于项目模板替换本机路径；无 KeepAlive/RunAtLoad → 仅每日 09:32 短暂启动）
sed -e "s#/Users/honghonghuan/.zcode/workspace/default/checkin-panel#$PROJECT_DIR#g" \
    -e "s#/opt/homebrew/bin/python3#$PY#g" \
    "$SERVER_SRC" > "$SERVER_DST"

# 3) 立即生效（需在本机 GUI 会话的终端里执行；若报 I/O error 就注销/重启一次，plist 会自动加载）
UID=$(id -u)
launchctl bootstrap "gui/$UID" "$CHECKIN_DST" 2>/dev/null || echo "checkin bootstrap 失败：请在本机终端执行，或注销后自动生效"
launchctl bootstrap "gui/$UID" "$SERVER_DST" 2>/dev/null || echo "server bootstrap 失败：请在本机终端执行，或注销后自动生效"
```

> **可选·常驻模式**：若想“网页永远可访问”，把第 2 步改为生成带 `RunAtLoad`+`KeepAlive` 的 plist（见下文）。从常驻切回轻量：覆盖为轻量 plist 后注销/重启，并在网页点「⏻ 停止服务」关掉当前常驻进程。

```bash
# 常驻版服务 plist（RunAtLoad + KeepAlive）
PY=$(command -v python3)
SERVER_DST="$HOME/Library/LaunchAgents/com.user.checkin-panel.server.plist"
cat > "$SERVER_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.user.checkin-panel.server</string>
    <key>ProgramArguments</key><array><string>$PY</string><string>$PROJECT_DIR/server.py</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/checkin-panel-server.log</string>
    <key>StandardErrorPath</key><string>/tmp/checkin-panel-server.log</string>
</dict>
</plist>
EOF
```

校验：

```bash
launchctl list | grep checkin-panel
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/
cat /tmp/checkin-panel.log
```

卸载：

```bash
UID=$(id -u)
launchctl bootout "gui/$UID/com.user.checkin-panel" 2>/dev/null
launchctl bootout "gui/$UID/com.user.checkin-panel.server" 2>/dev/null
rm "$HOME/Library/LaunchAgents/com.user.checkin-panel.plist" \
   "$HOME/Library/LaunchAgents/com.user.checkin-panel.server.plist"
```

---

## 8. 一键跑通的标准流程（AI 照抄即可）

```bash
PROJ="$PROJECT_DIR"
cd "$PROJ"

# 1) 起服务（如已起可跳过）
curl -s -o /dev/null http://127.0.0.1:8787/ || {
  nohup python3 server.py > server.log 2>&1 &
  echo $! > server.pid
  sleep 1.5
}

# 2) 先尝试自动签到
python3 server.py --checkin-now

# 3) 看状态，决定要不要自愈
curl -s http://127.0.0.1:8787/api/status | python3 -m json.tool
```

之后根据 `/api/status` 的 `needs_relogin` / `needs_pat` / `configured` 字段，按第 5 节分支处理；处理完回到第 2 步重签。

---

## 9. 判定「跑通」的终态

满足以下全部即视为一键跑通成功：

1. `server.py` 监听 `127.0.0.1:8787`（或指定端口）返回 HTTP 200；
2. `--checkin-now` 或 `/api/checkin` 对 `workbuddy / trae / qoder` 三个平台均返回 `"ok": true`（含 `already`）；
3. `/api/status` 中三平台 `checked_in_today` 为 `true`（或 `already`）且无明显 `error`。

> 注：若某平台确实未配置（如 Qoder 未录 PAT、WorkBuddy 客户端未登录），属于**用户侧待办**，AI 应给出明确提示而非视为失败。
