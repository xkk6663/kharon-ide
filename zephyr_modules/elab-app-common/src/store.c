#include <string.h>
#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/kvss/nvs.h>
#include <zephyr/logging/log.h>

#include "store.h"

LOG_MODULE_REGISTER(store, LOG_LEVEL_INF);

/* 分区名来自 dts：partitions_0x2000_default_16M.dtsi 里的 storage_partition
 * 偏移 0xfb0000、大小 192 KB。换板子时只要新区划表里也有这个名字，本文件不用改。 */
#define STORE_PARTITION        storage_partition
#define STORE_PARTITION_DEVICE PARTITION_DEVICE(STORE_PARTITION)
#define STORE_PARTITION_OFFSET PARTITION_OFFSET(STORE_PARTITION)

static struct nvs_fs fs;
static bool mounted;

int store_init(void)
{
	struct flash_pages_info info;
	int rc;

	if (mounted) {
		return 0;
	}

	fs.flash_device = STORE_PARTITION_DEVICE;
	if (!device_is_ready(fs.flash_device)) {
		LOG_ERR("flash 设备未就绪");
		return -ENODEV;
	}

	fs.offset = STORE_PARTITION_OFFSET;
	rc = flash_get_page_info_by_offs(fs.flash_device, fs.offset, &info);
	if (rc) {
		LOG_ERR("取 page info 失败 rc=%d", rc);
		return rc;
	}

	/* NVS 的扇区必须等于 flash page。S31 的 page 是 4 KB，
	 * storage 分区 192 KB → 最多 48 个扇区，这里用 3 个（12 KB），够存几百条。 */
	fs.sector_size = info.size;
	fs.sector_count = 3U;

	rc = nvs_mount(&fs);
	if (rc) {
		LOG_ERR("nvs_mount 失败 rc=%d", rc);
		return rc;
	}

	mounted = true;
	LOG_INF("NVS 已挂载：offset=0x%08x sector=%u B × %u", (unsigned int)fs.offset,
		fs.sector_size, fs.sector_count);
	return 0;
}

int store_get_info(struct store_info *out)
{
	if (!mounted || !out) {
		return -EINVAL;
	}

	out->offset = (uint32_t)fs.offset;
	out->sector_size = fs.sector_size;
	out->sector_count = fs.sector_count;
	/* NVS 要留一个扇区做搬移，所以可用容量按 (n-1) 个扇区估算 */
	out->capacity = fs.sector_size * (fs.sector_count - 1U);
	return 0;
}

int store_get_u32(uint16_t id, uint32_t *out)
{
	int rc;

	if (!mounted || !out) {
		return -EINVAL;
	}

	rc = nvs_read(&fs, id, out, sizeof(*out));
	if (rc == 0) {
		return -ENOENT;      /* 没有这个键 */
	}
	if (rc < 0) {
		return rc;           /* 真出错 */
	}
	if ((size_t)rc != sizeof(*out)) {
		return -EINVAL;      /* 键存在但类型不对（长度不符） */
	}
	return 0;
}

int store_set_u32(uint16_t id, uint32_t val)
{
	if (!mounted) {
		return -EINVAL;
	}
	return nvs_write(&fs, id, &val, sizeof(val));
}

int store_get_blob(uint16_t id, void *dst, size_t max, size_t *len)
{
	int rc;

	if (!mounted || !dst) {
		return -EINVAL;
	}

	rc = nvs_read(&fs, id, dst, max);
	if (rc == 0) {
		return -ENOENT;
	}
	if (rc < 0) {
		return rc;
	}
	if ((size_t)rc > max) {
		return -ENOMEM;      /* 存进去的比给的缓冲大 */
	}
	if (len) {
		*len = (size_t)rc;
	}
	return 0;
}

int store_set_blob(uint16_t id, const void *src, size_t len)
{
	if (!mounted || !src || len == 0) {
		return -EINVAL;
	}
	return nvs_write(&fs, id, src, len);
}

int store_get_str(uint16_t id, char *dst, size_t max, size_t *len)
{
	int rc;

	if (!mounted || !dst || max < 2) {
		return -EINVAL;
	}

	rc = nvs_read(&fs, id, dst, max - 1);
	if (rc == 0) {
		return -ENOENT;
	}
	if (rc < 0) {
		return rc;
	}
	dst[rc] = '\0';          /* 保证可打印 */
	if (len) {
		*len = (size_t)rc;
	}
	return 0;
}

int store_set_str(uint16_t id, const char *src)
{
	size_t n;

	if (!mounted || !src) {
		return -EINVAL;
	}

	n = strlen(src);
	/* 连 '\0' 一起存，读回来就不用补了 */
	return nvs_write(&fs, id, src, n + 1U);
}

int store_del(uint16_t id)
{
	if (!mounted) {
		return -EINVAL;
	}
	return nvs_delete(&fs, id);
}

uint32_t store_boot_count_bump(void)
{
	uint32_t cnt = 0;

	if (store_get_u32(STORE_ID_BOOT_CNT, &cnt) != 0) {
		cnt = 0;             /* 第一次上电没有这个键 */
	}

	cnt++;
	if (store_set_u32(STORE_ID_BOOT_CNT, cnt) < 0) {
		LOG_ERR("开机计数写入失败");
		return 0;
	}

	return cnt;
}
