# I2C

Korvo-1 上 I2C 是"活"的：总线一头挂着 ES8389 音频编解码器（实测地址 `0x10`），另外还有一颗器件应答 `0x14`。所以 I2C 是这块板子上**唯一能真正读写外部器件**的驱动，适合把接口一个个过一遍。

本页所有输出来自 `lab i2c`。

## 设备树与 Kconfig

`i2c@20385000` 就是 `i2c0`，SoC dtsi 里默认 `disabled`，由应用的 overlay 打开：

```dts
&i2c0 {
	pinctrl-0 = <&i2c0_korvo>;      /* SDA=GPIO0, SCL=GPIO1 */
	pinctrl-names = "default";
	clock-frequency = <I2C_BITRATE_FAST>;   /* 400 kHz */
	status = "okay";
};
```

引脚组的三个关键属性：`bias-pull-up`（板上另有外部上拉，两者叠加无妨）、`drive-open-drain`（I2C 必须开漏）、`output-high`（释放总线）。

```conf
CONFIG_I2C=y
```

`CONFIG_I2C_ESP32` 由 `DT_HAS_ESPESPRESSIF_ESP32_I2C_ENABLED` 自动选中，不用手写。

## 这颗驱动在这块板子上的能力边界

| 项目 | 实测结果 |
|---|---|
| 设备名 | `i2c@20385000` |
| 100 kHz / 400 kHz / 1 MHz | 全部返回 0（三层速率都接受，和别家驱动只收 100k/400k 不同） |
| 10 位地址 | `i2c_configure` 返回 0；`i2c_transfer` 带 `I2C_MSG_ADDR_10_BITS` 也返回 0（没有拒绝） |
| 缺 `I2C_MODE_CONTROLLER` | `-ENOTSUP`，并打内核日志 `Only I2C Controller mode supported` |
| `i2c_get_config` | 实现了，返回 0，`cfg = 0x00000014` |
| 地址超 7 位（`0x100`） | `-EIO`（不是 `-EINVAL`） |
| 器件不应答 | `-EIO` |

⚠️ 这张表里有两处"和教科书不一样"：10 位地址没有返回 `-ENOTSUP`、超范围地址返回 `-EIO` 而不是 `-EINVAL`。写通用代码时不要用这两个返回值当分支条件。

---

## i2c_configure

```c
int i2c_configure(const struct device *dev, uint32_t dev_config);
```

运行中改总线速率。`dev_config` 用 `I2C_SPEED_SET(速率) | I2C_MODE_CONTROLLER` 拼出来。ESP32 驱动每次调用都会重设分频。

```c
ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_STANDARD) | I2C_MODE_CONTROLLER);
shell_print(sh, "[configure] standard 100 kHz = %s", err(ret));

ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_FAST) | I2C_MODE_CONTROLLER);
shell_print(sh, "[configure] fast 400 kHz = %s", err(ret));

ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_FAST_PLUS) | I2C_MODE_CONTROLLER);
shell_print(sh, "[configure] fast plus 1 MHz = %s", err(ret));

ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_STANDARD) | I2C_MODE_CONTROLLER |
		    I2C_ADDR_10_BITS);
shell_print(sh, "[configure] 10 bit 地址 = %s", err(ret));

ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_STANDARD));
shell_print(sh, "[configure] 缺 I2C_MODE_CONTROLLER = %s", err(ret));
```

板子输出：

```text
[node] bus=i2c@20385000
[configure] standard 100 kHz = 0
[configure] fast 400 kHz = 0
[configure] fast plus 1 MHz = 0
[configure] 10 bit 地址 = 0
[configure] 缺 I2C_MODE_CONTROLLER = -ENOTSUP
```

紧接着串口上会出现这行内核日志，它对应最后一次调用：

```text
[00:00:03.149,000] <err> i2c_esp32: Only I2C Controller mode supported.
```

✅ **自检**：前四行全 0、最后一行 `-ENOTSUP`，和你树里这份驱动一致。如果 1 MHz 那行回 `-ENOTSUP`，说明你换到了别的驱动（例如 Allwinner 的 `i2c_sunxi`）。

---

## i2c_get_config

```c
int i2c_get_config(const struct device *dev, uint32_t *dev_config);
```

```c
uint32_t cfg = 0;
ret = i2c_get_config(bus, &cfg);
shell_print(sh, "[get_config] i2c_get_config = %s, cfg = 0x%08x", err(ret), cfg);
```

板子输出：

```text
[get_config] i2c_get_config = 0, cfg = 0x00000014
```

📖 `0x14` 里编码了"控制器模式 + 快速 400 kHz"。ESP32 驱动实现了这个接口；参考教程里的 Allwinner 驱动没实现，会回 `-ENOSYS`——同一个 API、不同驱动，能力边界就不同。

---

## i2c_write（零字节写当扫描器用）

```c
int i2c_write(const struct device *dev, const uint8_t *buf, uint32_t num_bytes, uint16_t addr);
```

发一次完整传输（结尾带停止）。`num_bytes = 0` 时只发地址——**这就是扫总线最省事的做法**：应答就返回 0，不应答返回 `-EIO`。

```c
int found = 0;
shell_print(sh, "[scan] 0x08..0x77 逐个零字节写：");
for (uint16_t a = 0x08; a <= 0x77; a++) {
	if (i2c_write(bus, NULL, 0, a) == 0) {
		shell_print(sh, "[scan] 发现 0x%02x", a);
		found++;
	}
}
shell_print(sh, "[scan] 共 %d 个从设备", found);
```

板子输出：

```text
[scan] 0x08..0x77 逐个零字节写：
[scan] 发现 0x10
[scan] 发现 0x14
[scan] 共 2 个从设备
```

✅ **自检**：扫到两颗就对了。`0x10` 是 ES8389 编解码器；`0x14` 是板上另一颗器件（地址可配的同类芯片，具体型号要看原理图）。如果只扫到一颗或零颗，先查 `status = "okay"` 和 pinctrl 是否落在 GPIO0/1。

---

## i2c_write + i2c_read（两次传输设地址再读）

```c
int i2c_read(const struct device *dev, uint8_t *buf, uint32_t num_bytes, uint16_t addr);
```

先 `write` 一次寄存器地址（结尾有停止），再 `read` 一次。**两次之间有停止条件**，只有能接受这种时序的芯片才这么用；标准做法见下面的 `i2c_write_read`。

```c
uint8_t reg = 0x00, val = 0;

ret = i2c_write(bus, &reg, 1, CODEC_ADDR);
shell_print(sh, "[write] i2c_write(reg 0x00) = %s", err(ret));

ret = i2c_read(bus, &val, 1, CODEC_ADDR);
shell_print(sh, "[read]  i2c_read(1B) = %s, val = 0x%02x", err(ret), val);
```

板子输出：

```text
[write] i2c_write(reg 0x00) = 0
[read]  i2c_read(1B) = 0, val = 0x01
```

---

## i2c_reg_read_byte（便捷封装）

```c
int i2c_reg_read_byte(const struct device *dev, uint16_t dev_addr, uint8_t reg_addr, uint8_t *value);
```

内部就是"写 1 字节地址 + 读 1 字节"，省掉两次手写调用。适合 ES8389 这种 8 位寄存器、单字节取值的器件。

```c
uint8_t regs[8] = {0};
for (uint8_t r = 0; r < 8; r++) {
	uint8_t v = 0;
	if (i2c_reg_read_byte(bus, CODEC_ADDR, r, &v) == 0) {
		regs[r] = v;
	}
}
shell_print(sh, "[regs] 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x",
	    regs[0], regs[1], regs[2], regs[3], regs[4], regs[5], regs[6], regs[7]);
```

板子输出：

```text
[regs] 0x01 0x08 0x40 0xc3 0x00 0x51 0x00 0xc0
```

✅ **自检**：这 8 个字节是 ES8389 上电后的默认值，能读出来说明"写地址 + 读数据"的时序在这颗芯片上是通的。注意 `0x00` 寄存器读出来是 `0x01`，和 `/` 上电默认一致；如果读到全 `0xff`，通常是地址发错或总线被拉死。

---

## i2c_write_read（写地址 + 重复起始 + 读数据）

```c
int i2c_write_read(const struct device *dev, uint16_t addr,
		   const void *write_buf, size_t num_write,
		   void *read_buf, size_t num_read);
```

两段传输之间用**重复起始**（restart），不发停止。读多字节寄存器时这是标准写法，也是 `i2c_reg_read_byte` 的底层形态。

```c
uint8_t burst[4] = {0};
ret = i2c_write_read(bus, CODEC_ADDR, &reg, 1, burst, sizeof(burst));
shell_print(sh, "[write_read] = %s: %02x %02x %02x %02x", err(ret),
	    burst[0], burst[1], burst[2], burst[3]);
```

板子输出：

```text
[write_read] = 0: 01 ff ff ff
```

📖 `01` 是 0x00 寄存器的值，后面 `ff ff ff` 是 ES8389 对 0x01–0x03 的应答（未定义寄存器常见回 `0xff`）。换成有连续寄存器的器件（比如加速度计、EEPROM）就能读到连贯的数据块。

---

## i2c_burst_read

```c
int i2c_burst_read(const struct device *dev, uint16_t dev_addr, uint8_t start_addr,
		   uint8_t *buf, uint32_t num_bytes);
```

和 `i2c_write_read` 的差别只有一个：**起始地址固定 1 字节**。寄存器地址是 16 位的器件（如 GT967 触摸屏）不能用它，要用 `i2c_write_read`。

```c
ret = i2c_burst_read(bus, CODEC_ADDR, 0x00, burst, sizeof(burst));
shell_print(sh, "[burst] i2c_burst_read(0x00,4) = %s", err(ret));
```

板子输出：

```text
[burst] i2c_burst_read(0x00,4) = 0
```

---

## i2c_transfer（自己拼消息）

```c
int i2c_transfer(const struct device *dev, struct i2c_msg *msgs, uint8_t num_msgs, uint16_t addr);
```

`i2c_msg` 的 `flags` 决定每段的起始/重复起始/停止：

| 标志 | 含义 |
|---|---|
| `I2C_MSG_WRITE` / `I2C_MSG_READ` | 方向 |
| `I2C_MSG_RESTART` | 这段之前发重复起始并重发地址 |
| `I2C_MSG_STOP` | 这段之后发停止（最后一段默认会发） |
| `I2C_MSG_ADDR_10_BITS` | 10 位地址 |

```c
struct i2c_msg msgs[2] = {
	{ .buf = &reg, .len = 1, .flags = I2C_MSG_WRITE },
	{ .buf = burst, .len = sizeof(burst),
	  .flags = I2C_MSG_READ | I2C_MSG_RESTART | I2C_MSG_STOP },
};

ret = i2c_transfer(bus, msgs, 2, CODEC_ADDR);
shell_print(sh, "[transfer] write+restart+read = %s", err(ret));

msgs[1].flags |= I2C_MSG_ADDR_10_BITS;
ret = i2c_transfer(bus, msgs, 2, CODEC_ADDR);
shell_print(sh, "[transfer] 10 bit 地址 = %s", err(ret));

msgs[1].flags &= ~I2C_MSG_ADDR_10_BITS;
ret = i2c_transfer(bus, msgs, 2, 0x100);
shell_print(sh, "[transfer] 地址 0x100 = %s", err(ret));
```

板子输出：

```text
[transfer] write+restart+read = 0
[transfer] 10 bit 地址 = 0
[transfer] 地址 0x100 = -EIO
```

✅ **自检**：`0x100` 那行回 `-EIO` 而不是 `-EINVAL`，说明驱动把地址截成 7 位后去总线上找，没找到自然不应答。这条和参考教程里 Allwinner 驱动的 `-EINVAL` 不同，写断言时要按自己的板子来。

---

## 没有设备应答

```c
ret = i2c_read(bus, &b, 1, GHOST_ADDR);   /* 0x50 上什么都没有 */
shell_print(sh, "[nack] 读 0x%02x = %s", GHOST_ADDR, err(ret));

ret = i2c_write(bus, &b, 1, GHOST_ADDR);
shell_print(sh, "[nack] 写 0x%02x = %s", GHOST_ADDR, err(ret));
```

板子输出：

```text
[nack] 读 0x50 = -EIO
[nack] 写 0x50 = -EIO
```

📖 不应答时驱动发停止条件让总线回空闲，所以连续 NACK 不会挂死总线——这就是扫描 0x08–0x77 能一遍跑完的前提。

---

## 完整命令与一次全流程

```powershell
.\.deps\venv\Scripts\python.exe tools\zephyr\serial_cmd.py COM9 "lab i2c@9"
```

本页所有输出都来自这一次抓取。想看别的器件，把 `CODEC_ADDR` 换掉、或用 `i2c_transfer` 手拼时序即可。

---

## 这一页要带走的东西

| 结论 | 证据 |
|---|---|
| 扫总线最省事的写法是零字节 `i2c_write` | 扫出 `0x10`、`0x14` |
| 读寄存器优先 `i2c_write_read`，别用"写一次停一次 + 读" | `write_read = 0: 01 ff ff ff` |
| 这颗驱动接受 1 MHz 和 10 位地址，超范围地址回 `-EIO` | 五行 `configure` + `transfer` 输出 |
| `i2c_get_config` 在这颗驱动上可用 | `cfg = 0x00000014` |

---

上一篇：[GPIO](GPIO.md) ｜ 下一篇：[ADC](ADC.md)
