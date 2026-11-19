# 12 · Zephyr 组件

Zephyr 把"能独立开关的子系统"都叫组件（subsys）。学 Zephyr，除了驱动，最常用的是下面三个。

---

## 1. shell：板子上的命令行

### 1.1 为什么它是最值得先学的组件

- 把每个外设变成一条命令，**不用编译就能改配置**；
- 自带 `device list`、`i2c scan`、`adc read`、`gpio set`、`kernel threads`；
- 你自己的调试命令也能挂上去，比 `printk` 高级得多。

### 1.2 打开它

```
CONFIG_SHELL=y
CONFIG_SHELL_BACKEND_SERIAL=y      # 串口后端，通常默认就是 y
CONFIG_DEVICE_SHELL=y
CONFIG_I2C_SHELL=y
CONFIG_ADC_SHELL=y
CONFIG_GPIO_SHELL=y
CONFIG_KERNEL_SHELL=y
```

**shell 是自动启动的**（`SYS_INIT` 里初始化后端），`main()` 里不用 `shell_init()`。这点很多人搞错，结果写了个手动初始化反而冲突。

### 1.3 注册自己的命令

三层结构，从简到繁：

```c
/* ① 顶层命令 */
static int cmd_hello(const struct shell *sh, size_t argc, char **argv)
{
    shell_print(sh, "argc=%zu argv[1]=%s", argc, argc > 1 ? argv[1] : "(none)");
    return 0;
}
SHELL_CMD_REGISTER(hello, NULL, "打招呼", cmd_hello);
```

```c
/* ② 带子命令 */
SHELL_STATIC_SUBCMD_SET_CREATE(sub_korvo,
    SHELL_CMD(pins, NULL, "打印管脚分配", cmd_pins),
    SHELL_CMD(pa,   NULL, "拉高 PA_CTRL",    cmd_pa),
    SHELL_SUBCMD_SET_END);
SHELL_CMD_REGISTER(korvo, &sub_korvo, "板级命令", NULL);
```

```c
/* ③ 返回值有意义 */
return -ENOEXEC;   /* shell 会打印错误，并给出该命令的 help */
```

三个细节：

- 参数校验用 `shell_error(sh, "...")`，别用 `printk`——`printk` 会绕过 shell 的行缓冲，输出错位；
- `SHELL_CMD` 的第二个参数是 `NULL`（没有子命令集），不是省略；
- 命令名和变量名都影响编译期的段布局，重复注册同名命令会在构建期报错。

### 1.4 在 Korvo-1 上的实测命令

```
uart:~$ device list          # Zephyr 认出的所有外设
uart:~$ i2c scan i2c0        # 扫 ES8389
uart:~$ gpio get gpio0 7     # 读 PA_CTRL
uart:~$ adc read 1           # 读按键分压
uart:~$ kernel threads       # 线程状态/栈使用
uart:~$ korvo pins           # 本项目自定义
```

`kernel threads` 的输出里有个 `stack usage`，**它是调栈大小的唯一可靠依据**——不要凭感觉给 `K_THREAD_STACK_DEFINE` 定 1024。

---

## 2. logging：比 printk 强在哪

```
CONFIG_LOG=y
CONFIG_LOG_MODE_DEFERRED=y     # 进缓冲区，由日志线程输出
CONFIG_LOG_DEFAULT_LEVEL=3     # 0=off 1=err 2=wrn 3=inf 4=dbg
```

```c
LOG_MODULE_REGISTER(app, LOG_LEVEL_DBG);
LOG_ERR("..."); LOG_WRN("..."); LOG_INF("..."); LOG_DBG("...");
```

三个要点：

1. **每个文件一个 module**，`LOG_MODULE_REGISTER` 只能出现一次；
2. **deferred 模式下 `LOG_INF` 是几乎不耗时的**（写环形缓冲就返回），所以可以在中断里用；`printk` 是同步输出，中断里用会拖慢系统；
3. **日志线程自己占一个线程和栈**（`CONFIG_LOG_PROCESS_THREAD_STACK_SIZE`），资源紧的时候要算进去。

实测：`hello_world` 132 KB，加了 shell + logging 后约 250 KB —— 多出来的就是这些子系统。16 MB flash 完全不在乎，但要知道钱花在哪。

---

## 3. settings：掉电不丢的配置

```
CONFIG_SETTINGS=y
CONFIG_SETTINGS_NVS=y          # ESP32 上落在 NVS 分区
CONFIG_NVS=y
```

```c
#include <zephyr/settings/settings.h>

static uint32_t volume = 50;

static int vol_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
    return read_cb(cb_arg, &volume, sizeof(volume));
}
SETTINGS_STATIC_HANDLER_DEFINE(vol, "app/volume", NULL, vol_set, NULL, NULL);

/* 启动时 */
settings_subsys_init();
settings_load();

/* 修改后 */
settings_save_one("app/volume", &volume, sizeof(volume));
```

`settings` 的价值在于**分区感知**：你只管 key，落盘位置由 dts 里的 `storage_partition` 决定。换芯片不用改代码。

---

## 4. 其它值得知道的

| 组件 | CONFIG | 用途 |
|---|---|---|
| `sys_reboot` | `CONFIG_REBOOT` | 软重启，比看门狗喂狗失败更可控 |
| `watchdog` | `CONFIG_WATCHDOG` | 板上有 `wdt0`，`wdt_setup` + 定时 `wdt_feed` |
| `stats` | `CONFIG_STATS` | 计数器，比全局变量安全 |
| `ring_buffer` | `CONFIG_RING_BUFFER` | 中断里写、线程里读的标准做法 |
| `zbus` | `CONFIG_ZBUS` | 线程间发布/订阅，比全局变量更结构化 |

`zbus` 是 Zephyr 特有的东西，本质是"带通道的消息总线"，值得单独花半天看文档。

---

## 自检

1. shell 命令里为什么不能用 `printk`？
2. `LOG_MODE_DEFERRED` 下日志丢了怎么办？（缓冲区满会丢，调 `CONFIG_LOG_BUFFER_SIZE`，或改成 immediate）
3. `settings` 的存储位置在哪定义？（dts 的 `storage_partition` / `zephyr,settings-nvs` chosen 节点）
4. `kernel threads` 里的栈用量百分比，超过多少该调栈？
