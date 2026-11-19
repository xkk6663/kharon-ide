/*
 * ota_engine 的主机侧回归测试（gcc + 不用任何 Zephyr 头）
 *
 * 为什么要在 PC 上跑：引擎被抽出来的全部意义就是"平台无关"，而这一点
 * 只有在不用 Zephyr 头、不用 xtensa 工具链编译时才被真正证明。
 * 同时它把"收包 / 续传 / 摘要比对 / 短包"这些分支从"要烧板子才知道"
 * 变成一条可以反复跑的断言。
 *
 * 编译（在仓库根）：
 *   gcc -std=gnu11 -Wall -Wextra -I applications/common/include `
 *       docs/tools/test_ota_engine.c applications/common/src/ota_engine.c -o t_engine.exe
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ota_engine.h"

/* ---------------------------------------------------------------- 假后端 */
#define SLOT_MAX 8192
static uint8_t g_store[SLOT_MAX];
static size_t g_stored;
static int g_begin_off;          /* 0=全量，>0=断点，<0=begin 失败 */
static int g_write_fail_at = -1; /* 第 N 次 write 之后失败（-1 = 永不失败）*/
static int g_write_calls;
static int g_finish_rc;          /* 模拟摘要比对结果 */
static char g_err[64];
static const char *g_tok;

static int32_t be_begin(void *ctx, const struct ota_req *req)
{
	struct ota_backend *b = ctx;

	(void)req;
	g_stored = (size_t)(g_begin_off < 0 ? 0 : g_begin_off);
	if (g_begin_off < 0) {
		snprintf(b->err_msg, sizeof(b->err_msg), "flash init failed");
		b->err_token = "flash init";
		return g_begin_off;
	}
	return g_begin_off;
}

static int be_write(void *ctx, const uint8_t *data, size_t len)
{
	/* 注意：失败原因必须写回 backend 结构自己的 err_msg / err_token ——
	 * 引擎读的是 be->err_msg，不是后端另存的某个全局。这版 ota.c 就是
	 * 让 snprintf 直接落在 g_be 上，测试这里也照同一约定来。 */
	struct ota_backend *b = ctx;

	g_write_calls++;
	if (g_write_fail_at >= 0 && g_write_calls > g_write_fail_at) {
		snprintf(b->err_msg, sizeof(b->err_msg), "flash write failed");
		b->err_token = "flash write";
		return -5;
	}
	if (g_stored + len > SLOT_MAX) {
		snprintf(b->err_msg, sizeof(b->err_msg), "overflow in fake flash");
		b->err_token = "flash write";
		return -5;
	}
	memcpy(g_store + g_stored, data, len);
	g_stored += len;
	return 0;
}

static int be_finish(void *ctx, const struct ota_req *req)
{
	struct ota_backend *b = ctx;

	(void)req;
	if (g_finish_rc) {
		snprintf(b->err_msg, sizeof(b->err_msg),
			 "sha256 MISMATCH vs expected X-Sha256");
		b->err_token = "sha mismatch";
	}
	return g_finish_rc;
}

static void be_abort(void *ctx)
{
	(void)ctx;
}

/* 假的 socket：从内存缓冲里往外吐数据 */
struct feed {
	const uint8_t *buf;
	size_t len;
	size_t pos;
};

static int feed_recv(void *ctx, uint8_t *buf, size_t want, size_t *got)
{
	struct feed *f = ctx;
	size_t n = f->len - f->pos;

	if (n > want) {
		n = want;
	}
	memcpy(buf, f->buf + f->pos, n);
	f->pos += n;
	*got = n;
	return 0;
}

/* ---------------------------------------------------------------- 断言 */
static int g_fails;

static void chk(int cond, const char *what)
{
	if (!cond) {
		printf("  FAIL: %s\n", what);
		g_fails++;
	} else {
		printf("  ok  : %s\n", what);
	}
}

/* 不实现真 SHA-256：只需要"两边算法一致"，这里造 16 个十六进制块。 */
static void fill_sha(const uint8_t *data, size_t len, char out[65])
{
	unsigned s = 0;

	for (size_t i = 0; i < len; i++) {
		s = s * 131u + data[i];
	}
	for (int i = 0; i < 8; i++) {
		snprintf(out + i * 8, 9, "%08x", (unsigned)(s * (unsigned)(i + 1) + 0x9e3779b9u));
	}
	out[64] = '\0';
}

static void mk_hdr(char *dst, size_t dst_len, uint32_t total, uint32_t ver, uint32_t nonce,
		   const char *sha, int force, int resume, const char *key)
{
	snprintf(dst, dst_len,
		 "POST /api/ota HTTP/1.1\r\n"
		 "Content-Length: %u\r\n"
		 "X-Version: %u\r\n"
		 "X-Nonce: %u\r\n"
		 "X-Sha256: %s\r\n"
		 "%s%s"
		 "X-Key: %s\r\n\r\n",
		 total, ver, nonce, sha, force ? "X-Force: 1\r\n" : "",
		 resume ? "X-Resume: 1\r\n" : "", key);
}

/* 假口令校验：只看请求头里的 X-Key 是不是 "ok"。
 * 真实现（be_auth -> ota_auth_ok）会拿 X-Key 算 HMAC 再和 X-Auth 比；
 * 这里只要证明"鉴权回调被调用、失败会被门禁拦住"即可。 */
static bool fake_auth(void *ctx, const char *hdr, const char *canonical)
{
	(void)ctx;
	(void)canonical;
	return hdr != NULL && strstr(hdr, "X-Key: ok\r\n") != NULL;
}

int main(void)
{
	struct ota_backend be = {
		.begin = be_begin,
		.write = be_write,
		.finish = be_finish,
		.abort = be_abort,
		.ctx = NULL,   /* 下面立刻改成 &be：失败原因要落回结构自己的 err_msg */
	};
	char why[96];
	struct ota_req req;
	char hdr[512];
	char sha[65];
	uint8_t body[3000];

	be.ctx = &be;

	for (size_t i = 0; i < sizeof(body); i++) {
		body[i] = (uint8_t)(i * 7u + 3u);
	}
	fill_sha(body, sizeof(body), sha);

	/* ------------------------------------------------ 门禁 */
	printf("[gate]\n");
	mk_hdr(hdr, sizeof(hdr), sizeof(body), 5, 9, sha, 0, 0, "ok");
	chk(ota_req_check(hdr, 4, SLOT_MAX, 8, fake_auth, "5|3000|", &req, why, sizeof(why)) ==
		    OTA_CHECK_OK,
	    "normal request passes");
	chk(req.total == 3000 && req.ver == 5 && req.nonce == 9 && strcmp(req.sha, sha) == 0,
	    "parsed total/ver/nonce/sha ok");
	chk(strcmp(ota_check_token(OTA_CHECK_OK), "ok") == 0, "OK token = ok");

	{
		char h2[512];

		snprintf(h2, sizeof(h2), "POST /api/ota HTTP/1.1\r\nX-Version: 5\r\n\r\n");
		chk(ota_req_check(h2, 4, SLOT_MAX, 8, fake_auth, "x", &req, why, sizeof(why)) ==
			    OTA_CHECK_NO_HDR,
		    "missing Content-Length -> NO_HDR");
		chk(ota_check_http(OTA_CHECK_NO_HDR) == 400, "NO_HDR -> 400");

		mk_hdr(h2, sizeof(h2), sizeof(body), 5, 9, "abc", 0, 0, "ok");
		chk(ota_req_check(h2, 4, SLOT_MAX, 8, fake_auth, "5|3000|", &req, why, sizeof(why)) ==
			    OTA_CHECK_NO_SHA,
		    "X-Sha256 not 64 hex -> NO_SHA");

		mk_hdr(h2, sizeof(h2), sizeof(body), 5, 9, sha, 0, 0, "bad");
		chk(ota_req_check(h2, 4, SLOT_MAX, 8, fake_auth, "5|3000|", &req, why, sizeof(why)) ==
			    OTA_CHECK_AUTH,
		    "auth fails -> AUTH");
		chk(ota_check_http(OTA_CHECK_AUTH) == 401, "AUTH -> 401");

		mk_hdr(h2, sizeof(h2), 9999, 5, 9, sha, 0, 0, "ok");
		chk(ota_req_check(h2, 4, SLOT_MAX, 8, fake_auth, "5|9999|", &req, why, sizeof(why)) ==
			    OTA_CHECK_TOO_BIG,
		    "bigger than slot -> TOO_BIG");
		chk(ota_check_http(OTA_CHECK_TOO_BIG) == 413, "TOO_BIG -> 413");

		mk_hdr(h2, sizeof(h2), sizeof(body), 5, 9, sha, 0, 0, "ok");
		chk(ota_req_check(h2, 4, SLOT_MAX, 9, fake_auth, "5|3000|", &req, why, sizeof(why)) ==
			    OTA_CHECK_REPLAY,
		    "nonce not increasing -> REPLAY");
		chk(ota_req_check(h2, 4, SLOT_MAX, 8, fake_auth, "5|3000|", &req, why, sizeof(why)) ==
			    OTA_CHECK_OK,
		    "nonce exactly +1 -> OK");

		mk_hdr(h2, sizeof(h2), sizeof(body), 3, 9, sha, 0, 0, "ok");
		chk(ota_req_check(h2, 4, SLOT_MAX, 8, fake_auth, "3|3000|", &req, why, sizeof(why)) ==
			    OTA_CHECK_OLD_VER,
		    "version not newer -> OLD_VER");
		chk(ota_check_http(OTA_CHECK_OLD_VER) == 409, "OLD_VER -> 409");

		mk_hdr(h2, sizeof(h2), sizeof(body), 3, 9, sha, 1, 0, "ok");
		chk(ota_req_check(h2, 4, SLOT_MAX, 8, fake_auth, "3|3000|1", &req, why, sizeof(why)) ==
			    OTA_CHECK_OK,
		    "X-Force: 1 bypasses version gate");
	}

	/* ------------------------------------------------ full transfer */
	printf("[full: begin=0 -> run -> finish]\n");
	g_begin_off = 0;
	g_stored = 0;
	g_write_calls = 0;
	g_finish_rc = 0;
	g_err[0] = '\0';
	{
		struct ota_sess s;
		struct feed f = { .buf = body, .len = sizeof(body) };

		ota_sess_init(&s, &req);
		chk(s.total == 3000 && s.written == 0 && s.start_off == 0, "sess init from req");
		chk(ota_sess_run(&s, &be, feed_recv, &f, NULL, 0) == 0, "run ok");
		chk(s.written == 3000, "written == total");
		chk(g_stored == 3000 && memcmp(g_store, body, 3000) == 0,
		    "bytes in fake flash match exactly");
		chk(s.phase == OTA_PHASE_VERIFY, "phase is VERIFY after run");
		chk(ota_sess_finish(&s, &be, &req) == 0, "finish ok");
		chk(s.phase == OTA_PHASE_DONE, "phase is DONE after finish");
		chk(strcmp(ota_phase_str(s.phase), "done") == 0, "phase name = done");
	}

	/* ------------------------------------------------ pre-body */
	printf("[pre-body: 500 bytes already read with header]\n");
	g_begin_off = 0;
	g_stored = 0;
	g_write_calls = 0;
	{
		struct ota_sess s;
		struct feed f = { .buf = body + 500, .len = 2500 };

		ota_sess_init(&s, &req);
		ota_sess_run(&s, &be, feed_recv, &f, body, 500);
		chk(s.written == 3000, "pre 500 + recv 2500 = 3000");
		chk(memcmp(g_store, body, 3000) == 0, "no misalignment when joining");
	}

	/* ------------------------------------------------ resume */
	printf("[resume: begin=1024 -> only the rest]\n");
	g_begin_off = 1024;
	g_stored = 1024;
	g_write_calls = 0;
	g_finish_rc = 0;
	{
		struct ota_sess s;
		struct feed f = { .buf = body + 1024, .len = 3000 - 1024 };

		ota_sess_init(&s, &req);
		s.written = 1024;
		s.start_off = 1024;
		chk(ota_sess_run(&s, &be, feed_recv, &f, NULL, 0) == 0, "resume run ok");
		chk(s.written == 3000, "written == total after resume");
		chk(g_stored == 3000 && memcmp(g_store, body, 3000) == 0, "resumed at right offset");
	}

	/* ------------------------------------------------ digest mismatch */
	printf("[finish fails: digest mismatch]\n");
	g_begin_off = 0;
	g_stored = 0;
	g_write_calls = 0;
	g_finish_rc = -5;
	{
		struct ota_sess s;
		struct feed f = { .buf = body, .len = sizeof(body) };

		ota_sess_init(&s, &req);
		ota_sess_run(&s, &be, feed_recv, &f, NULL, 0);
		int rc = ota_sess_finish(&s, &be, &req);

		chk(rc != 0, "finish returns non-zero");
		chk(strcmp(s.token, "sha mismatch") == 0, "token = sha mismatch");
		chk(s.http_code == 500, "http_code = 500");
		chk(s.phase == OTA_PHASE_FAIL, "phase = fail");
	}

	/* ------------------------------------------------ write failure */
	printf("[write fails at 2nd call]\n");
	g_begin_off = 0;
	g_stored = 0;
	g_write_calls = 0;
	g_write_fail_at = 1;
	g_err[0] = '\0';
	g_tok = NULL;
	{
		struct ota_sess s;
		struct feed f = { .buf = body, .len = sizeof(body) };

		ota_sess_init(&s, &req);
		chk(ota_sess_run(&s, &be, feed_recv, &f, NULL, 0) != 0, "run returns non-zero");
		chk(s.token != NULL && strcmp(s.token, "flash write") == 0,
		    "token comes from backend err_token");
		chk(strcmp(s.err_msg, "flash write failed") == 0, "err_msg comes from backend");
	}
	g_write_fail_at = -1;

	/* ------------------------------------------------ short body */
	printf("[short body: peer closed early]\n");
	g_begin_off = 0;
	g_stored = 0;
	g_write_calls = 0;
	{
		struct ota_sess s;
		struct feed f = { .buf = body, .len = 1500 };

		ota_sess_init(&s, &req);
		int rc = ota_sess_run(&s, &be, feed_recv, &f, NULL, 0);

		chk(rc != 0, "run returns non-zero");
		chk(s.token != NULL && strcmp(s.token, "short body") == 0, "token = short body");
		chk(s.http_code == 400, "http_code = 400");
	}

	/* ------------------------------------------------ begin fail / abort */
	printf("[begin fails / abort no-op]\n");
	g_begin_off = -5;
	{
		struct ota_sess s = { 0 };

		chk(be.begin(be.ctx, &req) < 0, "begin returns negative");
		ota_sess_abort(&s, &be);
		chk(strcmp(ota_phase_str(OTA_PHASE_FAIL), "fail") == 0, "phase name table works");
	}

	printf("\nRESULT: %s (%d assertion(s) failed)\n", g_fails ? "FAIL" : "PASS", g_fails);
	return g_fails ? 1 : 0;
}
