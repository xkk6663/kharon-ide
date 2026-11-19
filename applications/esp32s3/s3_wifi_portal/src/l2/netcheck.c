/*
 * netcheck —— 主动出网自检 + 默认路由 + 启动对时/探测线程
 *
 * L2 服务层。拿到 IP 之后"到底能不能出网"是它回答的；DNS 解析也在这里，
 * 因为 sntp 与 HTTP 自检都需要同一个解析入口。
 */
#include <string.h>
#include <stdlib.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/http/client.h>

#include "portal_cfg.h"
#include "netcheck.h"
#include "time_sync.h"

LOG_MODULE_REGISTER(portal_netchk, LOG_LEVEL_INF);

int dns_resolve_host(const char *host, char *ip_out, size_t len)
{
	struct zsock_addrinfo hints = {
		.ai_family = AF_INET,
		.ai_socktype = SOCK_STREAM,
	};
	struct zsock_addrinfo *res = NULL;
	int rc = zsock_getaddrinfo(host, "80", &hints, &res);

	if (rc != 0 || res == NULL) {
		LOG_WRN("dns: \"%s\" failed (%d)", host, rc);
		return -EHOSTUNREACH;
	}

	struct sockaddr_in *sin = (struct sockaddr_in *)res->ai_addr;

	zsock_inet_ntop(AF_INET, &sin->sin_addr, ip_out, len);
	zsock_freeaddrinfo(res);
	return 0;
}

K_THREAD_STACK_DEFINE(netcheck_stack, 4096);
static struct k_thread netcheck_tcb;

/* 主动发一次 GET：能收到任意 HTTP 状态码就算"真的能出网"。
 * 只看"连上 AP 了"是不够的——家里路由器把 WAN 拔了，STA 也照样能拿到
 * 10.168.1.x 和 RSSI，但一个包都出不去。 */
/* 百度的名字在这块板上解析不稳（实测 dns 直接返回 -EHOSTUNREACH，
 * 而同一个解析器解 ntp.aliyun.com 是成功的），所以给一个固定 IP 兜底：
 * 自检的目的是"包到底能不能出网"，不该被一条 DNS 记录卡死。 */
/* 这一组是"自检/对时走到哪一步"的诊断量。
 * 原来它们和 SNTP 的变量一起塞在 main.c 里；现在归属 netcheck，
 * 对时那边只保留 SNTP 自己的 rc/srv。 */
#define NETCHECK_HOST    "www.baidu.com"
static volatile int  g_netcheck_http_status;  /* 0 = 还没有结果 */
static volatile bool g_netcheck_done;
static volatile int  g_netcheck_rc;
static volatile int  g_dns_rc;                 /* 0 = 成功 */
static char          g_dns_ip[NET_IPV4_ADDR_LEN];
static char          g_net_ip[NET_IPV4_ADDR_LEN] = "-";

#define NETCHECK_FALLBACK_IP "183.2.172.177"

static int http_resp_cb(struct http_response *rsp,
			enum http_final_call final_data, void *user_data)
{
	ARG_UNUSED(user_data);

	if (final_data == HTTP_DATA_FINAL) {
		g_netcheck_http_status = rsp->http_status_code;
		g_netcheck_done = true;
		/* HTTP Date 头只有整秒，但它能在 SNTP 不通时先把时间对上 */
		time_apply_http_date(rsp);
	}

	return 0;
}

static void netcheck_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	uint64_t t0 = k_uptime_get();
	char ip[NET_IPV4_ADDR_LEN] = { 0 };
	int rc = dns_resolve_host(NETCHECK_HOST, ip, sizeof(ip));

	/* 解析失败和"出网不通"是两种完全不同的故障，必须分开报，不能都塞成 -22 */
	if (rc) {
		g_netcheck_rc = rc;
		g_dns_rc = rc;
		LOG_WRN("net check: dns failed rc=%d, fallback to %s", rc, NETCHECK_FALLBACK_IP);
		strncpy(ip, NETCHECK_FALLBACK_IP, sizeof(ip) - 1);
	} else {
		g_dns_rc = 0;
		strncpy(g_dns_ip, ip, sizeof(g_dns_ip) - 1);
		LOG_INF("net check: %s -> %s", NETCHECK_HOST, ip);
	}

	strncpy(g_net_ip, ip, sizeof(g_net_ip) - 1);

	int sock = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

	if (sock < 0) {
		g_netcheck_rc = -errno;
		g_netcheck_done = true;
		LOG_WRN("net check: socket failed (%d)", errno);
		return;
	}

	struct sockaddr_in sa = {
		.sin_family = AF_INET,
		.sin_port = htons(80),
	};

	zsock_inet_pton(AF_INET, ip, &sa.sin_addr);

	rc = zsock_connect(sock, (struct sockaddr *)&sa, sizeof(sa));
	if (rc < 0) {
		g_netcheck_rc = -errno;
		g_netcheck_done = true;
		zsock_close(sock);
		LOG_WRN("net check: connect %s:80 failed (%d)", ip, errno);
		return;
	}

	/* recv_buf 必须有：http_client_req() 在 recv_buf==NULL 或 recv_buf_len==0 时
	 * 直接返回 -EINVAL，一个包都不发。原来就是漏了这个才 `1 ms` 就 offline。 */
	static char recv_buf[768];

	struct http_request req = { 0 };

	req.method = HTTP_GET;
	/* url 只填 path，host 单独给：它只拼请求行和 Host 头，不做 DNS 也不 connect */
	req.url = "/";
	req.host = NETCHECK_HOST;
	req.port = "80";
	req.protocol = "HTTP/1.1";
	req.response = http_resp_cb;
	req.recv_buf = recv_buf;
	req.recv_buf_len = sizeof(recv_buf);

	rc = http_client_req(sock, &req, 8000, NULL);

	g_netcheck_rc = rc;
	g_netcheck_done = true;
	zsock_close(sock);

	LOG_INF("net check: %s (rc=%d http=%d, %llu ms)",
		g_netcheck_http_status > 0 ? "online" : "offline", rc, g_netcheck_http_status,
		k_uptime_get() - t0);
}

void start_net_services(void)
{
	static bool started;

	if (started) {
		return;
	}
	started = true;

	time_start();

	k_thread_create(&netcheck_tcb, netcheck_stack,
			K_THREAD_STACK_SIZEOF(netcheck_stack),
			netcheck_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(9), 0,
			K_NO_WAIT);
	k_thread_name_set(&netcheck_tcb, "netcheck");
}

/* 板子拿到 IP 了，但"能不能出网"还差一步：默认路由器条目。
 *
 * 实测症状：DNS 能通（DNS 服务器就在同网段，走二层直达），但发往网段外的包
 * 立刻失败 —— netcheck 返回 -EHOSTUNREACH(-118)，SNTP 返回 -ETIMEDOUT(-116)。
 * 原因是没有 IPv4 路由器/默认路由：DHCP 没给 option 3 时 Zephyr 不会自动建条目，
 * 出网找不到下一跳。
 *
 * 这里显式补一条：优先用接口上的网关；没有就按家用路由惯例退到"同网段 .1"。
 * 补完之后 netcheck 才可能真的出网，clock 也才有机会对上。 */
void ensure_default_route(struct net_if *iface, const char *our_ip, char *gw_out, size_t gw_len)
{
	struct net_in_addr gw = net_if_ipv4_get_gw(iface);

	if (gw.s_addr == 0) {
		struct net_in_addr addr = { 0 };

		if (net_addr_pton(AF_INET, our_ip, &addr) != 0) {
			return;
		}
		/* 10.168.1.226 -> 10.168.1.1 */
		addr.s4_addr[3] = 1;
		gw = addr;
		LOG_WRN("no gateway from DHCP, fallback to subnet .1");
	}

	if (net_if_ipv4_router_add(iface, &gw, true, 0) == NULL) {
		LOG_WRN("router add failed");
	}

	net_addr_ntop(AF_INET, &gw, gw_out, gw_len);
	LOG_INF("default router: %s", gw_out);
}

/* 页面分离的身份自检（定义在文件后部，这里先声明：拿到 IP 时就要验一次） */

/* ===================== 对外接口 ===================== */
void net_dns_note(int rc, const char *ip)
{
	g_dns_rc = rc;

	if (ip != NULL) {
		strncpy(g_dns_ip, ip, sizeof(g_dns_ip) - 1);
	}
}

void netcheck_view_get(struct netcheck_view *v)
{
	v->http_status = g_netcheck_http_status;
	v->rc = g_netcheck_rc;
	v->done = g_netcheck_done;
	v->dns_rc = g_dns_rc;
	v->dns_ip = g_dns_ip;
	v->net_ip = g_net_ip;
}

int net_http_status(void) { return g_netcheck_http_status; }
int net_rc(void) { return g_netcheck_rc; }
bool net_done(void) { return g_netcheck_done; }
int net_dns_rc(void) { return g_dns_rc; }
const char *net_dns_ip(void) { return g_dns_ip; }
const char *net_ip(void) { return g_net_ip; }
