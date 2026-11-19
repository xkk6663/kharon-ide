/*
 * store.h —— 芯片无关的持久化封装
 *
 * 这一层只依赖三样东西：
 *   1. Zephyr 的 NVS 子系统（<zephyr/kvss/nvs.h>）
 *   2. 分区表里一个叫 storage_partition 的分区（任何板子只要有分区表就有）
 *   3. flash 驱动（由 DT 自动选中）
 *
 * 它不认识 GPIO、不认识 ADC、不认识任何 ESP 专属寄存器。
 * 换到 ESP32-S3 / STM32 / nRF 上，这个文件可以原样拷过去。
 */

#ifndef STORE_H_
#define STORE_H_

#include <stddef.h>
#include <stdint.h>
#include <zephyr/kernel.h>

/* 键空间：0 和 0xFFFF 是 NVS 的保留值，业务 id 从 1 开始 */
#define STORE_ID_BOOT_CNT  1u   /* uint32：开机次数 */
#define STORE_ID_TAG       2u   /* string：设备标签 */
#define STORE_ID_RGB       3u   /* blob 3B：RGB 三原色 */
#define STORE_ID_BRIGHT    4u   /* uint32：亮度百分比 */

struct store_info {
	uint32_t offset;       /* storage 分区起始偏移 */
	uint32_t sector_size;  /* 一个 NVS 扇区 = 一个 flash page */
	uint32_t sector_count; /* 用了几个扇区 */
	uint32_t capacity;     /* 粗略可用容量（sector_size * (sector_count-1)） */
};

/**
 * @brief 挂载 NVS（只需调用一次，建议在 main 里）
 * @return 0 成功；负值失败（-ENODEV / -EINVAL / nvs_mount 的错误码）
 */
int store_init(void);

/** @brief 取分区几何信息，用于 shell 展示 */
int store_get_info(struct store_info *out);

/** @brief 读一个 uint32。不存在返回 -ENOENT */
int store_get_u32(uint16_t id, uint32_t *out);

/** @brief 写一个 uint32（已存在则覆盖）。返回 nvs_write 的返回值 */
int store_set_u32(uint16_t id, uint32_t val);

/** @brief 读一段 blob。len 返回实际读到的字节数 */
int store_get_blob(uint16_t id, void *dst, size_t max, size_t *len);

/** @brief 写一段 blob */
int store_set_blob(uint16_t id, const void *src, size_t len);

/** @brief 读字符串（以 '\0' 结尾写入 dst） */
int store_get_str(uint16_t id, char *dst, size_t max, size_t *len);

/** @brief 写字符串（自动带上结尾 '\0'） */
int store_set_str(uint16_t id, const char *src);

/** @brief 删除一个键 */
int store_del(uint16_t id);

/**
 * @brief 开机计数 +1 并落盘
 * @return 本次开机是第几次（从 1 开始）；读写失败时返回 0
 */
uint32_t store_boot_count_bump(void);

#endif /* STORE_H_ */
