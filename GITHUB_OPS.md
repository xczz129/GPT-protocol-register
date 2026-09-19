# GitHub 运维手册

> 本仓库（GPT-protocol-register）的 git / GitHub 日常操作规范。
> 最后更新：2026-09-20

## 一、仓库信息

| 项 | 值 |
|---|---|
| 远程地址 | `git@github.com:xczz129/GPT-protocol-register.git`（origin） |
| 推送凭证 | 服务器 `~/.ssh/id_ed25519`（公钥已加到 GitHub 账号 SSH keys） |
| API 凭证 | `/root/.github_token`（0600 权限，Fine-grained PAT，仅此仓库 Contents 读写，用于创建 Release） |

## 二、日常提交与推送

```bash
cd /root/full/gpt-register-fp
git add -u                    # 只暂存已跟踪文件的修改
git commit -m '改动主题（括号列要点）'
git push origin master
```

**Commit 风格**（保持既有惯例）：
- 中文一句话说清本次改动的主题
- 多项改动用 `+` 连接，如：`修复全部19项审查中危(收码边界/号码台账/乐观锁) + 接码iCloud凭据补挂`

## 三、版本发布（Release）

每次完成一轮重要修复 / 功能后发版。流程：

```bash
# 1. 确认测试全绿（基线 2326 passed）
.venv/bin/python -m pytest tests/ -q

# 2. 打带注释的 tag（多段 -m 会拼成标题+正文）
git tag -a vX.Y.Z -m 'vX.Y.Z - 版本标题' -m '正文：分类列改动要点。'

# 3. 推送 tag
git push origin vX.Y.Z

# 4. 用 token 创建正式 Release（侧边栏显示的就是它）
.venv/bin/python - <<'EOF'
import json, urllib.request
token = open('/root/.github_token').read().strip()
req = urllib.request.Request(
    'https://api.github.com/repos/xczz129/GPT-protocol-register/releases',
    data=json.dumps({
        'tag_name': 'vX.Y.Z',
        'name': 'vX.Y.Z - 版本标题',
        'body': '### 分类一\n改动点。\n\n### 分类二\n改动点。',
    }).encode(),
    headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
    },
    method='POST',
)
print(json.loads(urllib.request.urlopen(req, timeout=30).read())['html_url'])
EOF
```

**版本号规则**：
- `v主.次.补丁`（如 v1.1.0）
- 每轮审查修复 / 大功能 → 次版本 +1（v1.2.0）
- 小修补 / 文案 → 补丁 +1（v1.1.1）

> 光 push tag 不会出现在 Releases 列表，必须走第 4 步创建 Release 对象。

## 四、安全红线

1. **永不入库的文件**（`.gitignore` 已覆盖，禁止 `git add -f`）：
   - `config.yaml` —— 含 SMSBower / Online / iCloud 网关 / domain_api 全部真实密钥
   - `data/` —— 账号库（SQLite）、邮箱状态、cliproxy 凭据镜像
   - `backups/`、`tokens/`、`cookies/`
2. **新增文件含密钥时**：先补 `.gitignore` 再提交。
3. **提交前的密钥反查**（拿生产真实值搜 git 跟踪文件与全部历史）：
   ```bash
   # 从 config.yaml 提取敏感字段值，逐个执行
   git grep -l '<敏感值>'            # 当前工作区
   git log --all --oneline -S '<敏感值>'   # 全部提交历史
   ```
4. **仓库密钥已 `git filter-repo` 重写一次**（2026-09-20，清除历史中的 domain_api 密码）。
   若再发生泄漏，优先级：
   - ① 轮换该密钥（服务商侧改掉，历史值即作废）
   - ② 必要时重写历史：`.venv/bin/python -m git_filter_repo --replace-text <替换文件> --force`，然后 `git remote add origin <地址>` + `git push --force`
   - 注意：filter-repo 会移除 origin 且改写全部提交 SHA

## 五、历史版本

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0.0 | 2026-09-17 | 邮箱验证码登录（重登+接码）、CF 清障链重写、7 项高危修复 |
| v1.1.0 | 2026-09-19 | 19 项审查中危全清、接码 iCloud 凭据补挂、打野 email 死逻辑修复、git 历史密钥脱敏 |

相关文档：`KNOWN_MEDIUM_ISSUES.md`（19 项中危问题清单，含每项位置/影响/处理结果）。
