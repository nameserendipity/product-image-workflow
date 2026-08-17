# Parallel Worktree Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `D:\product-image-workflow` 安全迁移为永久检出本地 `main` 的主工作树，并清理本轮已合并且干净的临时工作树。

**Architecture:** 先在现有 `main` 工作树合入并验证工作树规范文档，再释放 `main-final` 对 `main` 的占用，最后让原始仓库目录检出 `main`。清理动作只针对已被 `main` 包含且状态干净的工作树和分支；`codex/review-fixes` 的未跟踪文件及其工作树保持不变。

**Tech Stack:** Git worktree、PowerShell、Python `unittest`

## Global Constraints

- `D:\product-image-workflow` 最终必须是唯一检出 `main` 的主工作树。
- 不使用 `git reset --hard`、`git checkout --`、强制删除分支或强制移除工作树。
- 不移动、暂存、提交或删除 `D:\product-image-workflow-review-fixes` 中的未跟踪文件。
- 不推送 `origin/main`；远程发布必须另行获得明确授权。
- 任何准备移除的工作树只要出现已修改、已暂存或未跟踪文件，就停止该工作树的清理。
- 共享依赖 Junction 仅作为忽略文件使用；删除 Junction 时不得触碰其目标目录。

---

### Task 1: 将工作树规范文档合入本地 main

**Files:**
- Create: `docs/superpowers/specs/2026-08-17-parallel-development-worktree-policy-design.md`
- Create: `docs/superpowers/plans/2026-08-17-parallel-worktree-migration.md`

**Interfaces:**
- Consumes: 本地 `main` 提交 `2eba76a`；设计分支 `codex/worktree-policy-design`
- Produces: 包含设计和实施计划的本地 `main` 合并提交

- [ ] **Step 1: 核对两个工作树都处于预期分支且干净**

```powershell
git -C D:\product-image-workflow-main-final status --short --branch
git -C D:\product-image-workflow-worktree-policy status --short --branch
git -C D:\product-image-workflow-worktree-policy log -1 --oneline
```

Expected: `main-final` 为干净的 `main`；策略工作树为干净的 `codex/worktree-policy-design`。

- [ ] **Step 2: 确认设计分支只比 main 多文档提交**

```powershell
git -C D:\product-image-workflow-worktree-policy diff --name-status main...HEAD
git -C D:\product-image-workflow-worktree-policy diff --check main...HEAD
```

Expected: 只列出本设计和计划文档，补丁检查退出码为 `0`。

- [ ] **Step 3: 在 main 工作树保留任务边界进行合并**

```powershell
git -C D:\product-image-workflow-main-final merge --no-ff codex/worktree-policy-design -m "merge: adopt parallel worktree policy"
```

Expected: 创建一个只引入两份文档的合并提交；无冲突。

- [ ] **Step 4: 验证合并结果和工作树状态**

```powershell
git -C D:\product-image-workflow-main-final diff --check HEAD^1..HEAD
git -C D:\product-image-workflow-main-final status --short --branch
git -C D:\product-image-workflow-main-final log -1 --oneline --decorate
```

Expected: `diff --check` 通过，`main` 工作树无未提交文件。

### Task 2: 释放 main-final 并让主目录接管 main

**Files:**
- Remove worktree directory: `D:\product-image-workflow-main-final`
- Reassign branch checkout: `D:\product-image-workflow` -> `main`

**Interfaces:**
- Consumes: Task 1 产生的本地 `main`
- Produces: `D:\product-image-workflow` 中唯一检出的 `main`

- [ ] **Step 1: 核对主目录当前任务已进入 main 且工作树干净**

```powershell
git -C D:\product-image-workflow status --short --branch
git -C D:\product-image-workflow merge-base --is-ancestor codex/douyin-direct-replace-batch main
```

Expected: 主目录无未提交文件；祖先检查退出码为 `0`。

- [ ] **Step 2: 检查 main-final 是否存在需要保留的忽略输出**

```powershell
git -C D:\product-image-workflow-main-final status --short --ignored
Get-ChildItem -Force D:\product-image-workflow-main-final\outputs -ErrorAction SilentlyContinue
Get-ChildItem -Force D:\product-image-workflow-main-final\work -ErrorAction SilentlyContinue
Get-ChildItem -Force D:\product-image-workflow-main-final\local_settings.json -ErrorAction SilentlyContinue
```

Expected: 除已知依赖 Junction 或缓存外，不存在用户输出、本地配置或未知文件。若存在，停止移除并报告。

- [ ] **Step 3: 验证共享依赖 Junction 的目标**

```powershell
Get-Item D:\product-image-workflow-main-final\spreadsheet_runtime\node_modules | Select-Object FullName,LinkType,Target
```

Expected: `LinkType` 为 `Junction`，目标为 `D:\product-image-workflow\spreadsheet_runtime\node_modules`。

- [ ] **Step 4: 移除干净的 main-final 工作树**

```powershell
git -C D:\product-image-workflow worktree remove D:\product-image-workflow-main-final
```

Expected: 命令退出码为 `0`；共享依赖目标仍存在。

- [ ] **Step 5: 在主目录检出 main 并设置远程跟踪关系**

```powershell
git -C D:\product-image-workflow switch main
git -C D:\product-image-workflow branch --set-upstream-to=origin/main main
git -C D:\product-image-workflow status --short --branch
```

Expected: 当前分支为 `main`，工作树干净；状态显示本地相对 `origin/main` 的领先数量。

### Task 3: 验证新的 main 主工作树

**Files:**
- Verify only; no tracked file changes

**Interfaces:**
- Consumes: Task 2 中的新主工作树
- Produces: 可复现的主线测试和 Git 状态证据

- [ ] **Step 1: 刷新远程引用并核对 main、远程和工作树关系**

```powershell
git -C D:\product-image-workflow fetch origin main
git -C D:\product-image-workflow branch --show-current
git -C D:\product-image-workflow rev-list --left-right --count origin/main...main
git -C D:\product-image-workflow worktree list --porcelain
```

Expected: 远程引用刷新成功；主目录为 `main`；`main` 不落后于 `origin/main`；没有其他工作树检出 `main`。若获取远程引用失败，继续本地验证，但必须将远程关系标为未刷新。

- [ ] **Step 2: 运行完整 Python 测试**

```powershell
D:\product-image-workflow\.venv\Scripts\python.exe -m unittest discover -v
```

Expected: `Ran 454 tests`，结果为 `OK`。

- [ ] **Step 3: 运行补丁和状态检查**

```powershell
git -C D:\product-image-workflow diff --check
git -C D:\product-image-workflow status --short --branch
```

Expected: 无补丁错误、无未提交文件。

### Task 4: 清理已合并且干净的旧工作树

**Files:**
- Remove worktree directories when clean:
  - `D:\product-image-workflow-integration`
  - `D:\product-image-workflow-cdp-port-fix`
  - `D:\product-image-workflow-kuaishou-parameters`
  - `D:\product-image-workflow-worktree-policy`
- Preserve: `D:\product-image-workflow-review-fixes`

**Interfaces:**
- Consumes: 已验证的本地 `main`
- Produces: 仅保留主工作树和仍有未完成内容的任务工作树

- [ ] **Step 1: 分别核对候选工作树干净且分支已进入 main**

```powershell
git -C D:\product-image-workflow-integration status --short --branch
git -C D:\product-image-workflow-cdp-port-fix status --short --branch
git -C D:\product-image-workflow-kuaishou-parameters status --short --branch
git -C D:\product-image-workflow-worktree-policy status --short --branch
git -C D:\product-image-workflow branch --merged main
```

Expected: 四个候选工作树均无文件状态；四个对应分支均列在已合并分支中。

- [ ] **Step 2: 再次确认 review-fixes 的文件并保持不动**

```powershell
git -C D:\product-image-workflow-review-fixes status --short --branch
```

Expected: 输出仍包含原有未跟踪文件；不执行任何写操作。

- [ ] **Step 3: 逐个移除候选工作树，任一失败即停止对应清理**

```powershell
git -C D:\product-image-workflow worktree remove D:\product-image-workflow-integration
git -C D:\product-image-workflow worktree remove D:\product-image-workflow-cdp-port-fix
git -C D:\product-image-workflow worktree remove D:\product-image-workflow-kuaishou-parameters
git -C D:\product-image-workflow worktree remove D:\product-image-workflow-worktree-policy
```

Expected: 每条命令退出码均为 `0`，不使用 `--force`。

- [ ] **Step 4: 安全删除已经合入 main 的本地临时分支**

```powershell
git -C D:\product-image-workflow branch -d codex/integration-20260817
git -C D:\product-image-workflow branch -d codex/cdp-profile-port-fix
git -C D:\product-image-workflow branch -d codex/kuaishou-parameters
git -C D:\product-image-workflow branch -d codex/worktree-policy-design
git -C D:\product-image-workflow branch -d codex/douyin-direct-replace-batch
git -C D:\product-image-workflow branch -d codex/remove-post-generation-review
git -C D:\product-image-workflow branch -d codex/fix-product-logic-quality-gate
```

Expected: `git branch -d` 只删除 Git 确认已合并的分支；`codex/review-fixes` 保留。

- [ ] **Step 5: 输出最终状态**

```powershell
git -C D:\product-image-workflow status --short --branch
git -C D:\product-image-workflow worktree list --porcelain
git -C D:\product-image-workflow branch --all --verbose --no-abbrev
git -C D:\product-image-workflow-review-fixes status --short --branch
```

Expected: 主目录检出干净的 `main`；只保留主工作树和 `review-fixes` 工作树；远程 `origin/main` 未被修改。
