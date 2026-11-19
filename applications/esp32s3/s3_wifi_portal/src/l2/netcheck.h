/*
 * netcheck.h —— 出网自检 + 默认路由 + DNS 解析（L2）
 */
#ifndef NETCHECK_H_
#define NETCHECK_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/net/net_if.h>

struct netcheck_view {
	int  http_status;
	int  rc;
	bool done;
	int  dns_rc;
	const char *dns_ip;
	const char *net_ip;
};

void netcheck_view_get(struct netcheck_view *v);
void net_dns_note(int rc, const char *ip);
int  net_http_status(void);
int  net_rc(void);
bool net_done(void);
int  net_dns_rc(void);
const char *net_dns_ip(void);
const char *net_ip(void);

void start_net_services(void);
void ensure_default_route(struct net_if *iface, const char *our_ip,
			  char *gw_out, size_t gw_len);
int  dns_resolve_host(const char *host, char *ip_out, size_t len);

#endif /* NETCHECK_H_ */
