# OpenList 每日新增同步

唯一来源是 `/wangyuheng`，`/gongyun` 和 `/local` 都是目标：

```text
/wangyuheng/2026/... ─┬─> /gongyun/2026/...
                    └─> /local/2026/...
/wangyuheng/2027/... ─┬─> /gongyun/2027/...
                    └─> /local/2027/...
```

只处理来源中名称为四位数字、且不早于 2026 的年份目录。自动发现新年份，不同步 `Attachments` 等其他根目录，也不会从两个目标反向读取文件来相互同步。

## 同名处理：先选好目录规则

**所有模式都不覆盖、不更新、不删除已有文件**，同名但大小或内容不同也跳过。目标中的额外文件保留，文件和文件夹同名时也跳过。

`sync_config.json` 中的 `existing_directories` 决定如何处理同名目录：

| 配置 | 同名文件夹 | 用途 |
| --- | --- | --- |
| `skip`（当前默认） | 整个文件夹跳过，不检查内部 | 严格遵循“文件或文件夹重名就跳过” |
| `recurse` | 进入同名目录，只补内部缺少的文件或子目录 | 支持过去月份中继续补传新文件 |

例如目标已有 `2026/11/`：

- `skip`：源端以后放进 `2026/11/` 的新文件**不会同步**。
- `recurse`：新文件会同步，已有同名文件仍不动。

**年份目录是固定的路由容器，总会进入比较；目录跳过规则从年份下面的月份/其他子目录开始应用。** 否则目标已经有 `2026` 时，该年份将永远不会再补传任何内容。

新目录作为整体提交给 OpenList，由 OpenList 在后台递归复制，Actions 不创建空目录、不逐个等待内部文件。如果选 `skip`，后台复制中途失败后，已创建但未传完的目录也会在后续被跳过。此时在 OpenList 重试失败任务，或临时用 `recurse` 跑一次补齐，再恢复 `skip`。已有同名坏文件或 0 字节文件仍然跳过，不会自动修复。需要日常持续补传的场景，直接选择 `recurse` 更合适。

## 部署到 GitHub Actions

1. 建议创建一个**私有仓库**，将以下文件上传到默认分支：

   ```text
   .github/workflows/sync.yml
   sync_openlist.py
   sync_config.json
   tests/test_sync.py
   .gitignore
   README.md
   ```

   上传时保留隐藏目录 `.github` 的路径。不要上传 `.playwright-mcp`、`.env`、浏览器记录或其他调试文件。

2. 在仓库中打开：

   **Settings → Secrets and variables → Actions → New repository secret**

   - Name：`OPENLIST_TOKEN`
   - Secret：你的 OpenList 密钥（原样填写，不加 `Bearer `）

   密钥不写入源码、配置或 workflow。本项目没有保存你提供的密钥。

3. 到 **Actions → OpenList daily sync → Run workflow**，首次保持 `dry_run` 勾选。

   `existing_directories` 选择 `config` 使用配置，也可临时选 `skip` 或 `recurse`。查看日志中的 `PLAN` 项和运行摘要，确认方向与计划数量。

4. 确认后再次手动运行，取消勾选 `dry_run`，向 OpenList 提交复制任务。此后每天自动提交。

5. 到 **OpenList → 管理 → 任务 → 复制** 查看实际传输进度、失败原因和重试操作。**Actions 绿色只表示本次提交过程成功，不代表文件传输完成。**

日程为 **UTC 19:17，即香港/北京时间次日 03:17**。推送到 `main` 和定时触发默认都是真实同步，不会等待手动预览。初次上传工作流时可能尚未配置 Secret，该次同步会因缺少密钥失败；添加 Secret 后重新运行即可。想先暂停自动运行，可在 Actions 中禁用 workflow。

每次推送到 `main` 都先运行测试，测试通过后提交同步任务，包括只修改 README 的推送；PR 仍只运行单元测试，不进行同步、不读取密钥。无需安装第三方 Python 包。

### “重新运行所有作业”仍跳过 sync？

重新运行会保留原来的事件类型及提交，不会变成 `workflow_dispatch`。旧版工作流只允许 `schedule` 和 `workflow_dispatch`，因此 `push` 事件里的 `sync` 作业始终被跳过。

新版已允许 `push`。请在**新版提交对应的运行记录**中点击“重新运行所有作业”；重跑旧版提交的记录仍可能按旧条件跳过。想直接使用最新配置，请到 Actions → OpenList daily sync → **Run workflow**，选择 `main`；要真正提交复制任务，取消勾选 `dry_run`。

## 运行方式

GitHub Actions 只负责扫描需要同步的目录并提交复制请求，收到接口响应后即可退出，不等待传输、不轮询进度、不逐文件校验大小。实际文件传输发生在 OpenList 服务器及挂载存储之间，**不会把文件下载到 GitHub runner 再上传**。OpenList 主机必须有足够的磁盘空间和带宽，两个目标挂载也必须可写。

- 使用 `/api/fs/copy`，明确设置 `overwrite=false`、`skip_existing=true`、`merge=false`。同一个目录中缺少的文件和子目录通过 `names` 批量提交，每批最多 100 项，而非逐个文件等待传输。
- 目标年份不存在时，直接提交整个年份文件夹。目标年份存在时，只比较其直接子项；`recurse` 模式额外进入已有子目录检查，缺少的新子目录仍作为整体交给服务端复制。
- 一次 HTTP 请求可以创建多个后台任务，OpenList 还可能将目录拆分为更多子任务；并不保证在任务面板中只有一条记录。
- 对已有目录强制刷新列表，分页读取，避免只读第一页或漏掉被缓存的新文件。
- 对两目标分别检查；某个目标失败，仍尝试另一个目标，最终将 workflow 标记失败。
- 日志输出 `SUBMIT` 和服务端返回的任务 ID，摘要统计 `copy_requests_accepted`、`tasks_returned`、`files_submitted`、`directories_submitted`，**不统计“复制完成数”**。目录项代表整个子树，未展开统计内部文件数量。
- 一个时间只运行一组 Actions 提交任务，新运行不会取消旧运行。工作流结束后，OpenList 后台任务仍会继续；再次手动运行前先查看服务端任务，避免上一次尚未创建目录时重复排队。
- 默认脚本最长运行 10 分钟，Actions job 上限 15 分钟。通常耗时取决于目录列表和 API 响应，不再取决于视频传输耗时；文件多、上游限速时依然可能较慢。
- 写请求超时不会直接重试，因为服务端可能已经受理。OpenList 中的后台任务可能继续执行，需先查看其任务面板再手动重跑。
- 工作流不做镜像、不请求删除/移动接口，也没有“更新同名文件”操作。

建议在同步过程中不要由其他客户端同时写同一个目标路径。服务端的 `skip_existing` 提供额外防护，但该 API 的存在检查和实际写入并非跨存储的原子事务。

## 配置

编辑 `sync_config.json`：

```json
{
  "base_url": "https://pan.sykbsm.net",
  "source_root": "/wangyuheng",
  "destination_roots": ["/gongyun", "/local"],
  "start_year": 2026,
  "existing_directories": "skip",
  "request_timeout_seconds": 120,
  "run_timeout_seconds": 600
}
```

不会按“最近两个月”过滤：在 `recurse` 模式下旧月份中补传的新文件也能找到；未变化的已有文件只检查名字，不重新下载。`skip` 模式不扫描同名月份的内部，也不在 Actions 中展开新目录。扫描很多已有目录时仍会消耗存储 API 请求。

本地运行（先通过安全方式设置 `OPENLIST_TOKEN` 环境变量，不要将密钥写进命令历史）：

```bash
python3 -m unittest discover -s tests -v
python3 sync_openlist.py --dry-run
python3 sync_openlist.py
```

## 注意 GitHub 日程限制

工作流必须在默认分支。GitHub 定时任务不是精确时钟，繁忙时可能延迟甚至丢失某次运行；下一次会重新检查目标。公共仓库连续 60 天无活动，定时任务可能自动停用，因此建议私有仓库并定期查看运行记录。私有仓库也应留意账户的 Actions 可用额度。传输已改为服务端后台进行，不再让 runner 等待大文件复制完成。

参考：[GitHub Actions schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[OpenList 文件接口](https://openlistteam.github.io/docs/guide/api/fs.html)、[任务接口](https://openlistteam.github.io/docs/guide/api/task.html)。
