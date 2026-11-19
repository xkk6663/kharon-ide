# Korvo-1 管脚分配（官方口径，可直接用于 devicetree）

来源：乐鑫官方用户指南 V1.1 的「管脚分配列表」
<https://docs.espressif.com/projects/esp-dev-kits/zh_CN/latest/esp32s31/esp32-s31-korvo-1/user_guide.html>

| 外设 | 信号 | GPIO | 说明 |
|---|---|---|---|
| 音频 I2S | I2S_MCLK | **GPIO2** | 主时钟 |
| | I2S_SCLK | **GPIO3** | 串行时钟 |
| | I2S_LRCLK | **GPIO4** | 帧时钟（左右声道） |
| | I2S_SDOUT | **GPIO5** | 音频输出（DAC 播放） |
| | I2S_DSIN | **GPIO6** | 音频输入（ADC 录音） |
| 音频 I2C | I2C_SDA | **GPIO0** | ES8389 数据线 |
| | I2C_SCL | **GPIO1** | ES8389 时钟线 |
| 系统 | PA_CTRL | **GPIO7** | 左右声道功放使能 |
| | WS2812_CTRL | **GPIO37** | 可寻址 RGB LED |
| | ADC BUTTON | **GPIO42** | PLAY/SET/VOL-/VOL+ 电阻分压检测 |
| microSD / SPI NAND | SDIO_DATA0 | **GPIO20** | 默认 microSD；切 NAND 时为 SPI2_CLK |
| | DB15 (R7) | **GPIO36** | LCD 数据位 15（换料电阻 R7 决定） |

补充事实：
- microSD 与 SPI NAND **共用 GPIO20–GPIO25**，默认是 microSD。切 NAND 要改焊（拆 R7/R65–R69，装 R22/R23/R1–R4/C6/R20/U4 等），不是软件能切的。
- BOOT 按键 = **GPIO61**；自动下载由 CP2102N 的 DTR/RTS 控制 EN 与 GPIO61。
- 串口 console = 模组的 `TX0/RX0`（模块符号第 68/69 脚），走板载 CP2102N。
- V1.0 与 V1.1 的 GPIO 配置完全相同，只有 PCB 尺寸和 LCD 叠放方式变了。

## 这些引脚在 Zephyr 里怎么用（按学习价值排序）

| 实验 | 引脚 | 用到的 Zephyr 子系统 | 备注 |
|---|---|---|---|
| 设备清单 | — | `CONFIG_DEVICE_SHELL` → `device list` | 最省事的第一步，看 Zephyr 认出了什么 |
| I2C 扫描 | GPIO0/1 | `i2c` + `CONFIG_I2C_SHELL` → `i2c scan` | 能扫到 ES8389，顺便验证总线 |
| ADC 读按键 | GPIO42 | `adc` + `CONFIG_ADC_SHELL` → `adc read` | 4 个键是电阻分压，读数分档即可判断 |
| GPIO 输出 | GPIO7 | `gpio` + `CONFIG_GPIO_SHELL` → `gpio set` | 拉高即功放使能，可听到底噪变化 |
| WS2812 | GPIO37 | `led_strip`（底层 RMT） | PR 作者明确说这条路已验证 |
| 呼吸灯 | 40Pin 排针上的空闲 IO | `pwm`（LEDC） | 板载没有普通 LED，得外接或飞线 |
| 深睡 | — | `sys_poweroff()` | 该 PR 没有 `HAS_PM`，只能用这个 |

## 引脚号 → Zephyr GPIO 端口

Zephyr 里访问引脚要写成 `&gpioX <pin>`，S31 的 62 个 GPIO 会分布于 `&gpio0` / `&gpio1` /（可能还有 `&gpio2`）。
**不要猜分配，打开** `zephyr/boards/espressif/esp32s31_function_coreboard/` 下的 `.dts`，看 `gpio0`/`gpio1` 节点的 `ngpios`，就能算出 GPIO37 落在哪个 bank、pin 号是多少。

例（假设某板 dts 写 `gpio1: gpio@... { ngpios = <32>; }` 且从 32 起算）：

```dts
/ {
    aliases {
        led-strip = &ws2812;
    };

    ws2812: ws2812 {
        compatible = "worldsemi,ws2812-rpi";
        chain-length = <1>;
        color-mapping = <0 1 2>;          /* 按实际 RGB 顺序调 */
        gpios = <&gpio1 5 GPIO_ACTIVE_HIGH>;   /* ← GPIO37，bank 和 pin 号以 board dts 为准 */
    };
};
```

## 别指望的引脚/外设

- I2S（GPIO2–6）：S31 的 I2S 驱动不在这个 PR 的已支持列表里，写了也编不出驱动。
- LCD（GPIO36 等 23 根）：ST7262E43 在 Zephyr 里没有驱动。
- 摄像头（13 根 IO）：DVP 无驱动。
