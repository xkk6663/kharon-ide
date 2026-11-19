/*
 * selftest.h —— 上电 HTTP 自检（L1）
 */
#ifndef SELFTEST_H_
#define SELFTEST_H_

#include <stddef.h>

void http_self_test(const char *path);
/* 指定目标 IP 的自检：AP 侧（192.168.4.1）和局域网侧（自己的 STA 地址）是两套
 * 接口，同一个路径在两边的返回码不一样（开发者接口在 AP 侧按设计回 404）。 */
void http_self_test_host(const char *host, const char *path);
void http_self_test_scope(const char *host, const char *expect);

#endif /* SELFTEST_H_ */
