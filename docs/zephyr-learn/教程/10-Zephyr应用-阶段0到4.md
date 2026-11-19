# 10 · Zephyr 应用（阶段 0–4）

这一章讲"应用层怎么写"，驱动细节留给第 11 章。

---

## 阶段 0 · 先把一个 sample 拆开看

`zephyr/samples/basic/blinky` 只有 30 行代码，但它把 Zephyr 的三层结构全暴露了。

**① `prj.conf`：我要开哪些功能**
```
CONFIG_GPIO=y
```
就这一行。它决定 `gpio` 子系统被编译进去。

**② `src/main.c`：代码怎么拿设备**
```c
#define LED0_NODE DT_ALIAS(led0)                  /* 从 devicetree 里按别名找 */
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(LED0_NODE, gpios);
```
注意这里**没有任何引脚号**——引脚号在 dts 里。

**③ board 的 `.dts`：硬件真相**
```dts
aliases {
    led0 = &blue_led;
};
leds {
    compatible = "gpio-leds";
    blue_led: led_1 {
        gpios = <&gpio0 7 GPIO_ACTIVE_HIGH>;
    };
};
```

对照关系记成一句话：**dts 描述"有个灯接在哪个脚"，prj.conf 决定"gpio 驱动要不要编进来"，main.c 只负责"用它"**。

### 自检

- `DT_ALIAS(led0)` 和 `DT_NODELABEL(blue_led)` 有什么区别？（前者按 `aliases` 里的通用名找，后者按节点 label 找；`led0` 这种通用别名让同一份代码能跑在不同板子上）
- 如果板子上没有 `led0` 别名，编译会报什么？（`DT_ALIAS` 展开失败，`undefined reference` 或 `Unused variable`）

---

## 阶段 1 · hello_world

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon-ide\west
west build -p -b esp32s31_function_coreboard/esp32s31/hpcore samples/hello_world
```

`-p` 是 pristine，等价于删掉 `build/` 重来。改 dts / 改 prj.conf 之后**必须加** `-p`，否则 CMake 缓存会让你怀疑人生。

板子的名字分两段：`esp32s31_function_coreboard`（板）+ `/esp32s31/hpcore`（SoC / 核）。少写后半段会报 "board qualifier not found"。

源码就一句：

```c
#include <stdio.h>
int main(void) { printf("Hello World! %s\n", CONFIG_BOARD_TARGET); return 0; }
```

`CONFIG_BOARD_TARGET` 是构建系统注入的字符串，能打印出来说明 Kconfig、DT、链接、串口全都通了——这也是为什么它是最有价值的第一课。

### 阶段 1 的自检

- 串口打印的板名是什么？和 `-b` 里写的一致吗？
- `west build -t pristine` 和 `-p` 是一回事吗？（是）
- 不接串口，怎么确认程序在跑？（`west flash` 后看板载 RGB 灯；或者用 `west espressif monitor`）

---

## 阶段 2 · shell：把硬件当命令行玩

`hello_world` 只能打印。**shell 才是 Zephyr 学习效率的放大器**——它把设备树里注册的每个外设变成一条命令。

做法见 12 章，核心是这一组配置：

```
CONFIG_SHELL=y
CONFIG_SHELL_BACKEND_SERIAL=y
CONFIG_DEVICE_SHELL=y      → device list
CONFIG_I2C_SHELL=y         → i2c scan / i2c read / i2c write
CONFIG_ADC_SHELL=y         → adc <dev> resolution / adc <dev> read
CONFIG_GPIO_SHELL=y        → gpio get / gpio set / gpio conf
CONFIG_KERNEL_SHELL=y      → kernel thread list / kernel uptime / kernel cycles
```

串口里敲 `device list`，实测输出（Korvo-1，本项目 `applications/esp32s31/korvo_shell`）：

```
devices:
- ldo1@1 (READY)      DT node labels: ldo1
- clock (READY)       DT node labels: clock
- gpio@20583800 (READY)  DT node labels: gpio1
- gpio@20583000 (READY)  DT node labels: gpio0
- uart@2038a000 (READY)  DT node labels: uart0
- adc@20815000 (READY)   DT node labels: adc0
- i2c@20385000 (READY)   DT node labels: i2c0
```

**这张表就是你板子上"Zephyr 认为存在"的外设清单**，比翻 dts 快十倍。注意 `adc0` 只有在你 overlay 里写了 `status = "okay"` 才会出现——它出厂是 disabled 的。

### 阶段 2 的三条实测坑（我全踩了）

```
uart:~$ i2c scan i2c0
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:             -- -- -- -- -- -- -- -- -- -- -- --
10: 10 -- -- -- 14 -- -- -- -- -- -- -- -- -- -- --
2 devices found on i2c0
```

**0x10 和 0x14 是真的存在**（ES8389 音频 codec 就在这条总线上，SDA=GPIO0/SCL=GPIO1）。这条命令是验证引脚改对了没有的最快方式——scan 出东西说明 pinctrl 生效了。

```
uart:~$ adc read 1
read: command not found
```

**ADC shell 是两层结构**：先选设备，再选动作。

```
uart:~$ adc adc@20815000 resolution 17
uart:~$ adc adc@20815000 read 1
read: 2247
<wrn> adc_esp32: ADC reading is uncompensated
```

`resolution` 为什么是 17 而不是 12？因为 S31 的 ADC 是 **17 位**架构（`soc_caps.h` 里 `SOC_ADC_DIGI_MIN_BITWIDTH` 和 `MAX_BITWIDTH` 都是 17），驱动只认 17，填 12 会报 `unsupported resolution`。`uncompensated` 警告是校准缺失，读数可用但会有偏差。

```
uart:~$ kernel threads
threads: command not found
uart:~$ kernel thread list
*0x2f016b10 shell_uart  stack 4096, unused 2884, usage 29 %
 0x2f0165a8 logging     stack 768,  unused 564,  usage 26 %
 0x2f017050 idle        stack 512,  unused 368,  usage 28 %
```

**命令名是 `kernel thread list`，不是 `kernel threads`**。这张栈用量表就是调栈大小的依据。

### 阶段 2 的自检

- `device list` 里为什么默认没有 `adc0`？（dts 节点 `status = "disabled"`，要 overlay 里改 `okay`）
- `kernel thread list` 里 `shell_uart` 用了 4096 中的多少？如果只有 10%，能砍到 2048 吗？


---

## 阶段 3 · 不写代码，先用 shell 摸引脚

这一步的价值在于：**在外设 API 之前，先确认硬件和 DT 是对的**。

```text
uart:~$ gpio get gpio0 7          # 读 PA_CTRL 当前电平
uart:~$ gpio conf gpio0 7 out     # 配成输出
uart:~$ gpio set gpio0 7 1        # 拉高 → 功放使能
uart:~$ i2c scan i2c0             # 扫总线上所有地址，应看到 ES8389
uart:~$ adc read 1                # 读 ADC 通道 1（按键分压）
```

`gpio conf/set` 是**运行时**改配置，不需要重新编译。这比"改代码→编译→烧录→看结果"快一个数量级，也是 Zephyr 相比裸机开发最舒服的地方之一。

### 阶段 3 的自检

- `i2c scan i2c0` 报 "no devices found"，可能是哪三件事？（引脚 pinctrl 没配对 / 从设备没供电 / 上拉电阻缺失）
- 为什么 `gpio set` 之后用万用表量 GPIO7，能确认"DT 里的 bank 和 pin 算对了"？

---

## 阶段 4 · 线程、队列、定时器

Zephyr 没有"任务"这个词，只有**线程**。`main()` 本身跑在一个线程里。

### 4.1 线程的最小写法

```c
#include <zephyr/kernel.h>

#define STACK_SIZE 1024
#define PRIO       5          /* 数字越小优先级越高，和 FreeRTOS 相反 */

K_THREAD_STACK_DEFINE(worker_stack, STACK_SIZE);
static struct k_thread worker_thread;

static void worker(void *a, void *b, void *c)
{
    while (1) {
        printk("worker alive, uptime=%lld ms\n", k_uptime_get());
        k_sleep(K_MSEC(1000));
    }
}

int main(void)
{
    k_thread_create(&worker_thread, worker_stack, K_THREAD_STACK_SIZEOF(worker_stack),
                    worker, NULL, NULL, NULL, PRIO, 0, K_NO_WAIT);
    return 0;
}
```

三个容易踩的点：

1. **栈要自己定义**（`K_THREAD_STACK_DEFINE`），Zephyr 不会替你分配；
2. **优先级数字方向**和 FreeRTOS 相反；
3. `main` 返回后线程并不会退出，它变成 idle 前的"main 线程"继续存在（`CONFIG_MAIN_THREAD_PRIORITY` 决定它的优先级）。

### 4.2 队列：把按键事件送给 LED 线程

```c
K_MSGQ_DEFINE(evt_q, sizeof(uint32_t), 8, 4);

/* 生产者 */
uint32_t evt = 1;
k_msgq_put(&evt_q, &evt, K_NO_WAIT);

/* 消费者 */
uint32_t got;
if (k_msgq_get(&evt_q, &got, K_FOREVER) == 0) { /* 处理 */ }
```

`k_msgq` 是**定长消息**队列（拷贝数据），`k_fifo` 是**指针**队列（只传引用）。按键事件这种小数据用 msgq，大数据块用 fifo。

### 4.3 定时器

```c
static void tick(struct k_timer *t) { /* 在系统时钟中断上下文里跑，不能阻塞 */ }
K_TIMER_DEFINE(my_timer, tick, NULL);
k_timer_start(&my_timer, K_SECONDS(1), K_SECONDS(1));
```

`k_timer` 的回调运行在**中断上下文**，里面不能 `k_sleep`、不能拿会阻塞的锁。要阻塞就发信号量唤醒线程。

### 4.4 日志

```c
LOG_MODULE_REGISTER(app, LOG_LEVEL_INF);
LOG_INF("value=%d", v);
```

`CONFIG_LOG=y` 之后才有。默认是 deferred 模式（日志进缓冲，由日志线程输出），所以 `LOG_INF` 本身几乎不耗时——这点和 `printk` 完全不同。

---

## 阶段 4 的完整练习

目标：按键线程 → 队列 → LED 线程，主线程不参与。

```c
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(app, LOG_LEVEL_INF);
K_MSGQ_DEFINE(key_q, sizeof(int), 8, 4);

static const struct gpio_dt_spec btn = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);

static void btn_thread(void *a, void *b, void *c)
{
    gpio_pin_configure_dt(&btn, GPIO_INPUT);
    int last = 1;
    while (1) {
        int now = gpio_pin_get_dt(&btn);
        if (now != last) {                 /* 轮询版，先跑通再换中断 */
            int v = now;
            k_msgq_put(&key_q, &v, K_NO_WAIT);
            last = now;
        }
        k_msleep(10);
    }
}

static void led_thread(void *a, void *b, void *c)
{
    int v;
    while (1) {
        k_msgq_get(&key_q, &v, K_FOREVER);
        LOG_INF("button -> %d", v);
    }
}
```

跑通之后再把轮询换成 `gpio_init_callback` + `gpio_add_callback` + `k_sem_give`——那是阶段 4 的进阶作业。

---

## 自检

1. `k_sleep(K_MSEC(1))` 和 `k_busy_wait(1000)` 的区别？
2. 为什么 `k_timer` 回调里不能 `k_msleep`？
3. `main` 线程的栈有多大？在哪改？（`CONFIG_MAIN_STACK_SIZE`）
4. 线程优先级数字 3 和 7，谁先跑？
