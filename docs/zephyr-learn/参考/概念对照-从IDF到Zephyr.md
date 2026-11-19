# 从 ESP-IDF 到 Zephyr：概念对照

你有 IDF 底子，学 Zephyr 最快的办法不是"从头学一套"，而是"把每个 IDF 概念映射到 Zephyr 里对应什么，再找出没有对应物的那部分"。

## 一张总表

| 你在 IDF 里的做法 | Zephyr 里对应 |
|---|---|
| `app_main()` | `main()` 本身就是一个线程，跑完不返回也不会退出（可以在里面 `while(1)` 或调 `k_thread_join`） |
| FreeRTOS `xTaskCreate` | `k_thread_create()`；Zephyr 没有"任务"，只有线程，优先级数字**越小越高**（和 FreeRTOS 相反） |
| `vTaskDelay` | `k_sleep(K_MSEC(n))` / `k_msleep()` |
| FreeRTOS queue | `k_msgq`（定长消息）/ `k_fifo`（指针队列） |
| 事件组 | `k_event` |
| 二值信号量 | `k_sem` |
| 互斥量 | `k_mutex` |
| 软件定时器 | `k_timer` |
| `esp_timer_get_time()` | `k_uptime_get()` |
| `menuconfig` / `sdkconfig` | `Kconfig` + `prj.conf`（每个工程自己的配置，不随组件继承） |
| `idf_component_register` / 组件 | CMake `target_sources` + `zephyr_library()`；模块生态走 west manifest |
| `ESP_LOGI(TAG, ...)` | `LOG_INF("...")`（`CONFIG_LOG`，可延时/即时两种后端） |
| `gpio_config()` + `gpio_set_level()` | dts 里 `gpios = <&gpioX n GPIO_ACTIVE_HIGH>` + `gpio_pin_configure_dt()` |
| `i2c_master_init()` + `i2c_master_transmit()` | dts 里 `i2c@...` 节点 + `i2c_transfer_dt()`，句柄由 `DEVICE_DT_GET()` 拿 |
| `ledc_*` | `pwm` API：`pwm_set_dt()` / `pwm_set_cycles()` |
| `spi_bus_initialize` | dts 里 SPI 节点 + `spi_transceive_dt()` |
| `adc_oneshot_read` | `adc_read_dt()` + `ADC_CHANNEL_ID()` |
| `esp_restart()` | `sys_reboot()`；`esp_deep_sleep_start()` → `sys_poweroff()`（要有 `HAS_PM`/`sys_poweroff` 支持） |
| `idf.py build` | `west build` |
| `idf.py flash` | `west flash` |
| `idf.py monitor` | `west espressif monitor` 或任意串口工具 |
| `idf.py set-target esp32s3` | 不需要——`-b <board>` 决定一切 |

## 三个真正的新概念（花时间在这里）

### 1. Devicetree 是"硬件的事实来源"

IDF 里引脚是代码里的宏；Zephyr 里引脚写在 `.dts`，代码只引用节点。

```dts
&i2c0 {
    status = "okay";
    clock-frequency = <100000>;
    /* pinctrl 决定 SDA/SCL 落在哪两个脚，通常在 board dts 里已有 default 节点 */
};
```

代码里：

```c
#define I2C DEVICE_DT_GET(DT_NODELABEL(i2c0))
if (!device_is_ready(I2C)) { return -ENODEV; }
```

三个必须记住的宏：
- `DT_NODELABEL(x)` —— 按 label 找节点
- `DT_ALIAS(x)` —— 按 `aliases` 找（`led0`、`sw0` 这种通用名）
- `DT_CHOSEN(x)` —— 系统级约定节点（`zephyr,console`、`zephyr,flash`）

**overlay** 就是在不改 board dts 的前提下，往这棵树上打补丁：`boards/<board>.overlay` 或 `app.overlay`。

### 2. Kconfig 是"功能开关"，不是"硬件配置"

一个驱动只有它依赖的 `DT_HAS_*` 为真时才会被编进去；但你还得在 `prj.conf` 里 `CONFIG_xxx=y` 才会真的实例化。所以"我在 dts 里写了节点，为什么没有驱动？"绝大多数时候是 `prj.conf` 少了一行。

### 3. 驱动 API 是统一的

同一个 `i2c_transfer()`，在 STM32、nRF、ESP32 上都是这么写。这是 Zephyr 最值钱的部分，也是它现在对 S31 支持不全时最难受的部分——API 在，驱动没写。

## 建议的"对照式"读法

读一个 Zephyr sample 时，按这个顺序问自己四个问题：

1. 这个 sample 依赖哪个硬件？→ 去 board dts 里找对应节点
2. 这个节点是谁把它打开的？→ `status = "okay"`
3. 这个驱动靠哪几行 `CONFIG_` 才编进来？→ `prj.conf`
4. 代码里怎么拿到设备句柄？→ `DEVICE_DT_GET`

把这四个问题在 `samples/basic/blinky`、`samples/drivers/led_strip`、`samples/subsys/shell/shell_module` 上各走一遍，Zephyr 的骨架就通了。
