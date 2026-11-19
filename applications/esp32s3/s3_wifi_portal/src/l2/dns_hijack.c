/*
 * dns_hijack —— Captive Portal 的 DNS 应答器（udp/53 全部回 AP 自己）
 *
 * L2 服务层。只依赖 socket 与配置宏，不认识 HTTP。
 */
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/net_ip.h>

#include "portal_cfg.h"
#include "dns_hijack.h"

LOG_MODULE_REGISTER(portal_dns, LOG_LEVEL_INF);

#define DNS_PORT 53

static void dns_hijack_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	int sock = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

	if (sock < 0) {
		LOG_ERR("dns: socket failed (%d)", errno);
		return;
	}

	struct sockaddr_in bind_addr = {
		.sin_family = AF_INET,
		.sin_port = htons(DNS_PORT),
	};

	if (zsock_bind(sock, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) < 0) {
		LOG_ERR("dns: bind 53 failed (%d)", errno);
		zsock_close(sock);
		return;
	}

	LOG_INF("dns hijack ready on udp/53");

	struct in_addr self;

	net_addr_pton(AF_INET, AP_IP, &self);

	uint8_t req[400];
	uint8_t rsp[512];

	while (true) {
		struct sockaddr_in from;
		socklen_t from_len = sizeof(from);
		ssize_t n = zsock_recvfrom(sock, req, sizeof(req), 0,
					   (struct sockaddr *)&from, &from_len);

		if (n < 0) {
			/* 不能空转：网络没就绪时 recvfrom 会立刻返回错误 */
			k_msleep(50);
			continue;
		}

		if (n < 12) {
			continue;
		}

		/* 只处理标准查询（QR=0），非 A 查询回空答案让客户端再问 A */
		if (req[2] & 0x80) {
			continue;
		}

		int p = 12;

		while (p < n && req[p] != 0) {
			if ((req[p] & 0xC0) == 0xC0) {   /* 压缩指针 */
				p += 2;
				break;
			}
			p += req[p] + 1;
		}

		if (p >= n) {
			continue;
		}

		if (req[p] == 0) {
			p++;
		}

		int q_end = p + 4;                      /* qtype + qclass */

		if (q_end > n) {
			continue;
		}

		uint16_t qtype = ((uint16_t)req[p] << 8) | req[p + 1];

		memcpy(rsp, req, 12);

		/* QR=1, AA=1, RA=1，RD 跟请求一致；RCODE=0 */
		rsp[2] = 0x84 | (req[2] & 0x01);
		rsp[3] = 0x80;

		int is_a = (qtype == 1);

		rsp[6] = 0;
		rsp[7] = is_a ? 1 : 0;                 /* ANCOUNT */
		rsp[8] = 0;
		rsp[9] = 0;                            /* NSCOUNT */
		rsp[10] = 0;
		rsp[11] = 0;                           /* ARCOUNT */

		memcpy(rsp + 12, req + 12, q_end - 12);

		size_t len = q_end;

		if (is_a) {
			uint8_t *a = rsp + q_end;

			a[0] = 0xC0;
			a[1] = 0x0C;                    /* 指回问题里的域名 */
			a[2] = 0x00;
			a[3] = 0x01;                    /* TYPE A */
			a[4] = 0x00;
			a[5] = 0x01;                    /* CLASS IN */
			a[6] = 0x00;
			a[7] = 0x00;
			a[8] = 0x00;
			a[9] = 0x1E;                    /* TTL 30 s */
			a[10] = 0x00;
			a[11] = 0x04;
			memcpy(a + 12, &self.s_addr, 4);
			len += 16;
		}

		zsock_sendto(sock, rsp, len, 0, (struct sockaddr *)&from, from_len);
	}
}

/* 线程延迟启动：AP 起来以后再开，避免在 Wi-Fi/网络初始化阶段抢资源 */
static K_THREAD_STACK_DEFINE(dns_stack, 3072);
static struct k_thread dns_thread_data;
static bool dns_started;

void start_dns_hijack(void)
{
	if (dns_started) {
		return;
	}

	k_thread_create(&dns_thread_data, dns_stack, K_THREAD_STACK_SIZEOF(dns_stack),
			dns_hijack_thread, NULL, NULL, NULL,
			K_PRIO_PREEMPT(8), 0, K_NO_WAIT);
	k_thread_name_set(&dns_thread_data, "dns_hijack");
	dns_started = true;
}

/* DNS 劫持自检：像手机那样查一次 captive.apple.com，看是不是回自己 */
void dns_self_test(void)
{
	int sock = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

	if (sock < 0) {
		LOG_ERR("dns self-test: socket failed (%d)", errno);
		return;
	}

	uint8_t q[64];
	size_t l = 12;

	memset(q, 0, sizeof(q));
	q[0] = 0x12;                 /* 随便一个 ID */
	q[1] = 0x34;
	q[2] = 0x01;                 /* RD */
	q[5] = 1;                    /* QDCOUNT=1 */

	/* 手写域名：captive.apple.com */
	q[l++] = 7;
	memcpy(q + l, "captive", 7);
	l += 7;
	q[l++] = 5;
	memcpy(q + l, "apple", 5);
	l += 5;
	q[l++] = 3;
	memcpy(q + l, "com", 3);
	l += 3;
	q[l++] = 0;
	q[l++] = 0x00;
	q[l++] = 0x01;               /* QTYPE=A */
	q[l++] = 0x00;
	q[l++] = 0x01;               /* QCLASS=IN */

	struct sockaddr_in dst = {
		.sin_family = AF_INET,
		.sin_port = htons(DNS_PORT),
	};

	net_addr_pton(AF_INET, AP_IP, &dst.sin_addr);
	zsock_sendto(sock, q, l, 0, (struct sockaddr *)&dst, sizeof(dst));

	uint8_t r[512];
	struct sockaddr_in from;
	socklen_t from_len = sizeof(from);
	ssize_t n = zsock_recvfrom(sock, r, sizeof(r), 0, (struct sockaddr *)&from, &from_len);

	if (n >= 16 && r[7] > 0) {
		struct in_addr got;

		memcpy(&got.s_addr, r + n - 4, 4);

		char ip[NET_IPV4_ADDR_LEN];
		const char *sl = net_addr_ntop(AF_INET, &got, ip, sizeof(ip));

		LOG_INF("dns self-test: captive.apple.com -> %s", sl ? ip : "?");
	} else {
		LOG_ERR("dns self-test: no answer (%d)", (int)n);
	}

	zsock_close(sock);
}
