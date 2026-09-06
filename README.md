# Asana → Obsidian Vault · 一次性迁移服务

把 Asana 项目导出为 Obsidian Vault（Markdown + 附件）的网页服务。
**读取一次、生成一次、下载后删除** —— 无账户体系、不做同步、不保存历史版本。

## 用户流程（四步）

1. 点击「连接 Asana」，完成 OAuth（只申请只读权限）；
2. 选择 Workspace 与项目；
3. 选择是否包含：已完成任务 / 评论 / 子任务 / 附件；
4. 生成并下载 `Obsidian-Vault.zip`。

## 隐私承诺（页面上明示）

- 只申请读取所需权限（`tasks:read projects:read attachments:read users:read workspaces:read custom_fields:read`）；
- 不保留项目内容、评论与附件 —— 源数据在任务完成时立即删除；
- ZIP 下载后，临时文件在 **3 小时**内自动销毁；
- OAuth 授权仅在本任务期间加密持有，任务结束即撤销；用户可随时在
  [Asana 授权应用页](https://app.asana.com/0/my-apps)自行撤销；
- 外链附件（Google Drive、Figma 等）默认保留原链接；只有真正托管在 Asana
  的附件才下载进 ZIP。

## 技术架构

```text
OAuth 授权 → 创建导出任务 → 拉取数据与附件
→ 生成 Markdown Vault → 打包 ZIP → 临时下载链接（3 小时）→ 自动清理
```

- **FastAPI + uvicorn**，SQLite（WAL）存会话与任务进度，无外部依赖服务
- 导出在后台线程执行：限流 2 req/s（Asana Free 档 ~150 req/min）、
  429/5xx 指数退避重试、`next_page.offset` 自动翻页 —— 参数与行为
  逐条移植自 `~/ar/bridge` 已验证的 `AsanaService` / `AsanaBackupService`
- Vault 结构与 bridge 一致：`Asana/{Project}/{Section}/{gid} {name}.md`，
  附件集中在 `{Project}/attachments/`，Markdown 以 `../attachments/` 相对引用
  （项目文件夹自包含，可单独解压/移动/多次导出合并）
- OAuth token 以 Fernet 加密落库；发起导出时从会话转入任务，
  任务终态（完成或失败）即调用 Asana revoke 撤销并抹除密文
- 单任务详情/评论/附件任一步失败都降级为占位内容，md 必落盘（bridge 同款）

## 项目结构

```text
app/
├── main.py          # FastAPI 路由：会话/OAuth/项目选择/导出/下载
├── config.py        # 环境变量配置（.env）
├── asana_client.py  # API 客户端（限流+重试+翻页，移植 bridge）
├── oauth.py         # OAuth 授权/换票/刷新/撤销（移植 bridge）
├── vault.py         # Markdown 生成（移植 bridge + 子任务/外链附件）
├── exporter.py      # 导出任务运行器（移植 bridge + token 撤销）
├── store.py         # SQLite：sessions / jobs
├── crypto.py        # Fernet token 加密
├── cleanup.py       # 后台清理线程（3h ZIP / 24h 记录）
└── static/index.html# 四步向导单页
tests/               # 57 个单元/集成测试（FakeClient，不打真实 API）
```

## 本地开发

```bash
uv venv --python 3.12 && uv sync
cp .env.example .env          # 填 SECRET_KEY（python -m app.genkey 生成）
uvicorn app.main:app --port 8600
```

### 开发模式（无 OAuth App 时联调）

`.env` 不填 `ASANA_CLIENT_ID/SECRET`、填入 `ASANA_DEV_TOKEN`（PAT）即可：
「连接 Asana」直接用 PAT 建立会话，页面显示「开发模式」徽标。
正式部署请配置 OAuth（见下），开发模式自动停用。

## 配置 OAuth（正式环境）

1. 打开 <https://app.asana.com/-/developer_console> → 创建 App；
2. Redirect URI 填 `https://你的域名/oauth/callback`；
3. 勾选 scope（必须与 `ASANA_SCOPES` 一致，否则回调报 `forbidden_scopes`）：
   tasks:read / projects:read / attachments:read / users:read / workspaces:read / custom_fields:read；
4. 把 Client ID / Secret 写入 `.env`。

## 部署（Render）

`render.yaml` 已就绪：push 后创建 Web Service，环境变量在 Dashboard 填
`SECRET_KEY` / `ASANA_CLIENT_ID` / `ASANA_CLIENT_SECRET` / `ASANA_REDIRECT_URI`
（`BASE_URL` 设为正式域名）。

## 生产部署（asana.artexbridge.com）

部署在自有服务器（Debian 12）：

- 代码：`/opt/asana_backup`（GitHub 私有库，服务器 deploy key 拉取）
- 运行：systemd `asana-backup.service` → uvicorn `127.0.0.1:8600`（开机自启）
- 入口：nginx 反代 → HTTPS（Certbot，证书自动续期）
- 配置：`/opt/asana_backup/.env`（OAuth 凭据 + `SECRET_KEY`，权限 600，不入库）

更新流程（在部署机上）：

```bash
cd /opt/asana_backup && git pull && uv sync
systemctl restart asana-backup
curl -s http://127.0.0.1:8600/healthz
```

日志：`journalctl -u asana-backup -f`

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `SECRET_KEY` | token 加密密钥（必填） | — |
| `ASANA_CLIENT_ID/SECRET` | OAuth App 凭据 | 空=开发模式 |
| `ASANA_REDIRECT_URI` | OAuth 回调地址 | — |
| `ASANA_DEV_TOKEN` | 开发模式 PAT（仅本地） | 空 |
| `BASE_URL` | 站点地址（决定 cookie secure） | `http://localhost:8600` |
| `DATA_DIR` | SQLite/ZIP/工作目录 | `./data` |
| `ZIP_TTL_HOURS` | ZIP 保存时限 | `3` |
| `JOB_RECORD_TTL_HOURS` | 任务/会话记录保留 | `24` |
| `ASANA_RATE_LIMIT` | 请求限速（req/s） | `2` |
| `MAX_TASKS_PER_EXPORT` 等 | 导出安全上限 | 5000 / 50 / 3 / 200MB |

## 测试

```bash
uv run pytest tests/ -q        # 57 passed（FakeClient，不打真实 API）
uv run ruff check app tests
```

## 与 ~/ar/bridge 的对应关系

| 本服务 | bridge（已验证） |
|--------|------------------|
| `asana_client.py` | `AsanaService`（限流/重试/翻页/opt_fields 原样） |
| `oauth.py` | `AsanaOAuthService`（+ revoke） |
| `vault.py` | `AsanaBackupService::buildMarkdown/toYaml/sanitize/replaceAssetUrls` |
| `exporter.py` | `AsanaBackupService::exportTask/finalizeBackup`（OSS→本地 ZIP） |
| `cleanup.py` | `CleanupExpiredAsanaBackups`（8h→3h，OSS→本地） |

新增（需求要求，bridge 没有）：Workspace 选择步骤、四个导出选项
（已完成/评论/子任务/附件）、外链附件只保留链接、任务终态撤销 OAuth token。
