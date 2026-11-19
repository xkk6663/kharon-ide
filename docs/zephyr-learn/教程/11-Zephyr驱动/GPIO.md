# GPIO

GPIO 是驱动层最好的起点：接口少、现象直接、出错也会立刻暴露。本页把 `lab gpio` 里调用的每一个接口拆成一小节，格式固定：

> **原型** → **这棵树里驱动的实际行为** → **示例代码** → **板子输出（COM9 原样）**

本页所有输出来自 `applications/esp32s31/korvo_lab`，构建目录 `build\esp32s31\korvo_lab\s31-debug`，命令 `lab gpio`。

## 这块板子上的引脚

| 用途 | 节点 | 物理引脚 | 说明 |
|---|---|---|---|
| PA_CTRL（功放使能） | `gpio0` pin7 | GPIO7 | 只能当输出，板上无指示灯 |
| BOOT 键 | `gpio1` pin29 | GPIO61 | 板级 dts 里 `gpio_keys` 定义为 `GPIO_PULL_UP \| GPIO_ACTIVE_LOW` |
| I2C_SDA / SCL | `gpio0` pin0 / pin1 | GPIO0 / GPIO1 | 板上有外部上拉，空闲读 1 |

`PA_CTRL` 在 overlay 里被包成 `gpio-leds` 节点并起别名，这样代码可以直接 `DT_ALIAS(pa_ctrl)` 拿到它，不用记寄存器地址：

```dts
/ {
	leds {
		compatible = "gpio-leds";
		pa_ctrl: pa_ctrl_0 {
			gpios = <&gpio0 7 GPIO_ACTIVE_HIGH>;
			label = "PA_CTRL";
		};
	};
	aliases { pa-ctrl = &pa_ctrl; };
};
```

```c
#define PA_GPIOS   DT_ALIAS(pa_ctrl)
#define KEY_GPIOS  DT_ALIAS(sw0)

static const struct gpio_dt_spec pa  = GPIO_DT_SPEC_GET(PA_GPIOS, gpios);
static const struct gpio_dt_spec key = GPIO_DT_SPEC_GET(KEY_GPIOS, gpios);
```

📖 `gpio_dt_spec` 是"端口设备 + 引脚号 + 标志"三件套，`*_dt` 系列接口就是为它设计的；设备树改了引脚，代码一个字都不用动。

---

## gpio_pin_configure_dt

```c
int gpio_pin_configure_dt(const struct gpio_dt_spec *spec, gpio_flags_t extra_flags);
```

在这棵树里，`gpio_esp32_config()` 会做：把引脚切到 GPIO 功能（`gpio_ll_func_sel`）→ 按标志设输入/输出/上下拉/开漏 → 输出模式下给初值。**开漏支持，开源不支持**（`GPIO_OPEN_SOURCE` 直接 `-ENOTSUP`，同时内核打一行 `<err> gpio_esp32: GPIO configuration not supported`）。

```c
ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT_INACTIVE);
shell_print(sh, "[configure] PA%d 输出，初始低 = %s", pa.pin, err(ret));

ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT | GPIO_OPEN_DRAIN);
shell_print(sh, "[configure] PA%d 开漏输出 = %s", pa.pin, err(ret));

ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT | GPIO_OPEN_SOURCE);
shell_print(sh, "[configure] PA%d 开源输出 = %s", pa.pin, err(ret));

ret = gpio_pin_configure_dt(&key, GPIO_INPUT | GPIO_PULL_UP);
shell_print(sh, "[configure] BOOT(gpio1 pin%d) 输入+上拉 = %s", key.pin, err(ret));
```

板子输出：

```text
[node] pa=gpio@20583000 key=gpio@20583800
[configure] PA7 输出，初始低 = 0
[configure] PA7 开漏输出 = 0
[configure] PA7 开源输出 = -ENOTSUP
[configure] BOOT(gpio1 pin29) 输入+上拉 = 0
```

后面还会单独出现这行内核日志，它对应 `GPIO_OPEN_SOURCE` 那次调用：

```text
[00:00:03.167,000] <err> gpio_esp32: GPIO configuration not supported
```

✅ **自检**：三种输出模式里只有 OPEN_SOURCE 报错，说明开漏是真的支持（板上 PA_CTRL 是推挽负载，开漏下仍能拉低）。如果你看到的输出是三个 0，说明你的树里驱动还没拒绝开源。

---

## gpio_pin_set_dt / gpio_pin_get_dt

```c
int gpio_pin_set_dt(const struct gpio_dt_spec *spec, int value);
int gpio_pin_get_dt(const struct gpio_dt_spec *spec);
```

`set` 写 `OUT_W1TS/OUT_W1TC`，`get` 读 **IN** 寄存器。

⚠️ 这是本页第一个坑：ESP32 的 GPIO 要在 `configure` 里带上 `GPIO_INPUT` 才会打开输入使能（`ENABLE` 位）。纯输出脚的 IN 位恒为 0，所以**输出后立刻回读，读到的是 0，不是你以为的 1**。

```c
ret = gpio_pin_set_dt(&pa, 1);
shell_print(sh, "[set] gpio_pin_set_dt(PA,1) = %s", err(ret));

level = gpio_pin_get_dt(&pa);
shell_print(sh, "[get] gpio_pin_get_dt(PA) = %d", level);

ret = gpio_port_get_raw(pa.port, &v);
shell_print(sh, "[get_raw] gpio_port_get_raw(gpio0) = %s, PA 位 = %d", err(ret),
	    !!(v & BIT(pa.pin)));
```

板子输出：

```text
[set] gpio_pin_set_dt(PA,1) = 0
[get] gpio_pin_get_dt(PA) = 0
[get_raw] gpio_port_get_raw(gpio0) = 0, PA 位 = 0
```

✅ **自检**：`set` 回 0、`get` 回 0、`get_raw` 里那一位也是 0 —— 三条都"成功"却都读不到高电平，这就是纯输出脚的真实情况。要验证输出真的变了，只能靠外部观测（示波器、负载、或下面 WS2812 那种能看到的现象）。

---

## gpio_pin_toggle_dt

```c
int gpio_pin_toggle_dt(const struct gpio_dt_spec *spec);
```

翻转 `OUT` 寄存器对应位。返回 0 只代表寄存器写成功。

```c
ret = gpio_pin_toggle_dt(&pa);
shell_print(sh, "[toggle] gpio_pin_toggle_dt(PA) = %s", err(ret));
for (int i = 0; i < 6; i++) {
	k_msleep(100);
	gpio_pin_toggle_dt(&pa);
}
shell_print(sh, "[toggle] 翻转 6 次完成（PA_CTRL 无指示灯，靠返回值确认）");
```

板子输出：

```text
[toggle] gpio_pin_toggle_dt(PA) = 0
[toggle] 翻转 6 次完成（PA_CTRL 无指示灯，靠返回值确认）
```

---

## 端口级接口：get_raw / set_bits_raw / clear_bits_raw / toggle_bits / set_masked_raw

```c
int gpio_port_get_raw(const struct device *port, gpio_port_value_t *value);
int gpio_port_set_bits_raw(const struct device *port, gpio_port_pins_t pins);
int gpio_port_clear_bits_raw(const struct device *port, gpio_port_pins_t pins);
int gpio_port_toggle_bits(const struct device *port, gpio_port_pins_t pins);
int gpio_port_set_masked_raw(const struct device *port, gpio_port_pins_t mask,
			     gpio_port_pins_t value);
```

这五个是"一次动一整组脚"的原子操作：`set_bits` 走 `W1TS`、`clear_bits` 走 `W1TC`、`toggle_bits` 做读改写、`set_masked` 用掩码换值。**它们都不做引脚校验**，写 1 到没配置的位没有效果但也不报错。

`port_get_raw` 在这棵树的实现是直接读 `IN`（`gpio_dev->in`；`gpio1` 读 `in1.val`）：

```c
ret = gpio_port_clear_bits_raw(pa.port, BIT(pa.pin));
gpio_port_get_raw(pa.port, &v);
shell_print(sh, "[port_clear] gpio_port_clear_bits_raw = %s, PA 位 = %d", err(ret),
	    !!(v & BIT(pa.pin)));
/* set_bits / toggle_bits / set_masked 同样各打印一行 */
```

板子输出：

```text
[port_clear] gpio_port_clear_bits_raw = 0, PA 位 = 0
[port_set] gpio_port_set_bits_raw = 0, PA 位 = 0
[port_toggle] gpio_port_toggle_bits = 0, PA 位 = 0
[port_masked] gpio_port_set_masked_raw(PA,0) = 0, PA 位 = 0
```

✅ **自检**：四个返回值全 0，PA 位全 0。这不是接口坏了，而是和上一节同一个原因：读的是输入寄存器。换个有外部上拉的输入脚（例如 GPIO0/1）再 `port_get_raw`，就会看到 `IN = 0x57f00003` 里 bit0/bit1 为 1。

---

## 输入：gpio_pin_get_dt + GPIO_PULL_UP

```c
ret = gpio_pin_configure_dt(&key, GPIO_INPUT | GPIO_PULL_UP);
k_msleep(10);
level = gpio_pin_get_dt(&key);
ret = gpio_port_get_raw(key.port, &v);
```

板子输出（**按下和不按都一样**）：

```text
[get] gpio_pin_get_dt(BOOT) = 1  (0=按下 1=松开)
[get_raw] gpio_port_get_raw(gpio1) = 0, BOOT 位 = 0
```

这里出现了一个必须讲清楚的现象：`gpio_pin_get_dt` 回 1，而同一个引脚的 `gpio_port_get_raw` 回 0，**两个接口互相矛盾**。我为此加了 `lab key` 去直接读寄存器：

```c
#define GPIO0_BASE 0x20583000u
#define REG_IN     0x64u
#define REG_IN1    0x68u

uint32_t in1  = sys_read32(GPIO0_BASE + REG_IN1);      /* bank1 的真实电平 */
uint32_t in0  = sys_read32(GPIO0_BASE + REG_IN);
uint32_t en1  = sys_read32(GPIO0_BASE + 0x40u);        /* ENABLE1 */
```

板子输出：

```text
ENABLE=0x00000003 ENABLE1=0x04000020
IN=0x57f00003 IN1=0x18000414 (gpio1 窗口 IN1=0x00000000)
```

逐条读这张表：

- `ENABLE=0x00000003`：bank0 只有 pin0 / pin1 打开了输入（就是 I2C 的 SDA/SCL）；
- `ENABLE1=0x04000020`：bit26 → 物理 58、bit2 → 物理 34，**没有 bit29**，说明 BOOT（物理 61）的输入使能根本没被写进去；
- `IN=0x57f00003`：bit0/bit1 为 1 —— I2C 两脚有外部上拉，读得到；
- `IN1=0x18000414`：bit10 → 物理 42（ADC 按键那一路）为 1，其余按板上实际状态；**bit29（BOOT）恒 0**；
- `gpio1 窗口 IN1=0x00000000`：`gpio1`（基址 `0x20583800`）那个窗口读出来是 0，和真实电平无关。

原因在驱动实现里：`gpio_esp32.c` 所有实例的 `.gpio_base` 都取 `DT_REG_ADDR(DT_NODELABEL(gpio0))`，只有 `.gpio_dev` 用各自的节点；于是 `&gpio1 29` 这条描述到底落到哪个物理脚，取决于 `esp_gpio_port_pad()` 的换算，而这套 PR 里 bank1 的输入使能/回读还没闭环。

✅ **自检（三步判据）**：
1. `lab key` 里 `ENABLE1` 的 bit29 是否为 1 —— 没置起来，就说明配置没落到 BOOT 脚；
2. `IN1` 的 bit29 按按键是否变化 —— 不变，说明连电气状态都没读到；
3. 同一时刻 `gpio_pin_get_dt` 的值是否也跟着变 —— 三者只要不一致，就是驱动/描述没对齐，不要怀疑按键坏。

**这一节给你的可迁移结论**：板子上"按一下能改变电平"的脚如果落在 `gpio1`（物理 ≥32），在这套树上先别用 `gpio_pin_get` 判断，先读 `IN1` 寄存器确认电气状态，再回头查驱动。落在 `gpio0`（物理 0–31）的脚（I2C 的 GPIO0/1）读数是可信的。

---

## gpio_init_callback + gpio_add_callback + gpio_pin_interrupt_configure_dt

```c
void gpio_init_callback(struct gpio_callback *cb, gpio_callback_handler_t handler, gpio_port_pins_t pin_mask);
int gpio_add_callback(const struct device *port, struct gpio_callback *cb);
int gpio_pin_interrupt_configure_dt(const struct gpio_dt_spec *spec, gpio_flags_t flags);
```

`gpio_esp32_pin_interrupt_configure()` 里：先关中断、清状态位、写 `INT_TYPE`（边沿/电平），最后 `gpio_ll_intr_enable_on_core()`。ISR 由 `gpio_esp32_isr()` 统一处理，清状态后按实例分发到 `gpio_fire_callbacks()`。

```c
static volatile uint32_t cb_count;
static volatile uint32_t cb_pins;
static struct gpio_callback key_cb;

static void on_key(const struct device *port, struct gpio_callback *cb, uint32_t pins)
{
	cb_count++;
	cb_pins = pins;
}

gpio_init_callback(&key_cb, on_key, BIT(key.pin));
ret = gpio_add_callback(key.port, &key_cb);
ret = gpio_pin_interrupt_configure_dt(&key, GPIO_INT_EDGE_BOTH);
```

板子输出：

```text
[init_callback] gpio_add_callback = 0, pin_mask = 0x20000000
[interrupt] 装 EDGE_BOTH = 0
[interrupt] 现在按几下 BOOT 键（10 秒窗口）...
[interrupt] 轮询到电平变化 0 次，回调 0 次
[interrupt] 最后 pins = 0x00000000，当前电平 = 1
[interrupt] GPIO_INT_DISABLE = 0
```

`pin_mask = 0x20000000` 就是 `BIT(29)`，说明回调挂的位置是对的；但**窗口里既没有轮询到电平变化，也没有回调**——和上一节 `IN1` 恒 0 是同一件事的两个表现。

✅ **自检**：`gpio_add_callback` 回 0 只证明回调链挂上了；要判断中断是否真的在工作，必须看"回调次数"随按键增长。在本板上这次是 0，属于**平台侧未闭环**，不是你的代码写错，判据见上一节三步。

---

## gpio_get_pending_int

```c
int gpio_get_pending_int(const struct device *dev);
```

读中断状态寄存器。ESP32 上 `gpio0` 走低 32 位状态、`gpio1` 走高 32 位状态。

```c
ret = gpio_get_pending_int(key.port);
shell_print(sh, "[pending] gpio_get_pending_int(gpio1) = %d", ret);
```

板子输出：

```text
[pending] gpio_get_pending_int(gpio1) = 0
```

---

## 这一页要带走的东西

| 结论 | 证据 | 怎么用 |
|---|---|---|
| 纯输出脚回读不可信 | `set=0` 但 `get=0`、`get_raw` 位为 0 | 验证输出要靠外部现象，不要靠回读 |
| `GPIO_OPEN_SOURCE` 不支持 | 返回 `-ENOTSUP` + 内核 `<err>` | 需要开源就用外部上拉 |
| `gpio1`（物理 ≥32）的输入/中断在这套 PR 里没闭环 | `ENABLE1` 无 bit29、`IN1` bit29 恒 0、回调 0 次 | 先用 `lab key` 读原始寄存器定位，再决定是否用该脚 |
| bank0 的输入是可信的 | `IN=0x57f00003`，GPIO0/1 有外部上拉读 1 | 做输入示例优先用 GPIO0/1 |

---

上一篇：[驱动总览](README.md) ｜ 下一篇：[I2C](I2C.md)
