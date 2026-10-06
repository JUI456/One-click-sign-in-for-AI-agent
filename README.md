# AI 积分签到面板（WorkBuddy / TRAE / Qoder）

本地 Web 面板：一个页面一键签到三个平台，显示各平台剩余积分、资源包明细和到期时间。

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

### 方式一：每天 09:30 自动签到（连网页都不用开）

```bash
# 自带的 plist 写死了原作者的用户名和 python 路径，换电脑请用下面命令按本机重新生成
PY=$(command -v python3)
sed -e "s#/Users/honghonghuan/.zcode/workspace/default/checkin-panel#$PROJECT_DIR#g" \
    -e "s#/opt/homebrew/bin/python3#$PY#g" \
    com.user.checkin-panel.plist > ~/Library/LaunchAgents/com.user.checkin-panel.plist
# 立刻生效（新版 macOS 用 bootstrap；load 可能报 I/O error）
UID=$(id -u)
launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/com.user.checkin-panel.plist 2>/dev/null || echo "请在本机终端执行，或注销后自动生效"
# 查看日志
cat /tmp/checkin-panel.log
# 卸载
launchctl bootout "gui/$UID/com.user.checkin-panel" 2>/dev/null
rm ~/Library/LaunchAgents/com.user.checkin-panel.plist
```

### 方式二：服务开机自启（登录后网页永远可访问，不用手动起服务）

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

> 小提示：plist 放进 `~/Library/LaunchAgents/` 后，**下次登录会自动加载**，不手动 bootstrap 也行（直接注销/重启一次最省事）。

另外：打开面板页面时，如有平台当天未签到会**自动补签**。

## 文件说明

- `server.py` — 面板服务（纯 Python 标准库，无依赖），只监听 127.0.0.1
- `index.html` — 前端页面
- `config.json` — 凭据保存处（权限 600，仅本机可读；请勿外传）
- `history.json` — 签到历史；`last_status.json` — 最近一次状态缓存

## ⚠️ 风险提示

这些接口是从各平台客户端逆向得到的非官方接口，平台可能随时调整（参数/路径/风控），届时签到会失败并在面板显示错误信息。自动签到属于个人账号的日常操作，风险总体较低（WorkBuddy 的 status 接口自身就带 streak 记录，说明平台对签到有正常预期），但请知悉：
1. 使用非官方接口理论上违反平台服务条款，存在账号被限制的理论风险；
2. 凭据保存在本机 `config.json`（TRAE 会话串、Qoder PAT），不要把该文件或整个目录发给别人；
3. TRAE 会话串 14 天过期是设计内行为，面板会明确提示重新获取。
