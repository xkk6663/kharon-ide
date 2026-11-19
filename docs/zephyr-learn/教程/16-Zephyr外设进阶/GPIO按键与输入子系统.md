# 16 · GPIO 按键：从"读引脚"升级到 input 子系统

> **一句话**：自己写 `gpio_pin_get()` + `k_msleep(20)` 也能做按键，但 Zephyr 已经给了你一套标准答案——`gpio-keys` 驱动 + input 子系统 + 消息队列 + 业务线程。这篇讲的就是这条链路**为什么长这样**，以及第 16 组实测踩到的三个坑。

---

## 一、硬件事实（用户给的，不是我猜的）

| 资源 | 脚 | 电气 | 说明 |
|---|---|---|---|
| 按键 KEY | **IO45** | **空闲实测为低电平**（按下应为高，见下） | 普通 GPIO 按键，不是 IO42 那种 ADC 分压梯 |
| 板载 RGB | **IO48** | WS2812 单线串行 | 走 RMT 的 `pulse_io`，本组没动它，但和按键**共用一个工程** |

**极性这件事必须实测，不能照抄"按键接地"。** 我先按惯性写了 `GPIO_PULL_UP | GPIO_ACTIVE_LOW`（假设"按下接地、松开上拉为高"），结果 `app btn` 打出：

```
[btn] 逻辑电平=1 物理电平=0（物理 1=高/上拉侧，0=低/接地侧）
```

物理 0 = 引脚在低电平，**而 dts 里明明开了内部上拉**——说明板上有更强的东西把它拉下来了。IO45 在 ESP32-S3 上还兼作 `VDD_SPI` 的 strapping 脚，开发板常配下拉电阻，内部那点上拉（几十 kΩ）压不过它。既然空闲被钉在低电平，那按键只可能把脚**拉高**，于是改成：

```dts
gpios = <&gpio1 13 GPIO_PULL_DOWN>;    /* 空闲 0 = 松开，按下 1 */
```

改完实测 `逻辑电平=0 物理电平=0`，与"松开"一致。

> 这段的价值不在结论，而在**方法**：`gpio_pin_get_dt()` 给逻辑电平（考虑 `ACTIVE_LOW`），`gpio_pin_get_raw()` 给物理电平，两个一起打出来，极性就没有争论空间了。第六节 6.2 给了一张"按下去没反应"的二分法排查表。

**IO45 到 devicetree 的换算**（这是最容易错的一步）：S3 的 GPIO 分成两组，`gpio0 = 0..31`，`gpio1 = 32..63`。所以

```
IO45 → gpio1 的 pin (45 - 32) = 13
```

写成 dts 就是 `<&gpio1 13 (...)>`。

> 顺带一个常被抄错的例子：ESP32-S3-DevKitC-1 官方板 dts 里的 `sw0` 指的是 `&gpio0 0`（BOOT 键，IO0）。IO0 是芯片的启动模式脚，**按住 IO0 复位会进下载模式**。所以我们这块最小板的 KEY 放在 IO45 是好运气——它不参与启动。

---

## 二、dts 只改两个地方

`applications\esp32s31\korvo_apps\boards\esp32s3_devkitc_esp32s3_procpu.overlay`（节选，全文见文件头注释）：

```dts
/* 1) 把"某个键"的引脚换掉（极性见第一节实测） */
&button0 {
	gpios = <&gpio1 13 GPIO_PULL_DOWN>;
	label = "KEY";
};

/* 2) 去抖时间写在父节点 /buttons 上 */
&{/buttons} {
	debounce-interval-ms = <20>;
};
```

板级 dts 原本是什么样（`zephyr\boards\espressif\esp32s3_devkitc\esp32s3_devkitc_procpu.dts`）：

```dts
buttons {
	compatible = "gpio-keys";

	button0: button_0 {
		gpios = <&gpio0 0 (GPIO_PULL_UP | GPIO_ACTIVE_LOW)>;
		label = "BOOT Button";
		zephyr,code = <INPUT_KEY_0>;      /* ← 键码，INPUT_KEY_0 = 11 */
	};
};
```

三处细节值得盯住：

1. **`compatible = "gpio-keys"` 在父节点上。** 驱动实例 = 父节点 `/buttons`，子节点 `button0` 只是"这组键里的一个键"，**它本身没有驱动**。这一点直接决定了第三节那个链接错误。
2. **`zephyr,code` 我们没改**，还是 `INPUT_KEY_0`。所以回调里收到的 `evt->code == 11`。想区分多个键就改成 `INPUT_KEY_1`、`INPUT_KEY_A`……
3. **`debounce-interval-ms` 属于父节点**（binding：`zephyr\dts\bindings\input\gpio-keys.yaml` 里写在 `properties:` 顶层）。写在 `button0` 里 dtc 会直接拒绝：

```
devicetree error: 'debounce-interval-ms' appears in /buttons/button_0 ...,
but is not declared in 'properties:' in ...\dts\bindings\input\gpio-keys.yaml
```

---

## 三、input 子系统的三层结构（这是本篇的核心）

```
       ┌──────────────┐   GPIO 中断(双边沿)    ┌────────────────────┐
IO45 ──┤ gpio_esp32   ├──────────────────────►│ input_gpio_keys.c  │
       └──────────────┘                        │  k_work_delayable  │
                                               │  (去抖 20 ms)      │
                                               └─────────┬──────────┘
                                                         │ 复查引脚电平，真的变了才报
                                                         ▼
                                               ┌────────────────────┐
                                               │ input_report_key() │  ← input 核心
                                               │ 事件入队            │
                                               └─────────┬──────────┘
                                                         │ 默认 CONFIG_INPUT_MODE_THREAD=y
                                                         ▼
                                        ┌──────────────────────────────┐
                                        │ input 线程（栈 1024，队列 16）│
                                        │ 遍历该设备的回调链表           │
                                        └──────────────┬───────────────┘
                                                       ▼
                                        ┌──────────────────────────────┐
                                        │ 你的 btn_cb(evt, user_data)   │
                                        └──────────────────────────────┘
```

按源码逐层对齐（都是本树里的真实文件）：

| 层 | 文件 | 关键行为 |
|---|---|---|
| 驱动 | `zephyr\drivers\input\input_gpio_keys.c` | `gpio_pin_interrupt_configure_dt(GPIO_INT_EDGE_BOTH)`；中断里只做 `k_work_reschedule(&work, K_MSEC(debounce))`（第 148 行），**不在中断里判长短按** |
| 驱动（去抖） | 同上 `gpio_keys_poll_pin()` | 延时到了再读一次电平，和上次状态不同才 `input_report_key(dev, code, pressed, true, K_FOREVER)`（第 90 行） |
| 核心 | `zephyr\include\zephyr\input\input.h` | `input_report_key()` 把 `value` 归一成 0/1；`INPUT_CALLBACK_DEFINE` 把回调塞进 `STRUCT_SECTION_ITERABLE(input_callback, …)` 这个 linker 段 |
| 消费者 | 你的 `main.c` | 回调签名必须是 `void (*)(struct input_event *, void *)` |

**为什么回调不在中断里**：中断里只 reschedule 一个 work，真正的"电平判定"发生在系统工作队列里；而**回调本身跑在 input 线程**（本工程 `.config` 实测 `CONFIG_INPUT_MODE_THREAD=y`、`CONFIG_INPUT_THREAD_STACK_SIZE=1024`、`CONFIG_INPUT_QUEUE_MAX_MSGS=16`）。这两条都意味着：**回调里不能阻塞、不能大栈、不能 `k_msleep()`**。

---

## 四、代码怎么分层（`applications\esp32s31\korvo_apps\src\main.c`）

### 4.1 能力探测：不要用 `sw0` 别名判断"有没有按键"

```c
#define HAS_KEY   DT_HAS_COMPAT_STATUS_OKAY(gpio_keys)
#define KEY_DEV_NODE DT_COMPAT_GET_ANY_STATUS_OKAY(gpio_keys)

static const struct device *const key_dev  = DEVICE_DT_GET(KEY_DEV_NODE);
static const struct gpio_dt_spec key_gpio   = GPIO_DT_SPEC_GET(KEY_NODE, gpios);
```

两种宏各管一件事，不要混：

- `DT_HAS_COMPAT_STATUS_OKAY(gpio_keys)` → "这棵树里有没有一个 enabled 的 `gpio-keys` 设备"（**驱动实例**）
- `DT_COMPAT_GET_ANY_STATUS_OKAY(gpio_keys)` → 拿到那个节点，喂给 `DEVICE_DT_GET`
- `DT_ALIAS(sw0)` → 只用来取**引脚规格**（因为别名正好指向带 `gpios` 的子节点）

### 4.2 回调只做一件事：入队

```c
struct btn_evt { uint32_t ms; bool pressed; };
K_MSGQ_DEFINE(btn_q, sizeof(struct btn_evt), 16, 4);

static void btn_cb(struct input_event *evt, void *user_data)
{
	if (evt->type != INPUT_EV_KEY) {
		return;
	}
	struct btn_evt e = { .ms = k_uptime_get_32(), .pressed = evt->value != 0 };
	k_msgq_put(&btn_q, &e, K_NO_WAIT);
}

INPUT_CALLBACK_DEFINE(DEVICE_DT_GET(KEY_DEV_NODE), btn_cb, NULL);
```

`INPUT_CALLBACK_DEFINE` 的第二个参数是**函数**，第三个是用户数据。它展开后是一个放在 linker 段的常量结构，**不需要你手动注册**——这就是 Zephyr "迭代器段"的典型用法（和 shell 命令、log 后端同一套机制）。

### 4.3 业务线程：长短按判定 + 落盘

```c
static void btn_thread(void *a, void *b, void *c)
{
	static const int levels[] = {15, 30, 60, 100};
	struct btn_evt e;
	uint32_t press_ms = 0;

	while (1) {
		if (k_msgq_get(&btn_q, &e, K_FOREVER) != 0) continue;

		if (e.pressed) {                     /* 按下：先记账 */
			press_ms = e.ms;
			g_btn_presses++;
			continue;
		}

		uint32_t dur = e.ms - press_ms;

		if (dur < 80)  continue;             /* 电气抖动，丢 */
		if (dur < 800) {                     /* 短按：轮转模式 */
			g_btn_short++;
			g_mode = (enum rgb_mode)((g_mode + 1) % (RGB_STATUS + 1));
			printk("[btn] 短按 %u ms -> 模式=%s\n", dur, mode_name[g_mode]);
		} else {                             /* 长按：亮度 15→30→60→100→15 */
			g_btn_long++;
			int idx = -1;
			for (size_t i = 0; i < ARRAY_SIZE(levels); i++) {
				if (g_bright == levels[i]) idx = (int)i;
			}
			idx = (idx + 1) % (int)ARRAY_SIZE(levels);
			g_bright = levels[idx];
			printk("[btn] 长按 %u ms -> 亮度=%d%%\n", dur, g_bright);
		}

		cfg_save();                          /* 按键改的也是配置，必须落盘 */
	}
}
```

四个设计取舍，都值得说清理由：

| 取舍 | 为什么 |
|---|---|
| 时间戳来自 `k_uptime_get_32()`，不是回调自己算 | 事件到得早晚不确定，用**驱动报事件那一刻**的时间才准 |
| 80 ms 以下直接丢 | 去抖 20 ms 只解决"一次按下弹几下"；人手抖 + 松手抖动可能更长，留一道保险 |
| 800 ms 分短/长按 | 常见手感阈值（0.5–1 s）；本工程取 800 ms，短按响应快、长按不容易误触 |
| `cfg_save()` 放在线程里 | NVS 写几十 ms；放回调里会卡住 input 线程，把后续事件挤爆 16 条队列 |

线程创建（`main()`）：

```c
#if HAS_KEY
	if (!device_is_ready(key_dev)) {
		LOG_ERR("按键设备未就绪");
	}
#endif
	...
	k_thread_create(&btn_thread_data, btn_stack, K_THREAD_STACK_SIZEOF(btn_stack),
			btn_thread, NULL, NULL, NULL, RGB_PRIORITY + 1, 0, K_NO_WAIT);
```

优先级用 `RGB_PRIORITY + 1`（比动画线程低一级）：按键业务不该抢动画的时序，但也不能低到被日志线程饿死。

### 4.4 配置放哪：`boards\<board>.conf`，不是 `prj.conf`

`applications\esp32s31\korvo_apps\boards\esp32s3_devkitc_esp32s3_procpu.conf`：

```
CONFIG_INPUT=y
```

理由和 overlay 一模一样：**S3 才需要 input 子系统，Korvo-1 上根本不该编进来**。放进 `prj.conf` 会让 Korvo 也背上 `input` 的代码体积（虽然 `gpio-keys` 因为没有 dts 节点不会实例化，但 `CONFIG_INPUT` 本身是有成本的）。放板级 `.conf`，两边各管各的。

### 4.5 自测命令 `app btnsim`：把"软件链"和"电气层"分开验收

```c
static int cmd_btnsim(const struct shell *sh, size_t argc, char **argv)
{
	int ms = (argc > 1) ? atoi(argv[1]) : 300;

	input_report_key(key_dev, INPUT_KEY_0, 1, false, K_NO_WAIT);  /* 注入"按下" */
	k_msleep(ms);
	input_report_key(key_dev, INPUT_KEY_0, 0, false, K_NO_WAIT);  /* 注入"松开" */

	shell_print(sh, "[btn] 已注入一次 %d ms 按键事件（只走 input 软件链路，不碰 GPIO）", ms);
	return 0;
}
```

三个设计点：

- **`false`（不 sync）**：驱动用的是 `input_report_key(..., true, K_FOREVER)`，同步等处理完；命令里用异步，避免 shell 线程被 input 线程的反向依赖卡住。
- **`k_msleep(ms)` 在 shell 线程里睡**：这里睡是安全的（shell 线程本来就是阻塞式交互），和"回调里不能睡"是两回事。
- **它证明的边界很清晰**：回调、队列、线程、业务、NVS 全都在链上；唯独 `GPIO 中断 + 去抖` 不在——那一段只能靠真按键。

这个命令的价值不是"作弊绕过硬件"，而是**可复现**：改完业务逻辑后跑一次 `btnsim 300` / `btnsim 1200`，就知道自己有没有把长短按判反、把亮度档写错。回归测试不依赖人手。

---

## 五、四个真坑（都是本轮实际报错）

### 坑 1：`DT_HAS_ALIAS(sw0)` 会帮你选中"子节点"，链接阶段报 `__device_dts_ord_15`

我第一版写的是：

```c
#define HAS_KEY DT_HAS_ALIAS(sw0)
static const struct device *const key_dev = DEVICE_DT_GET(KEY_NODE);   /* KEY_NODE = DT_ALIAS(sw0) */
```

编译过了，链接炸：

```
app/libapp.a(main.c.obj):(.literal.cmd_btn+0x4): undefined reference to `__device_dts_ord_15'
app/libapp.a(main.c.obj):(._input_callback.static._input_callback__btn_cb_+0x0): undefined reference to `__device_dts_ord_15'
```

原因：`sw0 = &button0`，`button0` 是 `gpio-keys` 的**子节点**，没有 `DEVICE_DT_INST_DEFINE`，所以它没有 `__device_dts_ord_NN` 符号；而 `DEVICE_DT_GET(子节点)` 会去引用这个符号 → 未定义。

**判据**：`DT_ALIAS(sw0)` 适合取 `gpios`；判断"设备在不在"要用 `compatible`。

### 坑 2：`debounce-interval-ms` 写在子节点 → dtc 直接拒

见第二节。报错原文：

```
devicetree error: 'debounce-interval-ms' appears in /buttons/button_0 in ...\zephyr.dts.pre,
but is not declared in 'properties:' in ...\dts\bindings\input\gpio-keys.yaml
```

### 坑 3：回调签名加 `const` → `-Wincompatible-pointer-types`

```c
/* 错 */
static void btn_cb(const struct input_event *evt, void *user_data)
```

```
main.c:287:48: error: initialization of 'void (*)(struct input_event *, void *)'
from incompatible pointer type 'void (*)(const struct input_event *, void *)'
```

`input_callback.callback` 的类型是**非 const**（`zephyr\include\zephyr\input\input.h:138`）。虽然你不改它，但签名必须一模一样。

### 坑 4：照抄"按键接地"的极性 → 空闲被读成"按下"

见第一节。这一坑**不报错、不警告**，只表现成"行为反了"（空闲就认为按着、真按下去反而当松开，于是每次按下都会立刻触发一次"长按"）。它是最典型的"编译器救不了你"的硬件假设错误，唯一解法是把逻辑电平与物理电平一起打出来对照。

---

## 六、实测原文（COM11，本板原生 USB）

构建：`FLASH: 215716 B / 8388352 B / 2.57%`（比无按键版 214708 B 多 **1008 B**：input 子系统 + 两条自测命令）。

复位后（原始日志 `logs\serial-s3-btn-4.txt`）：

```
[boot] 恢复配置：模式=solid 基色=14 00 00 亮度=30% 第 15 次开机（本板无 PA_CTRL）
=== Korvo apps（同一份代码，esp32s3_devkitc/esp32s3/procpu）===
app btn（按键：短按切模式 / 长按切亮度）
```

### 6.1 三层验证，逐层说清"验到了什么"

**第 1 层：设备就绪（驱动实例化成功）**

```
>>> app btn
[btn] 设备=buttons 脚=gpio@60004800
[btn] 逻辑电平=0 物理电平=0（本板空闲实测=物理 0，所以 0=松开 / 1=按下）
[btn] 按下=0 次，短按=0 次（切模式），长按=0 次（切亮度 15/30/60/100）
```

- `设备=buttons`：驱动实例名来自 `gpio-keys` 节点（`DEVICE_DT_INST_DEFINE` 用节点名当 name）。
- `脚=gpio@60004800`：**GPIO 控制器设备名**，不是引脚号。S3 的 `gpio1` 基址 `0x60004800`（`gpio0` 是 `0x60004000`）。看到 `800` 就知道"第二组 GPIO"。
- `逻辑电平=0 物理电平=0`：逻辑电平考虑 `ACTIVE_LOW`（本板没启用），物理是引脚真实电平。两个都打出来，是为了**极性可证伪**（见第一节）。

**第 2 层：软件链路（`app btnsim` 注入事件）**

这是本轮补的一个自测命令：直接 `input_report_key()` 注入一次"按下 → 等 N ms → 松开"，**绕过 GPIO 与去抖**，专门验证 `回调 → 消息队列 → 业务线程 → 计数/切模式/亮度/NVS` 这条链。

```
>>> app btnsim 300
[btn] 已注入一次 300 ms 按键事件（只走 input 软件链路，不碰 GPIO）
>>> app btn
[btn] 按下=1 次，短按=1 次（切模式），长按=0 次（切亮度 15/30/60/100）

>>> app btnsim 1200
[btn] 已注入一次 1200 ms 按键事件（只走 input 软件链路，不碰 GPIO）
>>> app btn
[btn] 按下=2 次，短按=1 次（切模式），长按=1 次（切亮度 15/30/60/100）
>>> app stats
[stats] 模式=solid 亮度=30% 帧数=497 单帧耗时=0 ms 最后颜色=06 00 00
```

读出来的四件事，条条对得上设计：

| 现象 | 证明什么 |
|---|---|
| 300 ms → `短按=1`，模式从 `off` 变 `solid` | 80 ms 抖动门限 + 800 ms 长短按门限都判对了 |
| 1200 ms → `长按=1`，亮度 15% → 30% | 亮度档位 `{15,30,60,100}` 步进逻辑正确 |
| `app stats` 里 `模式=solid 亮度=30%` | 业务真的改了全局状态，不是只加了计数 |
| 亮度 30% 在**复位后**的 `[boot]` 行里仍然出现（第 15 次开机） | `cfg_save()` 真的写了 NVS，跨复位保住了 |

**第 3 层：电气层（已由实机确认）**

`app btnsim` 覆盖不到两件事：① IO45 的机械触点是否真的把电平拉高；② `gpio-keys` 的中断 + 20 ms 去抖在真实抖动下的表现。这两件事必须有人按一下。

**2026-11-13 实机确认**：按住 IO45 的按键，**短按一下即切换 RGB 状态**（`off ↔ solid` 之间切换，正是 `btn_thread` 里 <800 ms 分支的行为）。至此三层全部打通：

```
电气层（IO45 触点 + 中断 + 20 ms 去抖）
   ↓
驱动层（input_gpio_keys.c → input_report_key）
   ↓
应用层（btn_thread 判长短按 → 切模式/切亮度 → cfg_save 写 NVS）
```

也就是说：`GPIO_PULL_DOWN` + `ACTIVE_HIGH` 这个极性判断是**对**的，物理 0 = 松开、按下变 1 成立。如果当初留着 `GPIO_PULL_UP | GPIO_ACTIVE_LOW`，空闲就会被读成"按下"，一按键反而被当成"松开"，短按切模式这一条就永远不会按预期触发。

补齐日志的方式（随时可复现）：

```powershell
& .\.deps\venv\Scripts\python.exe tools\zephyr\probe_serial.py COM11 12
```

短按一次（<0.8 s）、长按一次（≥0.8 s），期望看到：

```
[btn] 短按 312 ms -> 模式=solid
[btn] 长按 1420 ms -> 亮度=30%
```

再 `app btn` 看计数是否分别 +1、`app saved` 看 NVS 是否跟着变。

### 6.2 如果按下去没反应：二分法排查表

按顺序查，每步都能把范围砍一半：

| # | 查什么 | 命令/动作 | 判据 |
|---|---|---|---|
| 1 | 引脚有没有变化（电气层） | `probe_serial.py COM11 12` 抓日志，同时按住键不放，再 `app btn` | `物理电平` 从 0 变 1 → 电气通，问题在软件门限；一直 0 → 硬件/极性 |
| 2 | 极性是不是反了 | 把 overlay 那行换成 `(GPIO_PULL_UP \| GPIO_ACTIVE_LOW)` 重烧 | 换完空闲读 1、按下读 0 且计数动 → 原来的极性判断反了 |
| 3 | 键是不是没接在 IO45 | 按住键，读 `物理电平` | 一直是 0（或一直是 1）→ 这个脚上没接键，查原理图 |
| 4 | 软件链有没有断 | `app btnsim 300` | 计数动 → 链没问题，回到第 1 步；不动 → 看 input 线程栈/队列 |

第 4 步放最后，是因为**它最便宜也最能定性**：一条命令就能把"软件 bug"和"硬件/接线"彻底分开。

---

## 七、和 Korvo-1 的关系：这条代码在 Korvo 上是"零成本"的

| | Korvo-1（ESP32-S31） | ESP32-S3 最小板 |
|---|---|---|
| `HAS_KEY` | **0**（dts 里没有 `gpio-keys`） | 1 |
| 按键形态 | IO42 一路 ADC 分压梯，四键（SET/PLAY/VOL-/VOL+） | IO45 单键（空闲实测低电平，按下应为高） |
| 代码路径 | `app key`（ADC 窗口判定） | `app btn`（input 事件） |
| `CONFIG_INPUT` | 不编 | 编（板级 `.conf`） |

所以"同一份 `main.c`"里同时存在两套按键方案，靠 `HAS_KEY` 切：Korvo 上 `btn_cb`/`btn_thread`/`cmd_btn` 整段被预处理器删掉，`app btn` 命令返回 `-ENOTSUP`（打印"本板没有按键设备"）。**这就是第 15 组那套能力探测在应用层的第二次复用。**

---

## 八、坑表（本篇新增）

| # | 坑 | 现象 | 正解 |
|---|---|---|---|
| 1 | `debounce-interval-ms` 写在 `button0` 子节点 | dtc：`not declared in 'properties:'` | 写到父节点 `&{/buttons}` |
| 2 | `DT_HAS_ALIAS(sw0)` 当"有没有按键设备" | 链接：`undefined reference to __device_dts_ord_15` | 用 `DT_HAS_COMPAT_STATUS_OKAY(gpio_keys)` |
| 3 | 回调签名带 `const struct input_event *` | `-Wincompatible-pointer-types` | 去掉 `const` |
| 4 | 在回调里 `k_msleep()` / 写 NVS | 事件队列（16 条）被压满、丢事件 | 回调只入队，业务放线程 |
| 5 | 以为 `boards\<board>.overlay` 会和 `app.overlay` 叠加 | S3 上 `app.overlay` 整段没生效（见 `korvo_sys` 的 `__device_dts_ord_48`） | 二选一：板级 overlay 存在时要把通用内容搬进去（或 `-DDTC_OVERLAY_FILE="a;b"` 显式列出） |
| 6 | 把 `CONFIG_INPUT=y` 写进 `prj.conf` | Korvo 也背上 input 子系统 | 写进 `boards\<board>.conf` |
| 7 | 照抄"按键接地"的极性 | 开内部上拉后物理电平仍为 0（外部下拉更强），逻辑电平把"空闲"读成"按下" | 用 `gpio_pin_get_dt`（逻辑）+ `gpio_pin_get_raw`（物理）实测后再定 `ACTIVE_LOW` / `ACTIVE_HIGH` |
| 8 | 硬件没法按，就把整条链记成"未验证" | 结论含糊，后面的人还得重做一遍 | 加一个 `btnsim` 注入命令，把"软件链"和"电气层"分开验收 |

---

## 九、自检（答得出来才算学会）

1. IO45 为什么是 `<&gpio1 13>`，而不是 `<&gpio0 45>`？
2. `gpio-keys` 的**驱动实例**是哪个节点？为什么 `DEVICE_DT_GET(DT_ALIAS(sw0))` 会链接失败？
3. 去抖发生在中断里，还是工作队列里？回调又跑在哪个上下文？两者分别对应哪个 Kconfig？
4. 为什么"回调只入队"是硬要求，而不是代码风格偏好？（提示：`CONFIG_INPUT_QUEUE_MAX_MSGS`）
5. 短按/长按的时间差，是从哪个时间点算到哪个时间点？如果用回调里 `k_uptime_get_32()` 取"按下时刻"会有什么偏差？
6. 同一份 `main.c` 在 Korvo 上 `app btn` 为什么会返回 `-ENOTSUP`？预处理器删掉了哪几段？
7. `app btnsim` 能证明什么、不能证明什么？为什么它比"多按几次看运气"更适合做回归？
8. 如果 `物理电平` 一直不变，你的前三个怀疑对象是什么？（提示：脚号、极性、键的接线端）
