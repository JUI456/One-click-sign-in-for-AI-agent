# AI 积分签到面板（WorkBuddy / TRAE / Qoder）

本地 Web 面板：一个页面一键签到三个平台，显示各平台剩余积分、资源包明细、到期时间，以及**今日总消耗积分**与**三平台消耗占比**（基于每日用量基线差值统计）。

## 启动

> 项目路径每台机器不同，下面用 `$PROJECT_DIR` 指代你本机的项目根目录。默认位置通常是 `$HOME/.zcode/workspace/default/checkin-panel`；若不在该处，用 `find ~ -type d -name checkin-panel -path "*zcode*"` 查找，再把 `$PROJECT_DIR` 换成真实路径。

```bash
cd "$PROJECT_DIR"   # 或 cd 进你找到的项目目录
python3 server.py
# 打开 http://127.0.0.1:8787
```

## 三个平台的凭据来源

| 平台 | 凭据 | 维护成本 |
|---|---|---|
| WorkBuddy | 自动 | **零维护**：自动读取本机 WorkBuddy 客户端登录凭据（`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`），客户端保持登录即可 |
| TRAE | X-Cloudide-Session | **近乎零维护**：登录失效时卡片出现「🔄 重新登录（自动获取）」按钮 → 点它 → 在弹出页面正常登录 → 面板自动从 ZCode 内置浏览器的会话存储拿最新凭据（`~/Library/Application Support/ZCode/session/Partitions/zcode-embedded-browser/Cookies`），无需复制粘贴。每天 09:30 的定时签到也会先尝试无感自愈 |
| Qoder | PAT | **一年一次**：令牌失效时卡片出现「🔗 打开令牌页面」→ 新建令牌 → 复制 → 「手动录入」粘贴（`qoder.cn/account/integrations`） |

手动录入入口在「⚙️ 设置」，仅作为自动获取失败时的备用通道。

## 自动签到（每天定时，无需开面板）

> ⚠️ **激活方式（实测）**：在本机 macOS 上，`launchctl bootstrap` 与旧版 `launchctl load` 把任务注册到 `gui/$UID` 都会报 `Bootstrap failed: 5: Input/output error`（即使 plist 经 `plutil -lint` 校验合法、权限正常）。**不要依赖 bootstrap**——把 plist 放进 `~/Library/LaunchAgents/` 后，**注销并重新登录（或重启）一次**，macOS 会自动加载，这是最可靠的方式（zsh 里 `$UID` 是只读变量，`UID=$(id -u)` 报错不影响，bootstrap 用的是内置 `$UID`）。

### 方式一：每天 09:30 自动签到（连网页都不用开）

```bash
# 自带的 plist 写死了原作者的用户名和 python 路径，换电脑请用下面命令按本机重新生成
PY=$(command -v python3)
sed -e "s#/Users/honghonghuan/.zcode/workspace/default/checkin-panel#$PROJECT_DIR#g" \
    -e "s#/opt/homebrew/bin/python3#$PY#g" \
    com.user.checkin-panel.plist > ~/Library/LaunchAgents/com.user.checkin-panel.plist
# 立即激活（若 bootstrap 报 I/O error，注销/重启一次即可，LaunchAgents 会自动加载）
UID=$(id -u)
launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/com.user.checkin-panel.plist 2>/dev/null || echo "请在本机终端执行，或注销后自动生效"
# 查看日志
cat /tmp/checkin-panel.log
# 卸载
launchctl bootout "gui/$UID/com.user.checkin-panel" 2>/dev/null
rm ~/Library/LaunchAgents/com.user.checkin-panel.plist
```

### 默认·轻量模式：只在签到前后短暂运行，平时不常驻

面板服务**默认不再常驻后台**（解决“一直挂后台费电/占资源”的顾虑）。两种触发方式：

1. **按需打开（推荐，一键）**：双击项目里的 `open-panel.command`（macOS 会在终端里运行它）。它会：若面板已在运行就直接打开浏览器；否则以「空闲 30 分钟自动关闭」启动并打开浏览器。关闭网页约 30 分钟后服务自动退出，几乎不占资源、不耗电。
2. **每日签到后短暂自启**：安装下面的轻量 plist，每天 09:32 自动起一次服务（`--idle-shutdown 60`，约 1 小时后自动关），方便你早上看一眼当天签到结果。

```bash
# 轻量版服务 plist（无 KeepAlive、无 RunAtLoad → 不常驻，仅每日 09:32 短暂启动）
PY=$(command -v python3)
sed -e "s#/Users/honghonghuan/.zcode/workspace/default/checkin-panel#$PROJECT_DIR#g" \
    -e "s#/opt/homebrew/bin/python3#$PY#g" \
    com.user.checkin-panel.server.plist > ~/Library/LaunchAgents/com.user.checkin-panel.server.plist
UID=$(id -u)
launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/com.user.checkin-panel.server.plist 2>/dev/null || echo "请在本机终端执行，或注销后自动生效"
```

> 小提示：plist 放进 `~/Library/LaunchAgents/` 后，**下次登录会自动加载**，不手动 bootstrap 也行（直接注销/重启一次最省事）。若 `bootstrap` 报 `Bootstrap failed: 5: Input/output error`，忽略即可，注销/重启后 LaunchAgents 仍会自动加载。

### 一键开关（开 / 关）

- **开**：双击 `open-panel.command`（任意时刻都能起，轻量模式）。
- **关**：在网页里点右上角「⏻ 停止服务」，或直接关闭网页（空闲 30 分钟后自动关）。

> 注：「⏻ 停止服务」按钮**仅对轻量模式有效**。若处于常驻模式（plist 带 `KeepAlive`），服务停止后会被 launchd 立即重启，按钮会提示“仍被自动重启”；彻底停止常驻需先关闭/移除该 plist 再注销重启。另外，按钮依赖后端 `/api/shutdown` 接口，需服务器加载了含该接口的新代码（注销/重启后）才生效——旧代码上点它会提示“停止失败”。

### 可选·常驻模式：开机自启、随时可访问

若你更想要“网页永远开着就能访问”，把上面的轻量 plist 换成常驻版（加回 `RunAtLoad` + `KeepAlive`）：

```bash
PY=$(command -v python3)
cat > ~/Library/LaunchAgents/com.user.checkin-panel.server.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.user.checkin-panel.server</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$PROJECT_DIR/server.py</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/checkin-panel-server.log</string>
  <key>StandardErrorPath</key><string>/tmp/checkin-panel-server.log</string>
</dict></plist>
EOF
UID=$(id -u)
launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/com.user.checkin-panel.server.plist 2>/dev/null || echo "请在本机终端执行，或注销后自动生效"
```

> 从旧版（常驻）切换到轻量：先把轻量 plist 覆盖到 `~/Library/LaunchAgents/`，再注销/重启一次让新配置生效；当前已在跑的常驻进程可点网页「⏻ 停止服务」立即关掉。

另外：打开面板页面时，如有平台当天未签到会**自动补签**。

## 文件说明

- `server.py` — 面板服务（纯 Python 标准库，无依赖），只监听 127.0.0.1。运行模式：`--idle-shutdown N`（轻量，空闲 N 分钟自动关）、`--resident`（常驻）、`--open`（启动并开浏览器）、`--checkin-now`（仅签到后退出）
- `index.html` — 前端页面
- `open-panel.command` — 一键打开面板的启动脚本（macOS 双击即用，轻量模式）
- `link.svg` — 页面 favicon（链接图标；由原 `苹果.svg` 替换而来，服务端仅白名单放行此文件，避免泄露 `config.json` 等同目录密钥）
- `config.json` — 凭据保存处（权限 600，仅本机可读；请勿外传）
- `history.json` — 签到历史；`last_status.json` — 最近一次状态缓存；`daily_baseline.json` — 每日消耗基线（用于统计今日总消耗，运行时生成）

## ⚠️ 风险提示

这些接口是从各平台客户端逆向得到的非官方接口，平台可能随时调整（参数/路径/风控），届时签到会失败并在面板显示错误信息。自动签到属于个人账号的日常操作，风险总体较低（WorkBuddy 的 status 接口自身就带 streak 记录，说明平台对签到有正常预期），但请知悉：
1. 使用非官方接口理论上违反平台服务条款，存在账号被限制的理论风险；
2. 凭据保存在本机 `config.json`（TRAE 会话串、Qoder PAT），不要把该文件或整个目录发给别人；
3. TRAE 会话串 14 天过期是设计内行为，面板会明确提示重新获取。
