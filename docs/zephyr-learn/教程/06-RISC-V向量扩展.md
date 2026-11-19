# 06 · RISC-V 向量扩展

这一章有两个任务：把 RVV 讲明白，以及说清**为什么 Korvo-1 上跑不了它**。

对照对象是先前提到的参考教程（YuzukiNeko，板子是 Allwinner F101 / 玄铁 C907，芯片 `rv32imacfdv`，**有 V 扩展**）。它们的 RVV 章能真跑，我们这块不能——差别在两颗芯片，不在写法。

---

## 6.1 V 扩展是什么

RISC-V 用字母串描述指令集：`rv32imafc` 就是 32 位 + I 基础整数 + M 乘除 + A 原子 + F 单精度浮点 + C 压缩指令。

| 字母 | 含义 | C907（参考板） | S31（Korvo-1） |
|---|---|---|---|
| I / M / A | 基础 / 乘除 / 原子 | ✅ | ✅ |
| F | 单精度浮点 | ✅ | ✅ |
| D | 双精度浮点 | ✅ | ❌ |
| C | 压缩指令 | ✅ | ✅ |
| **V** | **向量** | ✅ | ❌ |

`V`（RVV，1.0 版）让一条指令处理一串数据：音频滤波、图像缩放、矩阵、`memcpy` 都吃这个。

和 x86 的 AVX、ARM 的 NEON 最大的区别：**RVV 的代码不假设寄存器有多宽**。同一段程序在 VLEN=128 和 VLEN=512 上都能跑，每一轮处理几个元素由硬件告诉软件。这是它被 AI 芯片追捧的原因。

## 6.2 RVV 的四个核心概念

这一段是"看得懂任何一份 RVV 代码"的最小知识集。参考教程里的示例、以及任何厂商的 RVV demo，都绕不开这四个东西。

### ① 寄存器与 CSR

| 名称 | 作用 |
|---|---|
| `v0`–`v31` | 32 个向量寄存器，每个 `VLEN` 位（C907 是 128 位 = 16 字节） |
| `vlenb` | 只读 CSR，等于 `VLEN / 8`（C907 上是 16） |
| `vl` | 这一轮实际处理几个元素 |
| `vtype` | 当前配置：元素宽度、寄存器分组、尾/掩码策略 |
| `vstart`、`vcsr` | 被打断后从第几个元素续、定点饱和状态 |

### ② `vsetvli`：用之前必须先配置

```asm
vsetvli vl, 剩余元素数, e32, m8, ta, ma
```

| 参数 | 含义 |
|---|---|
| `e8/e16/e32` | 元素宽度 SEW，8/16/32 位 |
| `m1/m2/m4/m8` | 寄存器分组 LMUL：把几个寄存器合起来当一个用 |
| `ta/ma` | 尾元素、被掩码元素的值不关心 |

一轮最多处理 `VLEN / SEW × LMUL` 个。C907 上 `VLEN=128`，所以 `e32,m8` 一轮最多 32 个，`e8,m1` 最多 16 个。

**没用过向量的上下文里 `vtype.vill` 是置位的，直接执行向量指令就异常**——所以"先 `vsetvli`"是硬规则，不是习惯。

### ③ 分段循环（strip-mining）

数据比一轮能处理的多时，不要自己算块大小，让硬件回报 `vl`，处理完把指针和剩余数往后推：

```c
while (n > 0) {
    size_t vl = __riscv_vsetvl_e32m8(n);   /* 硬件说这轮能做几个 */
    vuint32m8_t va = __riscv_vle32_v_u32m8(a, vl);
    vuint32m8_t vb = __riscv_vle32_v_u32m8(b, vl);
    __riscv_vse32_v_u32m8(c, __riscv_vadd_vv_u32m8(va, vb, vl), vl);
    a += vl; b += vl; c += vl; n -= vl;
}
```

最后一轮元素不够，`vl` 会自动变小，**不需要单独写收尾代码**。这是 RVV 和固定宽度 SIMD 写法上最直观的差别——AVX 要你手写"处理余数"的那一段。

### ④ 上下文与栈

- 向量寄存器是**调用者保存**（caller-saved）。函数调用、`k_sleep()`、`k_yield()` 之后不保证还在；**被动抢占（中断、时间片）不丢**，前提是移植层在异常帧里保存了它们。
- 每个中断帧要放下 `32 × VLEN / 8 + 32` 字节。C907 的 VLEN=128 → **544 字节**。参考教程因此把 `IDLE_STACK_SIZE` 抬到 1536、`MAIN_STACK_SIZE`/`ISR_STACK_SIZE` 抬到 4096。

## 6.3 参考教程是怎么打开的，为什么我们打不开

它们的开关长这样：

```
CONFIG_SUN252I_F101_RVV=y       # SoC 私有开关
  → 自动 select RISCV_ISA_EXT_V、RISCV_ISA_EXT_ZICSR、RISCV_SOC_CONTEXT_SAVE
  → -march 变成 rv32imafdcv_zicsr_zifencei_xtheadcmo
```

关键在那行 `select RISCV_ISA_EXT_V`。我核对了三个地方：

| 核对对象 | 结果 |
|---|---|
| 本地这棵树（上游 4.5.99 + PR#119851）全树 grep `RISCV_ISA_EXT_V` | **0 处** |
| 上游 v4.2.2 的 `arch/riscv/Kconfig.isa`（直接读 GitHub 原始文件） | 只有 M/A/F/D/G/Q/C、Zicsr、Zifencei、Zaamo/Zalrsc、Zba/Zbb/Zbc/Zbs/Zmmul，**没有 V** |
| 参考教程自己的 fork（`YuzukiHD/zephyr`） | **有**，由 `CONFIG_SUN252I_F101_RVV` select |

**结论**：`RISCV_ISA_EXT_V` 不是上游 Zephyr 的通用开关，是那个 fork 为 C907 加的移植代码。看到教程里写"加一行 CONFIG 就行"，先确认那个 CONFIG 是不是 SoC 私有的、这一行在不在你手上的树里。

### 我们这边的实测三条

```
# 1. 全树搜开关（0 处 = 这条线没有 RVV 支持）
python -c "import os;print(sum('RISCV_ISA_EXT_V' in open(os.path.join(dp,f),encoding='utf-8',errors='ignore').read() for dp,dn,fn in os.walk(r'...\west\zephyr') for f in fn if f.startswith('Kconfig')))"

# 2. 实际编译参数
#    build\esp32s31\korvo_shell\s31-debug\compile_commands.json 第一条
-march=rv32imafc_zicsr_zifencei
-mabi=ilp32f

# 3. 反汇编里找向量指令（结果为空）
riscv32-esp-elf-objdump.exe -d build\esp32s31\korvo_shell\s31-debug\zephyr\zephyr.elf | Select-String "vsetvli|vle32|vse32"
```

三条全是"没有"。而 S31 的 HAL 能力表里明确写着另一件事：

```c
#define SOC_CPU_HAS_FPU   1     /* 硬件浮点，有 */
#define SOC_CPU_HAS_PIE   1     /* 乐鑫私有 SIMD/DSP 协处理器，不是 RVV */
```

**PIE ≠ RVV**。PIE 是乐鑫自己加的定长 SIMD（128 位寄存器，专做音频/图像），只有 ESP-IDF 的 `esp-dsp` 用得上，和标准 V 扩展没有任何关系，也拿不到 `__riscv_v*` 内建函数。

## 6.4 三层卡点，逐个说清

想在 S31 上跑 RVV，要同时过三关，缺一不可：

| 层 | 要求 | S31 现状 |
|---|---|---|
| 硬件 | 实现 RVV 1.0，有 `vlenb` | ❌ 只有 PIE |
| 编译器 | `-march` 带 `v`，且 multilib 里有对应库 | ❌ 当前是 `rv32imafc` |
| RTOS 移植 | ISA 开关 + `mstatus.VS` 启动时打开 + 异常帧保存 32 个向量寄存器 + 栈默认值放大 | ❌ 上游没有，PR#119851 也没加 |

第三层最容易被忽略：**光有硬件和 `-march` 还不够**。不保存上下文，中断一来向量寄存器就被踩；不打开 `mstatus.VS`，编译器在 `memcpy()` 里顺手生成的向量指令立刻触发非法指令异常。

## 6.5 这一章该怎么用

- **如果你只是学 Zephyr 概念**：6.2 的四条（`vsetvli` / 分段循环 / caller-saved / 544 字节栈帧）已经够你读懂任何 RVV 代码，也够你回答"为什么向量代码不能随便写函数调用"。
- **如果你要真跑**：换一块有 V 的板子，或者照参考教程那样做移植（它的 fork 就是现成的移植样板：SoC Kconfig 加开关 → `-march` 加 `v` → 启动时开 `mstatus.VS` → 异常帧存 `v0`–`v31` → 抬栈）。
- **在 Korvo-1 上**：别试了，硬件就没有。把精力放到 PIE 之外的部分（DT / Kconfig / 驱动模型 / 多线程），那些才是你学 Zephyr 的目标。

## 6.6 参考教程对应的页面

- 参考教程 RVV 章：<https://yuzukihd.github.io/YuzukiNeko/zh/dev/zephyr/zephyr-riscv-vector/>
- 它的移植源：<https://github.com/YuzukiHD/zephyr>

读的时候注意它通篇是"F101 的移植"，`CONFIG_SUN252I_F101_*` 这类前缀就是提示：那是 SoC 私有代码，不是 Zephyr 通用能力。

## 自检

1. `vsetvli vl, n, e32, m8, ta, ma` 在 VLEN=128 时一轮最多处理几个元素？（128/32×8 = 32）
2. 为什么 RVV 的分段循环不需要写"余数收尾"？（每轮 `vl` 由硬件给，最后一轮自动变小）
3. 线程里放了值到 `v8`，中间调了一次 `printf` 还想它保持，为什么不行？（向量寄存器 caller-saved）
4. 打开 RVV 后中断帧多占多少字节？依据是什么？（`32×VLEN/8+32`，VLEN=128 → 544）
5. 看到教程说"加 `CONFIG_XXX_RVV=y` 就行"，你第一步该查什么？（这个 CONFIG 在你手上的树里存不存在、是不是 SoC 私有）
