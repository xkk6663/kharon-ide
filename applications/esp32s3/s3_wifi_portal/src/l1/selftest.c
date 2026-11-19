/*
 * selftest —— 上电自检：从板子自己访问自己的 HTTP 接口
 *
 * L1 接口层（断言的是"接口"而不是"服务"）。它是"手机连上热点到底看到什么"
 * 的机上证据：scope 必须是 user 而不是 dev。
 */
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/socket.h>

#include "portal_cfg.h"
#include "selftest.h"

LOG_MODULE_REGISTER(portal_selftest, LOG_LEVEL_INF);

void http_self_test_host(const char *host, const char *path)
{
	int sock = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

	if (sock < 0) {
		LOG_ERR("self-test: socket failed (%d)", errno);
		return;
	}

	struct sockaddr_in dst = {
		.sin_family = AF_INET,
		.sin_port = htons(80),
	};

	if (host == NULL || host[0] == '\0' ||
	    net_addr_pton(AF_INET, host, &dst.sin_addr) < 0) {
		LOG_ERR("self-test: bad host");
		zsock_close(sock);
		return;
	}

	if (zsock_connect(sock, (struct sockaddr *)&dst, sizeof(dst)) < 0) {
		LOG_ERR("self-test: connect %s:80 failed (%d)", host, errno);
		zsock_close(sock);
		return;
	}

	char req[160];

	snprintf(req, sizeof(req),
		 "GET %s HTTP/1.1\r\nHost: " CONFIG_NET_HOSTNAME ".local\r\n"
		 "Connection: close\r\n\r\n", path);

	zsock_send(sock, req, strlen(req), 0);

	char buf[96];
	ssize_t n = zsock_recv(sock, buf, sizeof(buf) - 1, 0);

	if (n > 0) {
		buf[n] = '\0';
		for (int i = 0; buf[i]; i++) {
			if (buf[i] == '\r' || buf[i] == '\n') {
				buf[i] = '\0';
				break;
			}
		}
		LOG_INF("self-test: %s GET %s -> \"%s\"", host, path, buf);
	} else {
		LOG_ERR("self-test: %s GET %s no response (%d)", host, path, (int)n);
	}

	zsock_close(sock);
}

void http_self_test(const char *path)
{
	http_self_test_host(AP_IP, path);
}

/* 页面分离的自动验收：连到 host:80，只读响应头里的 X-Portal-Scope。
 *
 * 为什么值得写这段：AP 侧只有手机连上 S3-Portal 才看得到，而用户明确不允许为了
 * 验证去切 PC 的 WiFi（切了 PC 就没网）。板子自己能连自己的两个地址：
 *   连 192.168.4.1  → 走 AP 网段 → 期望 scope=user（用户页）
 *   连自己的 STA 地址 → 走局域网网段 → 期望 scope=dev（开发者页）
 * 两条日志就证明"两个受众拿到的是两个页面"，不用动 PC 的网络。 */
void http_self_test_scope(const char *host, const char *expect)
{
	if (host == NULL || host[0] == '\0') {
		return;
	}

	int sock = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

	if (sock < 0) {
		LOG_ERR("scope-test: socket failed (%d)", errno);
		return;
	}

	struct sockaddr_in dst = {
		.sin_family = AF_INET,
		.sin_port = htons(80),
	};

	if (net_addr_pton(AF_INET, host, &dst.sin_addr) < 0) {
		LOG_ERR("scope-test: bad host %s", host);
		zsock_close(sock);
		return;
	}

	if (zsock_connect(sock, (struct sockaddr *)&dst, sizeof(dst)) < 0) {
		LOG_WRN("scope-test: connect %s:80 failed (%d)", host, errno);
		zsock_close(sock);
		return;
	}

	char req[96];

	snprintf(req, sizeof(req),
		 "GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", host);
	zsock_send(sock, req, strlen(req), 0);

	char buf[320];
	ssize_t n = zsock_recv(sock, buf, sizeof(buf) - 1, 0);

	if (n <= 0) {
		LOG_ERR("scope-test: %s no response (%d)", host, (int)n);
		zsock_close(sock);
		return;
	}
	buf[n] = '\0';

	const char *p = strstr(buf, "X-Portal-Scope: ");

	if (p == NULL) {
		LOG_ERR("scope-test: %s no scope header", host);
		zsock_close(sock);
		return;
	}

	p += strlen("X-Portal-Scope: ");

	size_t len = strcspn(p, "\r\n");
	bool ok = (len == strlen(expect)) && (strncmp(p, expect, len) == 0);

	if (ok) {
		LOG_INF("scope-test: %s -> scope=%.*s (expect %s) OK", host, (int)len, p, expect);
	} else {
		LOG_ERR("scope-test: %s -> scope=%.*s (expect %s) MISMATCH", host, (int)len, p,
			expect);
	}

	zsock_close(sock);
}

/* ------------------------------------------------- Captive Portal：DNS 劫持
 * 手机连上 AP 后会去查 captive.apple.com / connectivitycheck.gstatic.com。
 * 这里在 AP 网段上开一个最小 DNS 应答器（UDP 53），把**所有** A 查询都回答成
 * AP 自己的 IP，于是手机一定会把探测请求发到我们身上，再由通配资源 302
 * 回配网页 —— 系统就会弹「登录网络」。
 */
