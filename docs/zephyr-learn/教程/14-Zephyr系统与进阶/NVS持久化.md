# NVS 持久化：把数据真的写进 flash

> 工程：`applications\esp32s31\korvo_nvs`　构建目录：`build\esp32s31\korvo_nvs\s31-debug`　镜像 146048 B（FLASH 0.87%）
> 三条命令集全部**已烧录实测**，跨 4 次复位验证过「写 / 读 / 删」都真的落到 flash。原始输出见第 5 节。

---

## 1. 为什么"存个变量"不算持久化

`.bss` / `.data` 里的变量活不过一次复位：RAM 断电即失，连"复位不清 RAM"的错觉都不可靠——启动代码、栈、你之前的程序都会覆盖它。

真正持久化只有两条路：

| 路子 | 是什么 | 适合 |
|---|---|---|
| **裸 flash 读写** | `flash_read/write/erase` 自己管页和擦除 | 大块数据、镜像、文件系统 |
| **NVS**（本页） | Zephyr 的键值存储，自动处理擦除/搬移/CRC | 配置项、计数器、校准值 |

NVS 的价值在于**你不用管擦除**。NOR flash 有个反直觉的物理限制：**只能把 1 写成 0，要变回 1 必须整页擦除**。所以"改一个字节"实际上要先擦掉 4 KB 整页。NVS 把这件事消化掉了：它用"追加写 + 索引表 + 扇区搬移"的方式，让你在 API 层面感觉像在改一个字典。

---

## 2. 这棵树里的 NVS 长在哪（和上游文档不一样）

Zephyr 4.x 把 NVS 从 `subsys\fs\nvs` 挪到了 `subsys\kvss\nvs`，头文件也跟着变：

| | 老位置（网上大多数教程） | **本树（实测有效）** |
|---|---|---|
| 头文件 | `<zephyr/fs/nvs.h>` | **`<zephyr/kvss/nvs.h>`** |
| 源码 | `subsys/fs/nvs/nvs.c` | **`subsys/kvss/nvs/nvs.c`** |
| 样例 | `samples/subsys/fs/nvs` | **`samples/subsys/kvss/nvs`** |

API 本身没变（`nvs_mount` / `nvs_read` / `nvs_write` / `nvs_delete` / `nvs_read_hist`），变的只是路径。**照着旧教程写 include，第一步就编译不过。**

Kconfig 依赖链（写 `prj.conf` 时照这个核对）：

```
CONFIG_NVS
  depends on FLASH
  depends on FLASH_PAGE_LAYOUT
  select CRC
```

`FLASH_MAP` 不是 `NVS` 的依赖，但 `PARTITION_DEVICE()` / `PARTITION_OFFSET()` 这两个宏来自 `<zephyr/storage/flash_map.h>`，要开 `CONFIG_FLASH_MAP`。**三者缺一个都会在链接期炸**（而且报错信息往往指不到根因）。

---

## 3. 分区表：数据存在哪块物理地址

板级 dts 里 `#include <espressif/partitions_0x2000_default_16M.dtsi>`，实测这块板的分区：

| 分区 | 偏移 | 大小 | 用途 |
|---|---|---|---|
| `mcuboot` | 0x2000 | 56 KB | bootloader |
| `sys` | 0x10000 | 64 KB | 系统 |
| `image-0`（slot0） | 0x20000 | 7936 KB | 当前固件 |
| `image-1`（slot1） | 0x7e0000 | 7936 KB | OTA 备用 |
| **`storage`** | **0xfb0000** | **192 KB** | **NVS 就放这** |
| `coredump` | 0xfff000 | 4 KB | 崩溃转储 |

`west flash` 只写 `image-0`，**不碰 `storage`**——这就是为什么重新烧固件后数据还在（也很容易误判成"我的代码在 RAM 里保留了值"）。

### 3.1 NVS 的扇区必须等于 flash 的页

代码里这三行不是随便填的：

```c
fs.offset = PARTITION_OFFSET(storage_partition);   /* 0xfb0000 */
flash_get_page_info_by_offs(fs.flash_device, fs.offset, &info);
fs.sector_size = info.size;                        /* 4096 */
fs.sector_count = 3U;                              /* 12 KB */
```

- ESP32 系列 NOR flash 的**页 = 4 KB**（擦除最小单位）。
- NVS 的"扇区"casually 就是"一个可擦除单元"。`sector_size` 写成 4096 之外的数会直接 `-EINVAL`。
- `sector_count = 3` → 12 KB。**为什么不是 48**（192 KB / 4 KB）？够用就行：12 KB 能存几百条记录，而 NVS 每次写都要找空位，扇区越多启动扫描越慢。

实测 `nvs info` 输出：

```
[nvs] storage 分区 offset  = 0x00fb0000
[nvs] 扇区 = 4096 B × 3 个 = 12288 B
[nvs] 可用容量估算 = 8192 B（留 1 个扇区做搬移）
```

最后那行不是 API 给的，是自己算的：`4096 × (3-1) = 8192`。原因见 4.3。

---

## 4. 代码：为什么要拆成两个文件

```
applications\esp32s31\korvo_nvs\src\
├── store.h     接口（芯片无关）
├── store.c     实现（只依赖 NVS + storage_partition + flash 驱动）
└── main.c      shell 界面（这一层换板子才需要动）
```

这不是为了"显得工程化"，而是为了你下一步的计划：**接 ESP32-S3 时，`store.c/h` 可以原样拷过去**。它里面没有一行代码认识 ESP32：没有 GPIO 号、没有 SoC 寄存器、没有 `#ifdef CONFIG_SOC_ESP32S31`。

### 4.1 读的重灾区：`nvs_read` 返回 0 不是"成功"

这是 NVS 最反直觉的一点：

```c
rc = nvs_read(&fs, id, out, sizeof(*out));
if (rc == 0)        return -ENOENT;   /* 没有这个键！不是成功 */
if (rc < 0)         return rc;        /* 真出错 */
if (rc != sizeof(*out)) return -EINVAL;/* 键存在但长度不对 */
return 0;                             /* 命中，rc 是读到的字节数 */
```

`nvs_read` 的返回值是**实际读取的字节数**：

| 返回 | 含义 |
|---|---|
| `> 0` | 命中，值是读到的字节数（可能是 1，也可能是 8） |
| `0` | **这个 id 没有数据** |
| 负 | 错误码（`-EIO` / `-EINVAL` / ...） |

第一次抄官方 sample 的人几乎都会被这个 `0` 坑一次：写 `if (rc) { 成功 }` 会把"没找到"当成成功。

### 4.2 三种数据类型的读写差异

| 类型 | 写法 | 关键点 |
|---|---|---|
| `uint32` | `nvs_write(&fs, id, &val, 4)` | 长度固定，读回时校验 `rc == 4`，防"长度不符" |
| 字符串 | `nvs_write(&fs, id, str, strlen+1)` | **连 `'\0'` 一起存**，否则读回来要自己补 |
| blob | `nvs_write(&fs, id, buf, n)` | 任意长度（受扇区大小限制），读时给够缓冲 |

读的时候有个陷阱：**你不需要预先知道长度**，但必须给一个不小于实际长度的缓冲。给太小会返回 `-ENOMEM`（本次实现转成了自己的错误码），而且**已经消耗了一次 flash 读**。

### 4.3 为什么容量要按 `(sector_count - 1)` 算

NVS 的写入模型是**追加式**：

```
扇区写满 → 找下一个空扇区 → 把"仍然有效的键"搬过去 → 擦掉旧扇区
```

这个"搬移"（GC）需要**至少一个空扇区**才能工作。所以 3 个扇区里，实际能放的量约等于 2 个扇区的容量；全部塞满会触发频繁搬移，写放大严重。

### 4.4 删除不是真的删除

```c
nvs_delete(&fs, id);   /* 只是把这条记录的"有效位"清掉 */
```

空间不会立刻回收，要等扇区搬移时才真正消失。所以：

- 频繁 `set` 同一个键 → 同一扇区很快被写满 → 触发搬移（搬移时**该键的历史值全丢**，只留最新的）。
- 想拿历史值用 `nvs_read_hist(&fs, id, &buf, len, 0)`（第 5 个参数是"往前退第几条"）。

### 4.5 开机计数：最简单的"真持久化"证据

```c
uint32_t store_boot_count_bump(void)
{
	uint32_t cnt = 0;

	if (store_get_u32(STORE_ID_BOOT_CNT, &cnt) != 0) {
		cnt = 0;           /* 第一次上电，键还不存在 */
	}
	cnt++;
	if (store_set_u32(STORE_ID_BOOT_CNT, cnt) < 0) {
		return 0;
	}
	return cnt;
}
```

这段代码的价值不在功能，而在**可证伪**：如果数据只在 RAM 里，每次复位都会打印"第 1 次开机"；只要它变成 2、3、4，就证明真的写进了 flash。

**代价要知道**：每次上电都写一次 flash。NOR flash 的擦写寿命约 10 万次（按扇区算），开机计数每次占一条记录、3 个扇区能装几百条，几百次上电就要搬移一次。学习阶段无所谓，产品里"开机计数"应该改成"每 N 次落盘一次"或干脆放 RTC RAM。

---

## 5. 实机演示（原始输出，三轮）

### 5.1 第一轮：写

```
=== Korvo-1 NVS ===
本次是第 2 次开机（计数已落盘）
nvs info | nvs dump | nvs set 10 123 | nvs get 10
nvs tag korvo-1 | nvs rgb 20 30 40 | nvs bright 60 | nvs del 10
uart:~$ 
nvs info
[nvs] storage 分区 offset  = 0x00fb0000
[nvs] 扇区 = 4096 B × 3 个 = 12288 B
[nvs] 可用容量估算 = 8192 B（留 1 个扇区做搬移）
[nvs] 开机计数 = 2（本次是第 2 次）
uart:~$ 
nvs dump
[nvs] ---- 已知键 ----------
  1 boot_cnt = 2
[nvs] ---------------------
uart:~$ 
nvs set 10 123
[nvs] id=10 ← u32 123 已写入（4 B）
uart:~$ 
nvs get 10
[nvs] id=10 → u32 123 (0x0000007b)
uart:~$ 
nvs tag korvo-1
[nvs] 标签 "korvo-1" 已写入（8 B）
uart:~$ 
nvs rgb 20 30 40
[nvs] RGB = 14 1e 28 已写入（3 B）
uart:~$ 
nvs bright 60
[nvs] 亮度 = 60% 已写入（4 B）
uart:~$ 
nvs dump
[nvs] ---- 已知键 ----------
  1 boot_cnt = 2
  2 tag      = "korvo-1"
  3 rgb      = 14 1e 28
  4 bright   = 60%
[nvs] ---------------------
```

注意 `本次是第 2 次开机`：`west flash` 之后板子自动重启了一次（第 1 次），本次串口工具又复位了一次（第 2 次）。**计数在第一次就已经落盘了**，所以第二次能读到。

### 5.2 第二轮（又一次复位之后）：值还在，计数 +1

```
nvs info
[nvs] storage 分区 offset  = 0x00fb0000
[nvs] 扇区 = 4096 B × 3 个 = 12288 B
[nvs] 可用容量估算 = 8192 B（留 1 个扇区做搬移）
[nvs] 开机计数 = 3（本次是第 3 次）
uart:~$ 
nvs get 10
[nvs] id=10 → u32 123 (0x0000007b)
uart:~$ 
nvs dump
[nvs] ---- 已知键 ----------
  1 boot_cnt = 3
  2 tag      = "korvo-1"
  3 rgb      = 14 1e 28
  4 bright   = 60%
[nvs] ---------------------
uart:~$ 
nvs del 10
[nvs] id=10 已删除
```

**这一条是整页的核心证据**：`id=10` 是在上一轮写的，中间经历了复位、程序重新启动、`.bss` 被清零，值仍然是 `123`。

### 5.3 第三轮（再复位一次）：删除也持久

```
=== Korvo-1 NVS ===
本次是第 4 次开机（计数已落盘）
uart:~$ 
nvs get 10
[nvs] id=10 不存在
uart:~$ 
nvs info
[nvs] 开机计数 = 4（本次是第 4 次）
uart:~$ 
nvs dump
[nvs] ---- 已知键 ----------
  1 boot_cnt = 4
  2 tag      = "korvo-1"
  3 rgb      = 14 1e 28
  4 bright   = 60%
```

三轮连起来读：

| 现象 | 证明了什么 |
|---|---|
| 计数 2 → 3 → 4 | `nvs_write` 真的落盘，且每次复位后能读到上次的值 |
| `id=10` 跨复位仍是 123 | 任意键都能持久，不只是"已知的 4 个" |
| 删除后跨复位仍"不存在" | `nvs_delete` 也是持久操作（写的是有效位，不是内存标记） |
| `tag` / `rgb` / `bright` 一直在 | 字符串、blob、u32 三种类型都正常 |

---

## 6. 七个坑

| # | 现象 | 原因 | 解法 |
|---|---|---|---|
| 1 | `fatal error: zephyr/fs/nvs.h: No such file` | 本树 NVS 已迁到 kvss | `#include <zephyr/kvss/nvs.h>` |
| 2 | 链接报 `undefined reference to nvs_mount` | 只开了 `CONFIG_FLASH`，漏了 `CONFIG_NVS` | `CONFIG_FLASH=y` + `CONFIG_FLASH_PAGE_LAYOUT=y` + `CONFIG_NVS=y` |
| 3 | `PARTITION_DEVICE` 未定义 | 没开 `CONFIG_FLASH_MAP` | `CONFIG_FLASH_MAP=y` |
| 4 | 读到"成功"但值是垃圾 | `nvs_read` 返回 **0 = 没找到**，被当成成功 | 判 `> 0` 才算命中，见 4.1 |
| 5 | `nvs_mount` 返回 `-EINVAL` | `sector_size` 不等于 flash page | 用 `flash_get_page_info_by_offs` 拿 `info.size` |
| 6 | 编译警告 `%x expects unsigned int, argument has off_t` | `fs.offset` 是 `off_t`（long） | 强转 `(unsigned int)` 或改 `%ld` |
| 7 | 以为"烧完固件数据没了" | `west flash` 只写 `image-0`，不碰 `storage` | 要清空得整片擦除（`west flash --erase` 之类），别拿它当"复位测试" |

第 7 条要展开一句：**"重新烧录后数据还在"不是 bug，是分区设计的结果**。想让每次烧录都从干净状态开始，得手动擦 `storage` 分区；反过来，这也意味着"用户数据"能扛住固件升级——这正是产品里想要的行为。

---

## 7. 跨芯片复用：接 ESP32-S3 时怎么搬

你后面的计划是加一块 ESP32-S3 板，展示"跨项目编译 + 应用层通用"。这套代码就是为此设计的，迁移成本可以量化：

| 文件 | 换 ESP32-S3 要改什么 | 复用度 |
|---|---|---|
| `src/store.c` / `store.h` | **理论上零改动**（只依赖 `storage_partition` 名字和 flash 驱动） | ≈100% |
| `src/main.c`（shell 层） | 零改动（`nvs` 命令集与 SoC 无关） | ≈100% |
| `CMakeLists.txt` | 零改动 | 100% |
| `prj.conf` | 零改动（`FLASH`/`FLASH_MAP`/`FLASH_PAGE_LAYOUT`/`NVS` 在所有 SoC 上同名） | ≈100% |
| 板目标 `-b` | 改成 `esp32s3_devkitc/esp32s3/procpu` 之类 | — |
| **分区表** | **可能要补 `storage_partition` 节点** | 见下 |

唯一真正需要动的地方是**分区表**：`store.c` 用宏 `PARTITION_DEVICE(storage_partition)`，这要求新板的 dts 里有个 **`label = "storage"`** 的分区。ESP32-S3 的默认分区表不一定有。补法是在 `app.overlay` 里加：

```dts
/* 从 flash0 的现有分区表里切一块出来做 storage（大小按需） */
&flash0 {
	partitions {
		compatible = "fixed-partitions";
		#address-cells = <1>;
		#size-cells = <1>;

		storage_partition: partition@XX0000 {
			label = "storage";
			reg = <0xXX0000 DT_SIZE_K(64)>;
		};
	};
};
```

配套检查三件事：

1. `nvs info` 里的 `offset` 是否等于你在 dts 里写的地址；
2. `sector_size` 是否等于该 SoC 的 flash page（ESP32 系列都是 4 KB，STM32 常见 128 KB——**这时 `sector_count` 要重算，`3 × 128 KB` 会白白吃掉 384 KB**）；
3. 分区是否与 `image-0` 不重叠（重叠会烧坏固件）。

**这就是"应用层通用"的真实含义**：不是"代码一行不用改"，而是"**只有硬件描述那一层要改，逻辑层一行不动**"。这也正好是你未来做跨板演示时最该讲的那句话。

---

## 8. 实战：把"记忆"接进应用层（`korvo_apps`）

前面 7 节是拿独立工程 `korvo_nvs` 练 API。真正有用的是**让应用层有记忆**：上电恢复上次的模式/基色/亮度/功放，改完顺手落盘。这一段就是把 `store.c` 搬进 `applications/esp32s31/korvo_apps` 的全过程。

### 8.1 三个文件的改动

| 文件 | 改了什么 |
|---|---|
| `CMakeLists.txt` | `target_sources(app PRIVATE src/main.c src/store.c)` |
| `prj.conf` | 加 `FLASH` / `FLASH_MAP` / `FLASH_PAGE_LAYOUT` / `NVS` 四行 |
| `src/store.c` `store.h` | 从 `korvo_nvs` 原样拷进来，**一个字节没改** |
| `src/main.c` | 加键定义 + `cfg_save/cfg_load/cfg_clear` + `app saved` / `app clear` + 启动恢复 |

`store.c` 一个字节没改这件事，就是第七节那张"复用度 ≈100%"的表格的实证——它只认 `storage_partition` 和 flash 驱动，不认识 ESP32。

### 8.2 键空间要自己规划（这坑没人提醒）

`korvo_nvs` 演示里用的是 `id=10`（写 123）。如果应用层也拿 10 当 `K_MODE`，会发生什么？**同一块板的两个工程共用同一个 `storage` 分区**，`id=10` 里躺着的 `123` 会被应用当成"模式 = 123"，`mode_name[123]` 直接越界。

所以应用层另开一段：

```c
/* 用 20-23 这段独立 id，避免和 korvo_nvs 演示用的 10 号键撞车 */
#define K_MODE    20u
#define K_RGB     21u
#define K_BRIGHT  22u
#define K_PA      23u
```

实际产品里更稳的做法是**给每个模块划一段 id 区间**（比如 0-9 系统、10-19 音频、20-29 灯效），并且把区间写进头文件注释里。NVS 的 id 是 `uint16_t`，够用。

### 8.3 存 / 取 / 清三件套

```c
static void cfg_save(void)
{
	uint8_t rgb[3] = {g_base.r, g_base.g, g_base.b};

	store_set_u32(K_MODE, (uint32_t)g_mode);      /* 模式是枚举，按 u32 存 */
	store_set_blob(K_RGB, rgb, sizeof(rgb));      /* 颜色 3 字节，按 blob 存 */
	store_set_u32(K_BRIGHT, (uint32_t)g_bright);  /* 亮度 0-100 */
	store_set_u32(K_PA, (uint32_t)g_pa);          /* 功放 on/off */
}

static void cfg_load(void)
{
	uint32_t v;
	uint8_t rgb[3];
	size_t n = 0;

	/* 每个键都要"存在 + 范围合法"两道检查，坏数据不能直接用 */
	if (store_get_u32(K_MODE, &v) == 0 && v <= RGB_STATUS) {
		g_mode = (enum rgb_mode)v;
	}
	if (store_get_blob(K_RGB, rgb, sizeof(rgb), &n) == 0 && n == 3) {
		g_base.r = rgb[0];
		g_base.g = rgb[1];
		g_base.b = rgb[2];
	}
	if (store_get_u32(K_BRIGHT, &v) == 0 && v <= 100) {
		g_bright = (int)v;
	}
	if (store_get_u32(K_PA, &v) == 0) {
		g_pa = v ? 1 : 0;
	}
}

static void cfg_clear(void)
{
	store_del(K_MODE);
	store_del(K_RGB);
	store_del(K_BRIGHT);
	store_del(K_PA);
}
```

`cfg_load` 里那两个 `v <= RGB_STATUS` / `v <= 100` 不是啰嗦：**flash 里的数据可能来自上一版固件、可能是半写的、可能是别的工程写坏的**。持久化代码永远要假设"读出来的是脏数据"，否则一个越界的枚举值就能让 `mode_name[g_mode]` 打到野指针上。这是产品代码和个人实验最大的区别。

### 8.4 顺序很关键：先 load，再起动画线程

```c
int main(void)
{
	uint32_t boot = 0;

	if (store_init() == 0) {
		cfg_load();
		boot = store_boot_count_bump();
	} else {
		LOG_WRN("NVS 不可用，本次运行不记忆状态");
	}

	gpio_pin_configure_dt(&pa, GPIO_OUTPUT_INACTIVE);
	gpio_pin_set_dt(&pa, g_pa);        /* 功放状态也要恢复到存档值 */

	printk("[boot] 恢复配置：模式=%s 基色=%02x %02x %02x 亮度=%d%% PA=%d 第 %u 次开机\n",
	       mode_name[g_mode], g_base.r, g_base.g, g_base.b, g_bright, g_pa, boot);

	k_thread_create(&rgb_thread_data, ...);   /* 线程最后才起 */
	...
}
```

如果先把 `k_thread_create` 起起来再读 NVS，第一帧一定是默认色（0x00 0x20 0x40 蓝绿），用户会看到"闪一下才变成上次的颜色"。**25 fps 的线程 40 ms 就出一帧，而 NVS 读要几毫秒**——顺序错了就是肉眼可见的 bug。

### 8.5 每条命令在哪一刻落盘

| 命令 | 落盘时机 | 落了哪几个键 |
|---|---|---|
| `app rgb solid 20 0 0` | 设完模式/基色之后 | MODE + RGB + BRIGHT + PA（整包写，简单） |
| `app bright 15` | 夹到 0-100 之后 | 同上 |
| `app pa on/off` | `gpio_pin_set_dt` 之后 | 同上（`pa on` 还会改基色为 `08 20 08`，所以必须先改状态再存） |
| `app key n` | 每次按键状态**变化**时 | 同上 |
| `app clear` | 立即 | 删 4 个键 |

一点工程判断：**每按一次键就写 flash 是"能用但不优雅"**。NVS 一次写入约 4 B，`storage` 分区的擦写寿命按 10 万次算够用，但真实产品会做 300 ms 防抖 + 只在"值真的变了"时写。这里保留"变化即写"，是因为它让实测证据最干净（按一下键→复位→颜色还真在），文档里把这句话标出来比藏起来好。

### 8.6 顺手踩到的一个坑：`printk` 和 `LOG_INF` 不一样

一开始恢复信息是这么写的：

```c
LOG_INF("恢复配置：模式=%s ...", ...);
```

结果有三轮复位，串口里**这一行时有时无**——`LOG_INF` 走的是日志后端（默认 deferred 模式，由一个日志线程异步搬出去），而 `app` 模块自己的 `LOG_LEVEL_INF` 又把这个模块的级别抬到了 info，两边叠加就出现了"有时能打、有时被刷掉"的现象。改成 `printk` 之后每一轮都稳定出现，因为 `printk` 是**同步直出**，不排队。

判断标准很简单：**开机时必须让用户看到的那一行，用 `printk`；调试诊断用的，用 `LOG_INF`。**

### 8.7 实测原文（四轮）

**第一轮：干净状态 + 写入**

```
[boot] 恢复配置：模式=status 基色=00 20 40 亮度=60% PA=0 第 16 次开机
[saved] ---- NVS 里存的内容 ----
  模式   = 未存
  基色   = 未存
  亮度   = 未存
  功放   = 未存
  开机次数 = 16
uart:~$ app rgb solid 20 0 0
[rgb] 模式 = solid, 基色 = 14 00 00, 亮度 = 60%, 返回 = 0（已落盘）
uart:~$ app bright 15
[bright] 亮度 = 15%（下一帧生效，已落盘）
uart:~$ app stats
[stats] 模式=solid 亮度=15% 帧数=289 单帧耗时=2 ms 最后颜色=03 00 00
uart:~$ app saved
  模式   = solid (1)
  基色   = 14 00 00
  亮度   = 15%
  功放   = off
```

**第二轮：复位 → 配置自己回来了**

```
[boot] 恢复配置：模式=solid 基色=14 00 00 亮度=15% PA=0 第 17 次开机
[stats] 模式=solid 亮度=15% 帧数=74 单帧耗时=2 ms 最后颜色=03 00 00
  模式   = solid (1)   基色 = 14 00 00   亮度 = 15%   功放 = off
```

注意 `帧数=74`：这是复位后新起的线程数出来的帧，说明 **第一帧就已经是恢复后的红色**，不是跑了几帧才变。

**第三轮：开功放（会覆盖基色）→ 复位**

```
[boot] 恢复配置：模式=solid 基色=08 20 08 亮度=15% PA=1 第 19 次开机
[stats] 模式=solid 亮度=15% 帧数=75 单帧耗时=2 ms 最后颜色=01 04 01
```

`PA=1` 说明功放状态也记住了；`08 20 08 × 15% = 01 04 01`，亮度和基色的乘法关系仍然自洽——这条比"值存在"更有说服力，因为它证明了**恢复的是完整状态，不是几个孤立数字**。

**第四轮：`app clear` → 复位**

```
[clear] 已清空 NVS 里的模式/基色/亮度/功放（下次上电回到默认）
[boot] 恢复配置：模式=status 基色=00 20 40 亮度=60% PA=0 第 20 次开机
[stats] 模式=status 亮度=60% ...
```

四轮合起来正好覆盖持久化该有的四种行为：**没存过→用默认；存了→读回来；跨复位→还在；删了→回默认**。

### 8.8 资源代价

| 项 | 数值 |
|---|---|
| FLASH（整镜像） | 213504 B / 1.27% |
| NVS 占用（storage 分区） | 12 KB 里用了不到 1 KB |
| 每帧耗时 | 仍是 2 ms（NVS 只在命令时写，不占动画路径） |
| 开机多花的时间 | 几毫秒（一次 mount + 4 次 read） |

"25 fps 不掉帧"这点值得单独说：**持久化绝不能放在实时路径里**。如果把 `cfg_save` 塞进 40 ms 的动画循环，一次 flash 写（几毫秒到几十毫秒，还带擦除）就会让呼吸灯卡顿。放命令里、放状态变化里，才是对的。

---

## 9. 自检

1. `nvs_read` 返回 0 代表什么？为什么它容易把人骗过去？
2. 为什么 `sector_size` 必须等于 flash page？ESP32 上是多少？
3. `sector_count = 3` 和 `48` 都能跑，为什么选 3？可用容量为什么按 `n-1` 算？
4. `nvs_delete` 之后空间立刻回收了吗？什么时候回收？
5. `west flash` 重新烧固件后为什么 `storage` 里的数据还在？这对产品意味着什么？
6. 换到 STM32（page 128 KB）时，`sector_count = 3` 会有什么后果？
7. `store.c` 为什么能原样搬到 ESP32-S3？它依赖的三样东西分别是什么？
8. `korvo_apps` 里为什么键 id 要用 20-23，而不是接着 `korvo_nvs` 的 10 用？
9. `cfg_load()` 里为什么每个键都要再查一次范围？只判"读到没有"会出什么事？
10. 为什么恢复配置必须在 `k_thread_create` 之前？顺序反过来会看到什么现象？
11. `printk` 和 `LOG_INF` 在开机那一行上的差别是什么？什么时候该各用各的？
12. 如果把 `cfg_save()` 放进 40 ms 的动画循环，会出现什么可观测的故障？
