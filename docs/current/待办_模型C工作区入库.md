# 待办：模型 C 工作区入库（"别人 clone 即可上手"）

> 状态：2026-11-21 起立。路线 = **模型 C**（`kharon-manifest` 作清单仓，装 `west.yml` + `products/`；
> `kharon-boards`、`elab-app-common` 各自独立仓；`applications/` 随清单仓走）。
> 已完成的前置见文末"已完成"一节，本清单只列**还没做**的。

勾选顺序建议按编号来：1 → 2 → 3 → 4 → 5。5 是总验收，必须最后跑。

---

## [ ] 1. 给 `kharon-manifest` 建远端仓

**现状**：只有本地目录 `zephyr-workspace/kharon-manifest/`，里面是 `west.yml`、`products/kharon/*.yaml`、`README.md`，无 `.git`、无 remote。

**动作**

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon\zephyr-workspace\kharon-manifest
git init -b main
git add west.yml README.md products/
git commit -m "manifest: west.yml(import zephyr) + products 登记"
git remote add origin <你的私仓地址>
git push -u origin main
```

**只提交**：`west.yml`、`README.md`、`products/kharon/*.yaml`
**不提交**：`../zephyr/`、`../modules/`、`../build/`、`../.west/`、`../.kharon/`（它们在工作区根，不在本仓范围内）

**验收**：在任意空目录 `git clone <地址> t && cd t && python -m west init -l .` 能列出 71 个项目（manifest + zephyr + import 的 69）。

**依赖**：无。**风险**：`west.yml` 里 `revision` 目前钉的是 commit `25851fa`；若要改成 tag（如 `v4.5.0`）就在这一步一并改（改完与 `zephyr.commit` 保持同一值）。

---

## [ ] 2. 建 `kharon-boards`、`elab-app-common` 两个仓，并启用清单里那两段

**现状**：两个目录都在工作区里，能用（靠 `EXTRA_ZEPHYR_MODULES` 注入），但不在任何仓；`west.yml` 里对应两段仍是注释。

**动作**

```powershell
# 各自建仓（目录已存在，直接 init 即可）
cd ..\kharon-boards ; git init -b main ; git add . ; git commit -m "boards: Zephyr module (board_root)"
cd ..\elab-app-common ; git init -b main ; git add . ; git commit -m "app-common: Zephyr module (breath_sync/ota_engine/store)"
# 各自 push 到私仓，然后回到 manifest 仓
```

然后打开 `kharon-manifest/west.yml` 里 `manifest.projects` 的两段注释，填 `url`、`path`、`revision: v0.1.0`。

**附带要核对**：启用后这两个 module 会被 west 直接发现，而 `providers/official/zephyr/west/provider.yaml` 的 `EXTRA_ZEPHYR_MODULES` 仍会再注入一次（`services/kharon/__main__.py:126`）。改成"只留一处"，并跑一次构建确认没有重复注册的告警。

**验收**：新工作区 `west update` 后 ① `west boards` 能看到 `esp32s31_function_coreboard` / `blackpill_f411ce` 等自研板；② 应用侧 `#include "ota_engine.h"` 仍能解析。

**依赖**：需先有 1（清单仓能改才谈得上启用）。**风险**：进了 `projects:` 之后这两个仓归 west 管，本地手改会被 checkout 冲掉——改动必须先 push。

---

## [ ] 3. `kharon-ide` 配 remote

**现状**：`main` 分支、701 个跟踪文件、**无 remote**、还有 69 项未提交改动（cockpit 前端 + docs 等）。

**动作**

1. 先把 69 项改动按主题分批提交（至少拆成：cockpit UI、docs、其它）；
2. `git remote add origin <私仓地址>`，`git push -u origin main`；
3. 打一个起始 tag，例如 `git tag v0.1.0 && git push --tags`，供别处按 tag 取用。

**验收**：别人 clone 后 `copy config\host.local.yaml.example host.local.yaml` + `.\kharon.cmd bootstrap --check` 能跑起来（缺 SDK 时按提示填）。

**依赖**：无（可与 1、2 并行）。

---

## [ ] 4. 补丁守卫（CI）

**现状**：`patches/zephyr/0001-esp32-wifi-mbedtls-guard.patch` 已入库，bootstrap 会自动重放；因此 zephyr 树里**必然**显示 `M modules/mbedtls/legacy_support.cmake`。

**纠正一个说法**：守卫不能写成"`git -C zephyr status --porcelain` 必须为空"——补丁已 applied，树一定是脏的。正确的判据是**"树内改动恰好等于补丁集"**：

```powershell
# ① 每个补丁都已应用
git -C ..\zephyr-workspace\zephyr apply --check --reverse ..\kharon-ide\patches\zephyr\*.patch
# ② 改动文件集合 ⊆ 补丁触及的文件集合（多出来的说明有人手改了树）
$touched = (git -C ..\zephyr-workspace\zephyr diff --name-only).Trim() -split "`n"
$patched = (git -C ..\kharon-ide apply --numstat --summary .\patches\zephyr\*.patch | Select-String '^ ')
# ③ 清单哈希三处一致：west.yml 的 revision / zephyr.commit / .kharon/bootstrap.json 的 zephyr_commit
```

**验收**：命令在干净树与被补丁污染的树上都表现正确（前者只有补丁那几个文件、后者报差异）。

**依赖**：1、2 完成后 CI 才有稳定的清单可校验。

---

## [ ] 5. 端到端验收：模拟"别人 clone"

**在一个空的临时目录**（不要在你现有工作区里）跑完整链路：

```powershell
cd <临时空目录>
git clone <kharon-manifest 地址> zephyr-workspace
git clone <kharon-ide 地址> kharon-ide
cd kharon-ide
python -m venv .deps\venv
.deps\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy config\host.local.yaml.example host.local.yaml    # 只填 SDK / gdb / openocd
.\kharon.cmd bootstrap --pinned
.\kharon.cmd bootstrap --check          # 期望全绿（patches、modules.pin 两项也在）
.\kharon.cmd ws -p f411_breath
.\kharon.cmd run -p f411_breath --steps doctor,build
.\kharon.cmd cockpit                    # 127.0.0.1:8398
```

**验收判据**：全程**不装 VS Code 扩展**；`bootstrap --check` 无红色项；f411 与 s3 各能编过一个工程。

**依赖**：1、2、3、4 全部完成。

---

## 已知状态（不改，但要知道）

- `zephyr` 是 **shallow clone**（`git rev-parse --is-shallow-repository` = true）。要回退到更早的 commit 会失败；需要时跑一次 `git fetch --unshallow`。
- 当前钉的基线是 **`25851fa`（2026-10-27）**，已写进 `west.yml` 两处 + `.kharon/bootstrap.json`。旧的 `34e0255`（09-14）只是历史。
- 工作区根的 `.gitignore` / `.gitattributes` 在模型 C 下**没有归属仓**（清单仓的根是 `kharon-manifest/`，管不到工作区根）。眼下当本机便利文件留着；1、2 建仓时若各自需要忽略规则，写进各自仓的 `.gitignore`。
- `.west/config` 仍指向 `zephyr/west.yml`，**没有**切到 `kharon-manifest`。切换要等 1、2 完成、在全新工作区里验证，别在现用工作区上切。

## 已完成

- [x] `kharon-manifest/west.yml`（单文件双角色：`manifest:` 给 west / `zephyr:`·`projects:` 给 kharon），`import: true` 实测解析 71 项
- [x] `kharon-ide/patches/zephyr/0001-esp32-wifi-mbedtls-guard.patch` + `bootstrap` 幂等重放（`patches` 体检项）
- [x] `bootstrap._pin_modules`：pinned 时把 6 个模块 checkout 回清单 revision（保住 `hal_espressif` fork），`modules.pin` 体检项
- [x] 工作区 `.gitignore` / `.gitattributes`
- [x] `kharon-manifest/README.md`（模型 C 的克隆-上手流程与升级 Zephyr 的唯一入口）
