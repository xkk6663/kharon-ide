/*
 * ota_engine —— OTA 协议层的实现（平台无关）
 *
 * 依赖只有 libc（string/snprintf）与 stdint。
 * 不认识 Zephyr，不认识 flash，不认识 socket —— 所以可以被任何工程直接
 * 加进自己的构建，也可以被单元测试在 PC 上跑。
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ota_engine.h"

/* 本模块只承诺依赖 stdint/stddef/stdio/string —— errno 是为数不多的例外，
 * 而且只用来"给一个负数"（调用方只判 `<0`，不解释具体值）。
 * 但因为它要能在 PC 上编译（MinGW 的 errno.h 没有 POSIX 那几个名字），
 * 缺失的宏必须给同义替代，否则"平台无关"就成了"只在 Linux 上成立"。 */
#ifndef EBADMSG
#define EBADMSG EIO
#endif
#ifndef ECONNRESET
#define ECONNRESET EIO
#endif

/* 一次 recv 的目标块大小。与旧实现（app 里的 OTA_CHUNK）保持 2048：
 * 这个值决定了 TCP 收包的节奏，改了等于改了吞吐与内存占用，不是纯常量。 */
#define OTA_SESS_CHUNK 2048

const char *ota_phase_str(enum ota_phase ph)
{
	static const char *const names[OTA_PHASE_COUNT] = {
		[OTA_PHASE_IDLE] = "idle",
		[OTA_PHASE_HEAD] = "head",
		[OTA_PHASE_RECV] = "recv",
		[OTA_PHASE_VERIFY] = "verify",
		[OTA_PHASE_DONE] = "done",
		[OTA_PHASE_FAIL] = "fail",
	};

	if (ph < 0 || ph >= OTA_PHASE_COUNT || names[ph] == NULL) {
		return "unknown";
	}
	return names[ph];
}

/* 在 [0, len) 里找 "\r\n\r\n"，返回其起始下标，找不到返回 -1。 */
int ota_find_hdr_end(const uint8_t *buf, size_t len)
{
	for (size_t i = 0; i + 3 < len; i++) {
		if (buf[i] == '\r' && buf[i + 1] == '\n' && buf[i + 2] == '\r' &&
		    buf[i + 3] == '\n') {
			return (int)i;
		}
	}
	return -1;
}

/* 取一个头的值：找 "\r\nX-Name:"，返回指向值首字符的指针。
 * 前置 "\r\n" 是为了不把 "X-Name" 误配成 "X-Name-Foo" 的后缀。 */
const char *ota_hdr_get(const char *hdr, const char *name)
{
	char pat[48];

	if (hdr == NULL || name == NULL) {
		return NULL;
	}

	snprintf(pat, sizeof(pat), "\r\n%s:", name);

	const char *p = strstr(hdr, pat);

	if (!p) {
		return NULL;
	}
	p += strlen(pat);
	while (*p == ' ') {
		p++;
	}
	return p;
}

uint32_t ota_hdr_get_u32(const char *hdr, const char *name, uint32_t dflt)
{
	const char *v = ota_hdr_get(hdr, name);

	if (!v) {
		return dflt;
	}
	return (uint32_t)strtoul(v, NULL, 10);
}

/* 取一个头到定长缓冲里（只拷贝 [0-9a-f]，遇非十六进制就停）。 */
size_t ota_hdr_get_hex(const char *hdr, const char *name, char *dst, size_t dst_len)
{
	const char *v = ota_hdr_get(hdr, name);

	if (!v || dst_len == 0) {
		return 0;
	}

	size_t n = 0;

	while (n + 1 < dst_len) {
		char c = v[n];

		if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
			break;
		}
		dst[n] = c;
		n++;
	}
	dst[n] = '\0';
	return n;
}

bool ota_hex_eq(const char *a, const char *b, size_t n)
{
	if (a == NULL || b == NULL || strlen(a) != n || strlen(b) != n) {
		return false;
	}

	uint8_t diff = 0;

	for (size_t i = 0; i < n; i++) {
		diff |= (uint8_t)(a[i] ^ b[i]);
	}
	return diff == 0;
}

void ota_hex_encode(const uint8_t *in, size_t n, char *out)
{
	for (size_t i = 0; i < n; i++) {
		out[i * 2] = (char)('0' + (in[i] >> 4) + ((in[i] >> 4) > 9 ? 39 : 0));
		out[i * 2 + 1] = (char)('0' + (in[i] & 0x0f) + ((in[i] & 0x0f) > 9 ? 39 : 0));
	}
	out[n * 2] = '\0';
}

uint32_t ota_seg_index(uint32_t off, uint32_t seg_size)
{
	if (seg_size == 0) {
		return 0;
	}
	return off / seg_size;
}

uint32_t ota_seg_base(uint32_t seg_idx, uint32_t seg_size)
{
	return seg_idx * seg_size;
}

uint32_t ota_seg_count(uint32_t total, uint32_t seg_size)
{
	if (seg_size == 0) {
		return 0;
	}
	return (total + seg_size - 1) / seg_size;
}

/* ------------------------------------------------------------ 请求门禁 */
enum ota_check ota_req_check(const char *hdr, uint32_t local_ver, uint32_t slot_max,
			     uint32_t last_nonce, ota_auth_cb auth, void *auth_ctx,
			     struct ota_req *out, char *why, size_t why_len)
{
	if (hdr == NULL || out == NULL) {
		return OTA_CHECK_NO_HDR;
	}

	memset(out, 0, sizeof(*out));

	out->total = ota_hdr_get_u32(hdr, "Content-Length", 0);
	out->ver = ota_hdr_get_u32(hdr, "X-Version", 0);
	out->nonce = ota_hdr_get_u32(hdr, "X-Nonce", 0);

	const char *force_s = ota_hdr_get(hdr, "X-Force");

	out->force = (force_s != NULL && *force_s == '1');

	if (out->total == 0 || out->ver == 0 || out->nonce == 0) {
		if (why && why_len) {
			snprintf(why, why_len,
				 "missing required header (Content-Length / X-Version / X-Nonce)");
		}
		return OTA_CHECK_NO_HDR;
	}

	/* S2 的"期望摘要"：没有它，设备只能证明"Flash 里写的和收到的一致"，
	 * 证明不了"这就是被批准的那一版固件"。缺失直接拒，不是"记录备查"。 */
	if (ota_hdr_get_hex(hdr, "X-Sha256", out->sha, sizeof(out->sha)) != 64) {
		if (why && why_len) {
			snprintf(why, why_len,
				 "missing X-Sha256 (64 hex chars expected digest)");
		}
		return OTA_CHECK_NO_SHA;
	}

	/* S3 鉴权：放在体积/版本门禁之前，未授权方连"包有多大"都问不出来。
	 * 规范化串两端必须逐字一致，本工程定为：ver|size|sha256|nonce|force */
	{
		char canonical[192];

		snprintf(canonical, sizeof(canonical), "%u|%u|%s|%u|%d", out->ver,
			 out->total, out->sha, out->nonce, out->force ? 1 : 0);

		if (auth != NULL && !auth(auth_ctx, hdr, canonical)) {
			if (why && why_len) {
				snprintf(why, why_len, "ota auth failed (bad HMAC / bad X-Key)");
			}
			return OTA_CHECK_AUTH;
		}
	}

	/* S6：物理红线，超了就一个字节都不写 */
	if (out->total > slot_max) {
		if (why && why_len) {
			snprintf(why, why_len, "image too big: %u > slot max %u", out->total,
				 slot_max);
		}
		return OTA_CHECK_TOO_BIG;
	}

	/* S3：nonce 必须严格递增（防重放）。force 越不过这一条。 */
	if (out->nonce <= last_nonce) {
		if (why && why_len) {
			snprintf(why, why_len, "nonce replay (nonce <= last used)");
		}
		return OTA_CHECK_REPLAY;
	}

	/* S4：版本门禁。设备端自己判，不因为发起端说"已比对"就放行。
	 * force 只允许越过"版本不升"，越不过 size / nonce / 完整性。 */
	if (out->ver <= local_ver && !out->force) {
		if (why && why_len) {
			snprintf(why, why_len, "version %u not newer than %u", out->ver,
				 local_ver);
		}
		return OTA_CHECK_OLD_VER;
	}

	const char *resume_s = ota_hdr_get(hdr, "X-Resume");

	out->resume = (resume_s != NULL && *resume_s == '1');

	return OTA_CHECK_OK;
}

uint16_t ota_check_http(enum ota_check c)
{
	switch (c) {
	case OTA_CHECK_OK:
		return 200;
	case OTA_CHECK_NO_HDR:
	case OTA_CHECK_NO_SHA:
		return 400;
	case OTA_CHECK_AUTH:
		return 401;
	case OTA_CHECK_TOO_BIG:
		return 413;
	case OTA_CHECK_REPLAY:
		return 403;
	case OTA_CHECK_OLD_VER:
		return 409;
	default:
		return 400;
	}
}

const char *ota_check_token(enum ota_check c)
{
	switch (c) {
	case OTA_CHECK_OK:
		return "ok";
	case OTA_CHECK_NO_HDR:
		return "missing header";
	case OTA_CHECK_NO_SHA:
		return "missing sha";
	case OTA_CHECK_AUTH:
		return "badauth";
	case OTA_CHECK_TOO_BIG:
		return "too big";
	case OTA_CHECK_REPLAY:
		return "replay";
	case OTA_CHECK_OLD_VER:
		return "version older";
	default:
		return "bad request";
	}
}

/* ------------------------------------------------------------ 会话推进 */
void ota_sess_init(struct ota_sess *s, const struct ota_req *req)
{
	memset(s, 0, sizeof(*s));
	s->phase = OTA_PHASE_RECV;
	s->total = req->total;
	s->ver = req->ver;
	s->nonce = req->nonce;
}

/* 失败寄存器：一处填写，app 只负责按 http_code / token 说话 */
static int sess_fail(struct ota_sess *s, int32_t rc, uint16_t http, const char *token,
		     const char *msg)
{
	s->err = rc;
	s->http_code = http;
	s->token = token;
	if (msg) {
		snprintf(s->err_msg, sizeof(s->err_msg), "%s", msg);
	}
	s->phase = OTA_PHASE_FAIL;
	return (int)rc;
}

int ota_sess_run(struct ota_sess *s, const struct ota_backend *be,
		 ota_recv_cb recv, void *recv_ctx,
		 const uint8_t *pre, size_t pre_len)
{
	uint8_t chunk[OTA_SESS_CHUNK];
	int rc;

	if (s == NULL || be == NULL || recv == NULL) {
		return -EINVAL;
	}

	/* header 读完时可能已经顺手读进来一截 body，先处理它 */
	if (pre_len > 0) {
		uint32_t avail = s->total - s->written;

		if (pre_len > avail) {
			pre_len = avail;
		}
		if (pre_len > 0) {
			rc = be->write(be->ctx, pre, pre_len);
			if (rc != 0) {
				return sess_fail(s, rc, 500,
						 be->err_token ? be->err_token : "flash write",
						 be->err_msg[0] ? be->err_msg : "write failed");
			}
			s->written += (uint32_t)pre_len;
		}
	}

	while (s->written < s->total) {
		uint32_t want = s->total - s->written;
		size_t got = 0;

		if (want > sizeof(chunk)) {
			want = sizeof(chunk);
		}

		rc = recv(recv_ctx, chunk, want, &got);
		if (rc != 0) {
			return sess_fail(s, rc, 500, "recv", "recv failed mid-transfer");
		}
		if (got == 0) {
			return sess_fail(s, -ECONNRESET, 400, "short body",
					 "peer closed mid-transfer (short body)");
		}

		rc = be->write(be->ctx, chunk, got);
		if (rc != 0) {
			return sess_fail(s, rc, 500,
					 be->err_token ? be->err_token : "flash write",
					 be->err_msg[0] ? be->err_msg : "write failed");
		}
		s->written += (uint32_t)got;
	}

	s->phase = OTA_PHASE_VERIFY;
	return 0;
}

int ota_sess_finish(struct ota_sess *s, const struct ota_backend *be,
		    const struct ota_req *req)
{
	s->phase = OTA_PHASE_VERIFY;

	int rc = be->finish(be->ctx, req);

	if (rc != 0) {
		return sess_fail(s, rc, 500,
				 be->err_token ? be->err_token : "sha mismatch",
				 be->err_msg[0] ? be->err_msg : "finish failed");
	}

	s->phase = OTA_PHASE_DONE;
	return 0;
}

void ota_sess_abort(struct ota_sess *s, const struct ota_backend *be)
{
	if (be != NULL && be->abort != NULL) {
		be->abort(be->ctx);
	}
	if (s != NULL) {
		s->phase = OTA_PHASE_FAIL;
	}
}
