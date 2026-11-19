# ADC

> **主线（S3）注意**：S3 的 ADC 是 **12 位**，`adc_channel_setup` 里填 17 会直接回 `unsupported resolution`。写法是**从 17 往下探测**（见 `applications\esp32s31\korvo_apps\src\main.c` 的 `adc_probe_read()`），这样同一份代码在 S31（17 位）和 S3（12 位）都能跑。
> 另外：本文 §"unit 1 / unit 2"这段是 **S31 的 SoC dtsi 原文**，而它恰好是错的（`unit` 一基错位），我们踩到的完整证据在 `docs\课程同款\07-七项外设落地实录.md` §9.1。**这段当"反面教材"读，别照抄到 S31 的 overlay**；S3 那边 dtsi 的 unit 编号是对的。

这块板子的 ADC 有两个"反直觉"的点，都是实测撞出来的：

1. **分辨率只能填 17（仅 S31）。** 填 12 会回 `unsupported resolution (12)`，因为驱动用 `SOC_ADC_DIGI_MIN_BITWIDTH` / `MAX_BITWIDTH` 校验，S31 这两个宏都是 17。
2. **读数没有校准。** 每次 `adc_read` 之后都会打一条 `ADC reading is uncompensated` 警告——没有 efuse 校准数据时这是正常的，数值能用但绝对值不准。

本页所有输出来自 `lab adc`。

## 设备树与 Kconfig

SoC 里有 `adc0`（unit 1）和 `adc1`（unit 2），各 8 个通道，默认 `disabled`。应用 overlay 只打开 `adc0`：

```dts
&adc0 {
	status = "okay";
};
```

```conf
CONFIG_ADC=y
```

设备实际名是 `adc@20815000`。

📖 这块板子上 GPIO42 接了 ADC 按键分压，但**GPIO42 落在哪个 unit、哪个 channel 上，需要 S31 数据手册才说得准**，本教程标"待核对"。下面 8 个通道读到的是"通道号对应的引脚当前电平"，不代表某一号通道就是按键。

---

## adc_channel_setup

```c
struct adc_channel_cfg {
	enum adc_gain gain;              /* 增益 */
	enum adc_reference reference;    /* 参考源 */
	enum adc_acq_time acquisition_time;
	uint8_t channel_id;              /* 通道号 */
	/* 差分输入时还有 input_positive / input_negative */
};

int adc_channel_setup(const struct device *dev, const struct adc_channel_cfg *cfg);
```

每个通道**必须先 setup 再 read**。本页用的是 Zephyr shell 里 `adc` 命令的默认参数：`ADC_GAIN_1`（0 dB）、`ADC_REF_INTERNAL`（内部参考）、`ADC_ACQ_TIME_DEFAULT`（驱动默认采样时间）。

```c
struct adc_channel_cfg cfg = {
	.gain = ADC_GAIN_1,
	.reference = ADC_REF_INTERNAL,
	.acquisition_time = ADC_ACQ_TIME_DEFAULT,
	.channel_id = ch,
};
int ret = adc_channel_setup(adc_dev, &cfg);
```

本机 8 个通道全部 `setup` 成功（没有打印失败行，说明返回都是 0）。

---

## adc_read

```c
int adc_read(const struct device *dev, const struct adc_sequence *sequence);

struct adc_sequence {
	uint32_t channels;      /* 位掩码：读哪几路 */
	uint8_t resolution;     /* 分辨率，位 */
	void *buffer;           /* 结果缓冲 */
	size_t buffer_size;
	/* 还有 oversampling / calibration 等可选字段 */
};
```

三个要点：

1. `channels` 是**位掩码**，`BIT(ch)` 表示读第 ch 路；
2. `resolution` 必须落在驱动允许区间——S31 上只有 17；
3. 17 位 > 16 位，所以 `buffer` 要用 `uint32_t`（每路 4 字节），用 `uint16_t` 会把数据截断。

```c
int32_t raw = 0;
struct adc_sequence seq = {
	.resolution = 17,
	.buffer = &raw,
	.buffer_size = sizeof(raw),
	.channels = 0,
};

seq.channels = BIT(ch);
ret = adc_read(adc_dev, &seq);
```

8 个通道的实测（`resolution 17`、`gain 1`）：

```text
[node] adc=adc@20815000, resolution=17
[read] ch0 原始=0
       约 0 mV
[read] ch1 原始=2246
       约 18 mV
[read] ch2 原始=2148
       约 18 mV
[read] ch3 原始=2244
       约 18 mV
[read] ch4 原始=1918
       约 16 mV
[read] ch5 原始=2530
       约 21 mV
[read] ch6 原始=1826
       约 15 mV
[read] ch7 原始=2536
       约 21 mV
```

这 8 次读完后，串口上会连着出现 8 行警告：

```text
[00:00:12.308,000] <wrn> adc_esp32: ADC reading is uncompensated
...（每次 adc_read 一行，共 8 行）
```

✅ **自检**：`ch0 = 0`，其余通道在 1800–2500 之间徘徊（约 15–21 mV）。悬空脚读到几百毫伏级的漂移是正常的；`ch0` 恒 0 说明那一路被拉到地或没接通。**没有任何一路接近"按键分压"的量级**（按键分压通常是几百 mV 到 1 V 级别），这也印证了 GPIO42 的映射还没核对。

⚠️ 如果你把 `resolution` 写成 12，会直接看到：

```text
[read] ch1 = -ENOTSUP
```

（驱动打印的是 `unsupported resolution (12)`，返回 `-ENOTSUP`。）教程里凡是看到"填某个常用值就行"的说法，都要先确认你的 SoC 允许哪些值。

---

## adc_raw_to_millivolts

```c
int adc_raw_to_millivolts(int32_t ref_mv, enum adc_gain gain, uint8_t resolution, int32_t *valp);
```

把原始码换算成 mV。`ref_mv` 传**满量程参考电压**（本页按 1100 mV 算，对应 0 dB 增益下的内部参考），它和 `gain`、`resolution` 一起决定比例。

```c
int32_t mv = raw;
ret = adc_raw_to_millivolts(1100, ADC_GAIN_1, 17, &mv);
if (ret == 0) {
	shell_print(sh, "       约 %d mV", mv);
}
```

换算结果就是上面输出里的"约 xx mV"：`2246 → 18 mV`、`2536 → 21 mV`。

⚠️ 换算只对"参考电压 + 增益"这个组合成立。S31 的实际参考电压、以及 17 位满量程码值是 `2^17 - 1` 还是别的，都要以数据手册为准；这里给的是**按 1100 mV/2^17 线性推算**的值，用来判断"相对大小"够用，绝对精度不保证（还叠加了未校准这一层）。

---

## 完整命令

```powershell
.\.deps\venv\Scripts\python.exe tools/zephyr/serial_cmd.py COM9 "lab adc@4"
```

如果你只想用 shell 自带的命令，注意它的语法和 C API 不同：

```text
uart:~$ adc adc@20815000 resolution 17
uart:~$ adc adc@20815000 read 1
read: 2247
```

三处容易踩的坑（都实测过）：

- `adc read 1` → `command not found`：命令是 `adc <节点名> read <通道>`；
- 分辨率必须是 17，填 12 报 `unsupported resolution (12)`；
- 节点名要用 `adc@20815000`，不能用别名 `adc0`。

---

## 这一页要带走的东西

| 结论 | 证据 |
|---|---|
| 分辨率只接受 17 | 驱动用 `SOC_ADC_DIGI_MIN/MAX_BITWIDTH` 校验 |
| 17 位要用 32 位缓冲 | `uint32_t raw` + `buffer_size = sizeof(raw)` |
| 未校准是正常状态 | 每次读后 8 行 `uncompensated` 警告 |
| GPIO42 的通道映射待核对 | 8 路读数都在 15–21 mV，没有一路像按键分压 |

---

上一篇：[I2C](I2C.md) ｜ 下一篇：[WS2812](WS2812.md)
