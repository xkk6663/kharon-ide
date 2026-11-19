/*
 * time_sync.h —— SNTP 对时（L2）
 */
#ifndef TIME_SYNC_H_
#define TIME_SYNC_H_

#include <stdbool.h>
#include <stdint.h>

void time_start(void);
void clock_hms(char *buf, size_t n);

bool time_is_ok(void);
int  time_sntp_rc(void);
const char *time_sntp_srv(void);

/* netcheck 收到响应头后把 Date 交给这里兜底对时（SNTP 不通时用） */
struct http_response;
void time_apply_http_date(const struct http_response *rsp);

#endif /* TIME_SYNC_H_ */
