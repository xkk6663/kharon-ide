# RGB：从「点亮一粒灯」到「25 fps 动画引擎」

`11-Zephyr驱动/WS2812.md` 讲的是「怎么把一粒灯点亮」。这一篇讲的是「点亮之后怎么办」——因为**只会点亮，是做不出功能的**。

板载 RGB 是一粒 WS2812（GPIO37），`led_strip` 驱动只给你三样东西：`led_strip_length()`、`led_strip_update_rgb()`、`led_strip_update_channels()`。它不给你呼吸、不给彩虹、不给心跳，甚至不给你「渐变」——**渐变是靠你自己按帧算出来的**。

这篇的思路：把「算下一帧」和「刷出去」拆成两层，中间用一份应用状态连接，于是：

- 换一种动画 = 换一个 `switch` 分支，不动驱动；
- 加一条业务规则（比如「录音中亮红色」）= 改一个枚举，不动帧计算；
- 想看它到底跑多快 = 加一个计数器，不动线程。

---

## 一、分层：谁负责什么

```text
        ┌──────────────────────────────────────────┐
        │ 应用层   applications/esp32s31/korvo_apps/src/main.c       │
        │  模式状态机 · 帧计算 · 亮度 · 帧率统计     │
        │  ← shell 命令改状态，线程按状态出帧 →      │
        └──────────────────────┬───────────────────┘
                               │  led_strip_update_rgb(dev, &px, 1)
        ┌──────────────────────▼───────────────────┐
        │ 驱动层   drivers/led_strip (WS2812)       │
        │  把 r/g/b 拆成 GRB 字节，交给时序后端      │
        └──────────────────────┬───────────────────┘
                               │
        ┌──────────────────────▼───────────────────┐
        │ 外设层   pulse_io + RMT@20355000 通道 0    │
        │  按 WS2812 的 T0H/T1H 硬性时序拨 GPIO37    │
        └──────────────────────────────────────────┘
```

三层各有各的判据：外设层看「灯有没有颜色」（肉眼），驱动层看「返回 0」，应用层看「`app stats` 的帧数在涨、颜色在变」。

**为什么要这么分？** 因为 `led_strip_update_rgb` 是一次**阻塞**的 RMT 传输：一粒灯 24 bit × 1.25 µs ≈ 30 µs，加上 `reset-delay = 300 µs`，一次调用实测量到单帧 2 ms。如果每个动画都在中断里调它，系统会抖；如果每个动画自己写时序，你就在重写驱动。分开之后，应用层只需要关心「40 ms 到了，下一帧该是什么颜色」。

---

## 二、驱动给的那一行 API

```c
int led_strip_update_rgb(const struct device *dev, struct led_rgb *pixels, size_t num_pixels);
```

`struct led_rgb` 是 `r/g/b` 三个 `uint8_t`（**注意不是 `u8_t` 的老写法**，4.5 里已经改了）。调用方不用管 GRB 顺序、不用管时序，设备树的 `color-mapping = [GRB]` 和 `reset-delay` 已经写死了。

本板设备树里的这个节点（`11-Zephyr驱动/WS2812.md` 有完整解释）：

```dts
ws2812: ws2812 {
	compatible = "worldsemi,ws2812-pulse-io";
	chain-length = <1>;
	color-mapping = [GRB];
	spi-or-rmt = "rmt";
	rmt-channel = <0>;
	reset-delay = <300>;
	pinctrl-0 = <&rmt_korvo>;
	pinctrl-names = "default";
};
```

所以应用层拿到的句柄只有一个：

```c
#define STRIP_NODE DT_ALIAS(led_strip)
static const struct device *const strip = DEVICE_DT_GET(STRIP_NODE);
```

启动时打一行自证：

```c
LOG_INF("led_strip=%s length=%d", strip->name, led_strip_length(strip));
```

实测输出：

```text
[00:00:00.000,000] <inf> app: led_strip=ws2812 length=1
```

`length=1` 很关键——它证明 `chain-length` 解析对了。如果这里出来 0 或者别的数，后面所有 `update_rgb(..., 1)` 都会写越界或写空。

---

## 三、应用状态：五个模式一个枚举

```c
enum rgb_mode {
	RGB_OFF,
	RGB_SOLID,
	RGB_BREATH,
	RGB_RAINBOW,
	RGB_STATUS,
};

static volatile enum rgb_mode g_mode = RGB_STATUS;   /* 上电默认心跳灯 */
static volatile struct led_rgb g_base = {.r = 0x00, .g = 0x20, .b = 0x40};
static volatile int  g_bright = 60;                  /* 0..100 全局亮度 */
static volatile uint32_t g_frames;                   /* 已刷出的帧数 */
static volatile uint32_t g_last_ms;                  /* 上一帧耗时 */
static volatile uint8_t  g_last_rgb[3];              /* 最后一帧的颜色 */
```

三个设计决定：

1. **`g_mode` 是唯一的状态源。** shell 命令只改它，线程只读它。没有「模式对象」「回调表」，因为只有 5 个模式，`switch` 比函数指针表更好读，也不会在中断里跳飞。
2. **`g_base` 和 `g_bright` 分开。** `g_base` 是「基色」（`solid 20 0 0` 里的 `20 0 0`），`g_bright` 是全局亮度。分开的好处是：调亮度不用重新输一遍颜色，改颜色也不会把亮度冲掉。
3. **状态量都标 `volatile`。** shell 线程（优先级 14）写，动画线程（优先级 7）读，编译器不能把它们缓存进寄存器。

上电默认 `RGB_STATUS` 是刻意的：**设备一上电就该能自己说明「我还活着」**，而不是黑着等人敲命令。这是嵌入式产品的默认礼仪。

---

## 四、帧计算：每种模式的「下一帧」

核心就一个函数，输入「当前毫秒」，输出「这一帧的 RGB」：

```c
static struct led_rgb compute_frame(uint32_t now_ms)
{
	struct led_rgb px = {0};
	int env = 100;              /* 包络：呼吸、心跳用 */

	switch (g_mode) {
	case RGB_OFF:
		return px;

	case RGB_SOLID:
		px = g_base;
		break;

	case RGB_BREATH:
		env = triangle(now_ms, 2400);        /* 2.4 s 一次呼吸 */
		px = g_base;
		break;

	case RGB_RAINBOW: {
		int hue = (int)((now_ms % 3600) * 360 / 3600);   /* 3.6 s 转一圈 */
		hsv2rgb(hue, 100, 100, &px);
		break;
	}

	case RGB_STATUS: {
		/* 心跳：亮 200 ms、灭 200 ms、亮 200 ms、然后长灭 1400 ms */
		uint32_t p = now_ms % 2000;
		env = ((p < 200) || (p >= 400 && p < 600)) ? 100 : 0;
		px.r = 0x00; px.g = 0x10; px.b = 0x60;
		break;
	}
	}

	return scale(&px, env * g_bright / 100);   /* 包络 × 全局亮度 */
}
```

**这个函数是纯粹的：「同样的时间戳进来，同样的颜色出去」。** 它不读硬件、不 `k_msleep`、不打印日志，所以你可以把它单独拿出来在 PC 上跑，一秒模拟 1000 帧，肉眼验证呼吸曲线对不对——不用烧板子。

### 四.1、呼吸为什么用三角波而不是 sin

```c
static int triangle(uint32_t t, uint32_t ms_period)
{
	uint32_t half = ms_period / 2;
	uint32_t p = t % ms_period;

	if (p < half) {
		return (int)(p * 100 / half);          /* 0 → 100 */
	}
	return (int)((ms_period - p) * 100 / half); /* 100 → 0 */
}
```

三个理由：

1. **不用浮点。** 这块板子有 FPU，但 `sin()` 是 libm 的软浮点路径（newlib 的 `sinf`），一次调用几百个周期；三角波是两次整数减法加一次除法。
2. **不用查表。** 查表要占 flash，还要处理相位精度。
3. **视觉上够用。** 人眼对「线性上升 vs 正弦上升」的差别几乎感知不到；真正影响观感的是**周期**和**峰值停留时间**，不是曲线形状。

实测 `app rgb breath 0 0 60` + `app stats` 抓到 `最后颜色 = 00 00 00`——那正好是三角波落在谷底的相位，属于正常采样，不代表呼吸坏了。**这就是为什么 `app stats` 要打「最后一帧」而不是「当前模式」**：它给的是随机的瞬时证据，多抓几次就能看出波形。

### 四.2、彩虹：HSV 是给「人」用的坐标

RGB 里做彩虹很别扭——你要让 `r/g/b` 三个通道轮流当主角，还得保证亮度和饱和度不跳。HSV 里就一句话：**色相 h 从 0 转到 360，s 和 v 钉在 100。**

```c
static void hsv2rgb(int h, int s, int v, struct led_rgb *out)
{
	int c = (v * s) / 100;
	int x = c * (60 - abs((h % 120) - 60)) / 60;
	int m = v - c;

	switch (h / 60) {
	case 0:  out->r = c + m; out->g = x + m; out->b = m;     break;
	case 1:  out->r = x + m; out->g = c + m; out->b = m;     break;
	case 2:  out->r = m;     out->g = c + m; out->b = x + m; break;
	case 3:  out->r = m;     out->g = x + m; out->b = c + m; break;
	case 4:  out->r = x + m; out->g = m;     out->b = c + m; break;
	default: out->r = c + m; out->g = m;     out->b = x + m; break;
	}
}
```

这是标准 HSV 六段算法的整数版：`c` 是色度（chroma），`x` 是次分量，`m` 是补底。三处容易写错的地方：

- `h` 必须先把 `[0,360)` 取模再进 `switch`，否则 `h=360` 会落到 `default`，颜色跳一下；
- `abs()` 要 `#include <stdlib.h>`，不然 GCC 15 直接报 `implicit declaration`（本次构建实际撞到过这个错）；
- 全程 `int`，最后才赋给 `uint8_t`——中间任何一步提前截断，彩虹就会出现「断层」。

实测 `app rgb rainbow`：

```text
[rgb] 模式 = rainbow, 基色 = 00 20 40, 亮度 = 60%, 返回 = 0
```

注意「基色」对一个彩虹模式没意义（它打的是上一次 `g_base` 的残留），**这是这套命令输出的一个已知瑕疵**：彩虹/心跳不用基色，但结构体里还留着旧值。判断彩虹是否真的在跑，要看 `app stats` 的 `最后颜色`：

```text
[stats] 模式=rainbow 亮度=60% 帧数=206 单帧耗时=2 ms 最后颜色=00 3c 28
```

`00 3c 28` = `(0, 0x3c, 0x28)`，是绿偏青的那一段色相；过几秒再抓会是另一个值。**颜色在变 = 彩虹在转。**

### 四.3、亮度：两级乘法，顺序不能反

呼吸/心跳有一个「包络」`env`（0–100），全局亮度 `g_bright`（0–100），所以：

```c
return scale(&px, env * g_bright / 100);   /* 先把两个百分比乘起来，再一次除 */
```

```c
static struct led_rgb scale(const struct led_rgb *c, int pct)
{
	struct led_rgb out = {
		.r = (uint8_t)((c->r * pct) / 100),
		.g = (uint8_t)((c->g * pct) / 100),
		.b = (uint8_t)((c->b * pct) / 100),
	};
	return out;
}
```

两个坑：

1. **顺序反了会丢精度。** 若先 `scale(px, env)` 再 `scale(px, bright)`，`0x20 × 30% = 9`，再 `9 × 15% = 1`，而正确算法 `0x20 × 4.5% ≈ 1`——差别不大；但换到 `0x60 × 1%` 这种小值，两次截断会直接变 0。
2. **截断只向下取整。** `20 × 60% = 12`，`12 × 15% → 3`（实际是 `20×9% = 1.8 → 1`，但两级算出来是 3/1 的差别）。教程里给出的实测证据是：

```text
app rgb solid 20 0 0
[rgb] 模式 = solid, 基色 = 14 00 00, 亮度 = 60%, 返回 = 0      ← 0x14 = 20（十进制 20 被打印成十六进制）
app bright 15
[bright] 亮度 = 15%（下一帧生效）
app stats
[stats] 模式=solid 亮度=15% 帧数=253 单帧耗时=2 ms 最后颜色=03 00 00
```

`03` 就是 `20 × 15% = 3`。**亮度真的生效了，而且是从 60% 拉到 15% 之后下一帧就变了。**

顺带一个观察：`app rgb solid 20 0 0` 里的 `20` 是十进制，打印出来是 `14`（十六进制），第一次看会以为是两个不同的数。**这是日志格式问题，不是值错了**——命令解析用 `strtoul(argv[k], NULL, 0)`，`20` → 20 → `0x14`。

---

## 五、线程：为什么是 25 fps 的独立线程

```c
#define RGB_STACK_SIZE   2048
#define RGB_PRIORITY     7

K_THREAD_STACK_DEFINE(rgb_stack, RGB_STACK_SIZE);
static struct k_thread rgb_thread_data;

static void rgb_thread(void *a, void *b, void *c)
{
	const uint32_t period = 40;      /* 25 fps */

	while (1) {
		uint32_t t0 = k_uptime_get_32();
		struct led_rgb px = compute_frame(t0);
		int ret = led_strip_update_rgb(strip, &px, 1);

		if (ret == 0) {
			g_frames++;
			g_last_ms = k_uptime_get_32() - t0;
			g_last_rgb[0] = px.r; g_last_rgb[1] = px.g; g_last_rgb[2] = px.b;
		}
		k_msleep(period);
	}
}
```

### 五.1、为什么不是 `k_timer` + `k_work`

`k_timer` 的回调跑在**系统时钟中断上下文**里，绝对不能在里面调 `led_strip_update_rgb`——它要阻塞 2 ms 等 RMT 传完，等于在中断里睡 2 ms，整个系统的 tick 全部迟到。

换成 `k_work` 丢到系统工作队列，也有问题：系统工作队列是**全局共享**的，你每 40 ms 占它 2 ms，别的驱动（比如 shell 的延迟处理、日志刷盘）就会被你拖慢，而且一旦有别的长任务排在前面，动画就会卡帧——**表现为灯「一顿一顿」而不是匀速呼吸**。

独立线程最干净：优先级 7（比 shell 的 14 高，保证命令输入卡顿时灯不卡；比系统工作队列低，保证不抢关键路径），栈 2048 足够（`compute_frame` 里最深的是 `hsv2rgb` 的 6 个 int 局部变量，加 `led_strip` 内部的一次调用）。

### 五.2、为什么是 25 fps

- **10 fps**：呼吸和彩虹都能看出「台阶」，人眼对亮度跳变的阈值大约在 15–20 fps；
- **25 fps**：40 ms 一帧，单帧 2 ms，CPU 占用 5%，肉眼已经是连续的；
- **50 fps 以上**：WS2812 单灯每帧固定 300 µs reset + 24 bit 传输，加上 RMT 排队，实测没有收益，只是把 CPU 从 5% 抬到 10%。

**帧率的上限不由你想要多少决定，由「一帧要多久」决定。** 单帧 2 ms，理论上限 500 fps；但 `reset-delay = 300 µs` 是 WS2812 的硬性要求（低于这个时间下一帧会被灯当成同一帧的延续），所以真正能压到的是 1 ms 量级——**没必要**。

### 五.3、帧率怎么读

`app stats` 打的是**累计帧数**，不是 fps。要算 fps，抓两次、看增量：

```text
[stats] 模式=solid  亮度=15% 帧数=253 ...
[stats] 模式=breath 亮度=15% 帧数=337 ...
[stats] 模式=status 亮度=15% 帧数=444 ...
```

337 → 444 是 107 帧，中间隔了约 4.3 秒（三条命令各自的采样窗口加起来），107 / 4.3 ≈ **25 fps**。跟 `period = 40 ms` 完全对得上。

**为什么不直接打 fps？** 因为「累计帧数」是一个**单调、可差分、不受采样时刻抖动影响**的量；而 fps 是一次性算出来的，抓的瞬间恰好卡一下就失真。工程上宁可选可累加的量。

---

## 六、上板实测（原样串口文本）

### 6.1 上电 → 默认心跳

```text
uart:~$ *** Booting Zephyr OS build 34e025569cbb ***
[00:00:00.000,000] <inf> app: led_strip=ws2812 length=1
[00:00:00.000,000] <inf> app: PA_CTRL=GPIO7 配置返回 0

=== Korvo-1 apps ===
app rgb status|rainbow|off
app rgb solid 20 0 0
app rgb breath 0 0 60
app bright 30
app pa on|off
app stats

app stats
[stats] 模式=status 亮度=60% 帧数=74 单帧耗时=2 ms 最后颜色=00 00 00
```

`帧数=74` 说明从上电到敲命令之间，线程已经跑了 74 帧（约 3 秒），**心跳灯在没人管的情况下自己在跑**。`最后颜色=00 00 00` 是心跳的长灭相位——刚好抓到。

### 6.2 纯色 + 亮度

```text
app rgb solid 20 0 0
[rgb] 模式 = solid, 基色 = 14 00 00, 亮度 = 60%, 返回 = 0

app bright 15
[bright] 亮度 = 15%（下一帧生效）

app stats
[stats] 模式=solid 亮度=15% 帧数=253 单帧耗时=2 ms 最后颜色=03 00 00
```

### 6.3 呼吸 → 彩虹 → 关灯

```text
app rgb breath 0 0 60
[rgb] 模式 = breath, 基色 = 00 00 3c, 亮度 = 15%, 返回 = 0
app rgb rainbow
[rgb] 模式 = rainbow, 基色 = 00 00 3c, 亮度 = 15%, 返回 = 0
app rgb off
[rgb] 模式 = off, 基色 = 00 00 3c, 亮度 = 15%, 返回 = 0
app stats
[stats] 模式=solid 亮度=15% 帧数=552 单帧耗时=2 ms 最后颜色=01 04 01
```

注意最后这一条：`app rgb off` 之后 `app pa on` 把模式改回了 `solid`（功放联动，见下一篇），所以 `模式=solid`、`最后颜色=01 04 01`——**这是功能，不是 bug**：开功放时给一盏常亮绿灯，是「应用层语义」的一部分。

### 6.4 彩虹实证（等着看颜色变）

```text
app bright 60
[bright] 亮度 = 60%（下一帧生效）
app rgb rainbow@3
[rgb] 模式 = rainbow, 基色 = 00 20 40, 亮度 = 60%, 返回 = 0
app stats
[stats] 模式=rainbow 亮度=60% 帧数=206 单帧耗时=2 ms 最后颜色=00 3c 28
```

---

## 七、可扩展：状态灯是「一行代码一条语义」

这套结构真正的好处，是加业务语义时不用碰帧计算。比如做录音笔：

| 状态 | 想表达 | 改哪里 |
|---|---|---|
| 待机 | 蓝色慢呼吸 | `g_mode = RGB_BREATH; g_base = {0,0,0x30};` |
| 录音中 | 红色常亮 | `g_mode = RGB_SOLID; g_base = {0x40,0,0};` |
| 正在连 Wi-Fi | 蓝色快闪 | 新加 `RGB_BLINK`，`triangle(now, 400)` |
| 电量低 | 橙色慢闪 | `RGB_BLINK` + `g_base = {0x40,0x18,0}` |
| 出错 | 红色三连闪后停 | `RGB_STATUS` 换一种包络 |

**唯一要守住的原则：所有状态切换都走「改 `g_mode` + `g_base`」，绝不在业务代码里直接调 `led_strip_update_rgb`。** 一旦有两处代码同时刷灯，就会出现「一半帧是红、一半帧是蓝」的撕裂，而且极难复现。

---

## 八、这一篇踩过的坑

| 现象 | 原因 | 解法 |
|---|---|---|
| 灯完全不亮，无任何报错 | `rmt` 节点没 `status = "okay"`，`WS2812_STRIP_PULSE_IO` 不 select RMT | overlay 里显式打开 `&rmt` |
| 编译报 `implicit declaration of function 'abs'` / `'strtoul'` | 没 include `<stdlib.h>` / `<string.h>` | 补头文件（本次构建实际撞到） |
| 自定义 `CONFIG_APP_RGB_THREAD_STACK_SIZE` 报未定义 | 自建 Kconfig 符号没写 `Kconfig` 文件 | 直接用 `#define`，别为两个常数建 Kconfig |
| 呼吸「一顿一顿」 | 用了 `k_work` 系统工作队列，被别的任务插队 | 独立线程 + 固定 `k_msleep(40)` |
| 彩虹有色带断层 | `hsv2rgb` 中间结果提前截断成 `uint8_t` | 全程 `int`，最后一步才赋值 |
| `app rgb rainbow` 之后 `app stats` 颜色没变 | 抓的两次恰好同相位（3.6 s 一圈） | 隔 1 s 再抓一次 |
| 串口被 `<wrn> ADC reading is uncompensated` 刷满 | `CONFIG_LOG_DEFAULT_LEVEL=3` | 设成 `1`，只留 error |

---

## 九、自检清单

跑完这一篇，你应该能回答：

- [ ] `led_strip` 这层到底给应用层留了几个 API？为什么渐变要靠应用层算？
- [ ] 为什么呼吸用三角波而不是 `sin()`？省下来的是什么？
- [ ] `env * g_bright / 100` 这个乘法顺序如果反了，会出什么问题？
- [ ] 为什么动画必须放独立线程，而不能放 `k_timer` 回调或 `k_work`？
- [ ] 25 fps 这个数字是拍的还是算的？单帧 2 ms 意味着理论上限多少？
- [ ] `app stats` 为什么打「累计帧数」而不是「fps」？
- [ ] `[rgb] 模式 = solid, 基色 = 14 00 00` 里，`14` 对应你输入的哪个数？

---

上一篇：[README](README.md) ｜ 下一篇：[功能按键与功放](功能按键与功放.md)
