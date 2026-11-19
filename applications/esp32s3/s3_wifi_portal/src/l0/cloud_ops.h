/*
 * cloud_ops —— 公网通道的命令执行器（L0，不认识 l2）
 *
 * 云端只下发"白名单"里的命令；执行需要 l2 的东西（RSSI / 网络自检 / 灯）
 * 由 main.c 在上电时用 cloud_ops_bind() 把真实函数挂进来 —— 这样 l0 不必
 * include l2 的头，分层防火墙继续成立。
 *
 * 设计约束：
 *   · 白名单之外一律拒绝（返回 -ENOENT），绝不开放"任意 shell 命令"；
 *   · 任何写密钥的命令（psk / token）永不在此实现 —— 密钥只能本地/产线注入；
 *   · reboot 只置标志，由 ota_cloud 在把回执发出去之后再真正复位，
 *     否则云端永远收不到"我执行了"。
 */
#ifndef CLOUD_OPS_H_
#define CLOUD_OPS_H_

#include <stddef.h>
#include <stdint.h>

/* l2 能力的挂钩：main.c 注册，NULL 表示该能力不可用（命令会明确回错） */
struct cloud_ops_hooks {
	const char *(*ip)(void);
	const char *(*ssid)(void);
	int (*rssi)(void);
	int (*net_http)(void);
	int (*net_dns_rc)(void);
	/* 返回 0 = 已生效；color ∈ red/green/blue/purple/off；ms 为持续时长 */
	int (*led_test)(const char *color, int ms);
};

void cloud_ops_bind(const struct cloud_ops_hooks *h);

struct cloud_ops_ret {
	int rc;           /* 0 成功；-ENOENT 非白名单；-EINVAL 参数错 */
	int want_reboot;  /* 1 = 回执发完后应复位 */
	int fast_rounds;  /* >0 = 接下来几轮用快轮询（命令下发后立刻确认） */
};

/* 执行一条命令。out 里放人话结果（≤ out_n，已截断）。 */
int cloud_ops_run(const char *name, const char *args, char *out, size_t out_n,
		  struct cloud_ops_ret *ret);

/* 影子里"环境类"字段（rssi/ssid/ip/net），自带前导逗号，拼进外层 JSON */
void cloud_ops_env_json(char *buf, size_t n);

#endif /* CLOUD_OPS_H_ */
