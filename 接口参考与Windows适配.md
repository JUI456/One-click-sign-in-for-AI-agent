# 接口参考与 Windows 适配（开发者/接手 AI 必读）

> 本文是《配置使用指南.md》（用户向）和《AI一键运行指南.md》（AI 运维向）的进阶篇：
> 记录三个平台**全部上游接口的实测细节与坑**，以及把本项目迁移到 **Windows** 的完整评估。
> 改代码前先读本文，能避免踩一遍已踩过的坑。

---

## 一、上游接口参考（实测于 2026-10-06）

### 1. WorkBuddy（腾讯 CodeBuddy 体系，国内版）

**凭据**：自动读取本机客户端登录文件
`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`
→ 取 `auth.accessToken`（Bearer）、`auth.domain`（决定 base）、`account.uid`。客户端保持登录即自动续期，**零维护**。

**Base URL**：`auth.domain` 含 `workbuddy.ai` → `https://www.workbuddy.ai`；否则 `https://www.workbuddy.cn`

| 接口 | 方法 | 说明 |
|---|---|---|
| `/v2/billing/meter/daily-checkin` | POST `{}` | 签到。`code 0`=成功；`code 10001`=今天已签（幂等） |
| `/v2/billing/meter/checkin-activity-status` | POST `{}` | 连签天数/每日奖励/活动截止（Buddy加油站） |
| `/v2/billing/meter/get-user-resource` | POST `{}` | 资源包明细：`Accounts[]` 里 `CapacityUnit=="credits"` 的才是积分包；`CapacityUsed`/`CapacityRemain`/`CapacitySize`=已用/剩余/总量；`ExpiredTime`(ms) 或 `CycleEndTime`(字符串含秒)=到期 |
| `copilot.tencent.com/v3/config` | GET | 模型列表（glm-5.3-flash、deepseek-v4.1-flash 等） |
| `copilot.tencent.com/v2/chat/completions` | POST | 聊天上游（OpenAI 兼容格式），**只能 `stream:true`**——非流式报 `code 11101`；响应 `usage.credit`=本次扣费 |

**公共请求头**：`Authorization: Bearer <accessToken>`、`X-User-Id: <uid>`、`X-Domain: <domain>`、`User-Agent: CLI/2.63.2 CodeBuddy/2.63.2`

**坑**：
- 逐包扣费**延迟结算**：聊天计费（usage.credit）立刻返回，但落到包的 `CapacityUsed` 要等后端批量结算（分钟级甚至更久），期间 `已用+剩余 ≠ 总量` 属正常；
- 包行会被后端**重排/合并**（例如"已用97"的行结算后消失，出现"已用3"的新行），逐包看趋势即可，**总数始终准确**；
- 无逐笔消耗明细接口（app 内"积分明细"页同样来自 get-user-resource）。

### 2. TRAE（国内版）

**凭据**：`X-Cloudide-Session` Cookie（约 **14 天**有效）。来源三选一（面板「设置」可切换）：
1. **ZCode 内置浏览器自动抓取**（推荐）：`~/Library/Application Support/ZCode/session/Partitions/zcode-embedded-browser/Cookies` 是 SQLite，Cookie **明文存储**，直接读 `name='X-Cloudide-Session'` 的值；
2. Chrome/Safari：被 macOS Keychain 加密，无法程序读取 → 手动模式；
3. 手动粘贴：trae.cn 登录 → F12 → Application → Cookies → 复制。

**Base URL**：`https://api.trae.cn`

| 接口 | 方法 | 说明 |
|---|---|---|
| `/cloudide/api/v3/common/GetUserToken` | POST 空体 | 用 Cookie 换 JWT（`Result.Token`）。401/403=会话过期。JWT 当天有效，每次操作前现换 |
| `/trae/api/v2/ug/checkin_credits/status` | POST `{}` | 签到状态：`checked_in`/`did_checked_in`、`credits`+`extra_credits`=今日到账 |
| `/trae/api/v2/ug/checkin_credits/claim` | POST `{}` | 签到。**只回 `code 0`（无数额）**，到账数额查 status；`code 9074`=风控 → 换随机 16 位数字 `x-device-id` 重试（≤5 次） |
| `/trae/api/v2/pay/user_current_entitlement_list` | POST `{}` | 积分包列表：`usage.credits_amount`=该包已用、`quota.credits_limit`=总量、`display_desc`=官方名（签到奖励/每月登录赠送）、`expire_time`=秒级到期 |

**业务请求头**：`Authorization: Cloud-IDE-JWT <jwt>`、`x-device-id: <16位数字>`、`X-User-Region: cn`、`Referer/Origin: https://www.trae.cn/`

**官方消耗规则**（官方页 tooltip 原文）："系统优先使用快到期积分；到期日期相同时，优先使用 Work 专属和赠送积分。" 已实测验证（FEFO+溢出顺延）。

### 3. Qoder（国内版）

**凭据两层**：
- **PAT**（一年有效，签到+汇总额度必需）：用户在 `qoder.cn/account/integrations` 手动创建，面板「设置」录入；
- **网页会话 Cookie**（约 **7 天**，仅用于逐包明细的可选增强）：`qoder_session_cookie`，从 ZCode 内置浏览器 Cookie 库抓取（同 TRAE 机制）；失效自动退回汇总模式，不影响签到。

**OpenAPI Base**：`https://openapi.qoder.com.cn`（国际版 `openapi.qoder.sh`）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/jobToken/exchange` | POST `{"personal_token":PAT}` | PAT 换 job token（短期） |
| `/sash/api/v1/me/campaigns` | GET | 签到活动。**必须带 `cosy-clienttype: 10`**（桌面端标识）——用默认的 `5` 会静默返回空列表，看起来"没活动"其实是头不对 |
| `/sash/api/v1/me/campaigns/{id}/claim` | POST | 领取。`status=CLAIMED`、`replayed=true`=重复领取 |
| `/api/v2/quota/usage` | GET | 汇总额度：`userQuota`（套餐，体验版为 0）+ `addOnQuota`（签到攒的奖励额度在这里）|

**公共请求头**：`authorization: Bearer <jobToken>`、`cosy-version: 1.0.1`、`cosy-clienttype: 5`（活动接口覆盖为 10）、`user-agent: qoder/1.1.47`

**逐包明细**（网页会话接口，非 openapi）：
`GET https://qoder.cn/api/v2/me/usages/big_model_credits`，带 `Cookie: qoder_session_cookie=<值>`
→ `resource_package_quota.quota_detail[]`：每包 `limit_value/used_value/remaining_value/expires_at(ms)`。**FEFO 实测**：2026-10-06 首次消耗 2 credits，精确落在最早到期的包上。

**聊天接口**：`gateway.qoder.com.cn/algo/...`，需 COSY 私有签名（WASM），脚本无法直接调用——消耗验证只能靠用户在 Qoder 客户端实际使用后面板刷新对比。

### 4. 通用注意事项

- 平台间请求随机间隔 0.8~2s，降低风控风险；
- WorkBuddy 的 status 接口自带 streak 记录，说明签到是平台预期内的正常行为；TRAE/Qoder 同理（官方活动页入口）；
- 所有上游接口均为逆向所得，**路径/参数可能随时变化**，失败时先看面板错误信息里的 HTTP 码与 `code` 字段。

---

## 二、Windows 适配评估（结论：可以，约 90% 代码直接通用）

### 现状盘点

| 模块 | Windows 兼容性 | 说明 |
|---|---|---|
| server.py 主体（HTTP 服务/签到/状态/前端） | ✅ 开箱即用 | 纯 Python 标准库 + pathlib，无 macOS 依赖 |
| Qoder（PAT 签到 + 汇总额度） | ✅ 开箱即用 | 纯 HTTP，凭据手动录入 |
| TRAE 手动模式 | ✅ 开箱即用 | 手动粘贴会话串 |
| TRAE 无感自愈 / Qoder 逐包明细 | ⚠️ 需优雅降级 | 依赖 ZCode 内置浏览器的明文 Cookie 库（macOS/ZCode 专属）。Cookie 库路径不存在时 `grab_*` 已返回 None 自动退回，**但建议加 `sys.platform` 分支并在 UI 上隐藏这两个"自动"选项** |
| WorkBuddy 凭据自动读取 | ❌ 需适配 | 路径不同 + 可能加密（见下） |
| open-panel.command / launchd plist | ❌ macOS 专属 | 换 `.bat`/`.ps1` + 任务计划程序 |

### Windows 上要改的四件事

1. **WorkBuddy 凭据路径与解密**（工作量最大的一块）
   - Windows 路径：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`
   - macOS 上该文件是明文 JSON；**Windows 上字段可能被 Electron safeStorage（DPAPI）加密**。参考 `dingminhua/dsh-connect-workbuddy` 的做法：设置 `ELECTRON_RUN_AS_NODE=1` 调用 WorkBuddy/CodeBuddy 应用的 Node 原生模块解密。
   - 建议先实测：如果 Windows 上该文件也是明文，只需加个路径分支（半天内完成）；若加密则需实现 DPAPI 解密（再加 0.5~1 天）。
2. **TRAE 会话自动化**（可选）
   - Windows 上用「手动模式」即可（已支持，14 天一换）；
   - 若想做到无感：参考 `star620/TRAE-Automatic-sign-in`（C#/WPF，WebView2 内嵌 trae.cn 登录后读 Cookie）——它本身就是 Windows 项目，思路可直接搬。
3. **定时任务**：launchd → 任务计划程序
   ```bat
   schtasks /create /tn "CheckinPanel" /tr "\"C:\Python312\python.exe\" \"C:\checkin-panel\server.py\" --checkin-now" /sc daily /st 09:30
   ```
   常驻服务建议用 NSSM 包装，或放 shell:startup。
4. **杂项**
   - `open-panel.command` 是 macOS 双击脚本 → 写个等价的 `open-panel.bat`；
   - launchd 相关代码块在 Windows 上跳过即可（不报错）；
   - 手机局域网访问逻辑通用（0.0.0.0 监听已实现）。

### 工作量估计

核心功能（签到+面板+Qoder+TRAE 手动模式）**半天内可跑通**；WorkBuddy 凭据自动读取视加密情况追加 0.5~1 天。

---

## 三、给接手 AI 的维护注意事项（踩过的坑）

1. **重启服务的正确姿势**：先 `ps aux | grep server.py` 查 PID 再 `kill <PID>`。**不要** `pkill -f 'python3 server.py'`——homebrew 的进程名是大写 `Python`，pkill 匹配不到，旧进程不死 → 新实例 `EADDRINUSE` 静默崩溃 → 请求一直打到旧代码（表现为"改了没生效"）。
2. **本仓库受 Mimosa 安全扫描保护**：用 Bash heredoc 直接写 `server.py`/配置文件会被 PreToolUse 钩子拦截，**必须用 Write/Edit 工具**改代码。
3. **index.html 的样式近期由用户与其他 AI 维护**（消耗占比卡、轻量/常驻切换按钮等），改样式前先确认现状，避免覆盖别人的改动。
4. `config.json`（含 TRAE 会话串、Qoder PAT）与 `history.json` 已 chmod 600，**不要提交到 git、不要粘贴到对话外**。
5. 改完 `server.py` 后验证是否真正生效：`curl -s http://127.0.0.1:8787/api/status | grep <新字段名>`——别只看进程活着。
6. 历史记录 `history.json` 有去重逻辑（与上一条结果完全相同则不追加），前端按天汇总展示；如需清空直接删文件即可，服务会重建。
