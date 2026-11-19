# 03 · 获取 Zephyr 代码

## 三条路，选对那条能省几个小时

| 方式 | 拿到什么 | 适合 |
|---|---|---|
| `west init` 官方流程 | 完整 git 历史 + 全部模块 | 网络好、想长期跟进上游 |
| release tag 单独 clone | 某个稳定版本 | 只想读代码学概念 |
| **镜像 + PR 增量**（本教程） | 最新主干 + 只差 PR 那几个对象 | 国内网络、只要 S31 能编译 |

国内直连 GitHub 实测只有 **30–70 KB/s**，Zephyr 主仓一次浅克隆的压缩包约 240 MB，纯等要 1 小时以上。下面是实际走通的路径。

## 方案一：官方流程（网络好的时候用）

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon-ide
west init west
cd west
west update
```

`west update` 会按 `zephyr/west.yml` 把几十个模块拉到 `modules/` 下，2–4 GB。这套流程的好处是 `west` 的所有子命令（`west boards`、`west blobs fetch`、`west flash`）都能正常工作。

## 方案二：镜像 + 只取 PR 增量（本教程实际采用）

关键在于理解一件事：**git 只传输你本地没有的对象**。所以先用国内镜像把主干拿到，再从 GitHub 取 PR，传输量就是两者之差——从 240 MB 降到不到 1 MB。

### 2.1 从 Gitee 镜像浅克隆主干

```powershell
git clone --depth 1 https://gitee.com/mirrors/zephyr C:\Users\xiao1\Desktop\ELAB\kharon-ide\west\zephyr
```

实测这个镜像跟得很紧（本次拿到的 HEAD 是 2026-10-27），版本号 `4.5.99`，几分钟就完。

### 2.2 加 GitHub 为第二个 remote，取 PR 分支

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon-ide\west\zephyr
git remote add gh https://github.com/zephyrproject-rtos/zephyr
git fetch --depth 1 gh refs/pull/119851/head:pr-s31
git checkout pr-s31
```

**两个必须注意的点：**

1. **`--depth 1` 不能省。** 少了它，git 会认为你要完整历史，报文里会出现「Receiving objects: 1,592,845」——那是 160 万个对象，等于把整个仓库的历史再下一遍。加上之后实测只有 **918 个对象**，几秒钟完成。
2. **`refs/pull/<号>/head` 是 PR 的唯一地址**，不是分支名。PR 不存在对应分支，你只能这么取。

### 2.3 建 west 工作区

刚才只是 clone 了一个仓库，还没有 west 的目录结构。补一条命令：

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon-ide
west init -l west\zephyr
```

`-l` 表示"用本地已存在的仓库当 manifest 仓"，不会再联网 clone 一遍。它会在 `west\.west\config` 里写下 `path = zephyr`。

### 2.4 只拉真正需要的模块

`west update` 全量会拉几十个模块。编译一个 `hello_world`，实际只缺 `hal_espressif`：

```powershell
cd west
west update hal_espressif --narrow -o=--depth=1
```

`--narrow` 让它只处理指定项目，`-o=--depth=1` 把深度参数透传给 git。

注意 `zephyr/west.yml` 里 `hal_espressif` 的 revision 是 `pull/657/head`——**它也是一个 PR**，不是 tag。所以这一步同样会去 GitHub 取增量，慢是慢在这。

## 验证拿到了什么

```powershell
cd west
Get-Content VERSION                      # 版本号
git -C zephyr log -1 --format="%h %ad %s" --date=short
Test-Path zephyr\soc\espressif\esp32s31    # 必须是 True
Test-Path zephyr\boards\espressif\esp32s31_function_coreboard   # 必须是 True
```

前两个告诉你"是什么版本"，后两个告诉你"有没有 S31"。**只要有一个 False，后面所有步骤都会失败，先解决这里。**

## 目录长什么样

```
zephyr-learn\west\
├── .west\config            ← 工作区配置，记录 manifest 在哪
├── zephyr\                ← 主仓（切在 pr-s31）
│   ├── soc\espressif\esp32s31\
│   ├── boards\espressif\esp32s31_function_coreboard\
│   ├── kernel\  drivers\  subsys\  include\  dts\  samples\
│   └── west.yml
└── modules\hal\espressif\  ← PR 657 带来的 HAL
```

`modules/` 下只会出现你 `west update` 过的模块。这是正常的，不是残缺。

## 不需要的步骤（省时间）

- **不用 `west blobs fetch`**：那是 Wi-Fi/BLE 的二进制库，Zephyr 下 S31 的无线还没支持，编译 `hello_world` 用不到。
- **不用装 Zephyr SDK**，见 02 章的方案 B。
- **不用 git submodule**：west 模块不是 submodule，别手动 `git submodule update`。

## 自检

- `git fetch` 不加 `--depth 1` 会发生什么？为什么？
- `west init -l` 和 `west init` 的区别是什么？
- `west update hal_espressif` 之后，`modules/` 下为什么只有 espressif 一个目录？
