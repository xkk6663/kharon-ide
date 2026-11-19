/*
 * dns_hijack.h —— captive portal 的 DNS 应答器（L2）
 */
#ifndef DNS_HIJACK_H_
#define DNS_HIJACK_H_

void start_dns_hijack(void);           /* 起线程并 bind udp/53 */
void dns_self_test(void);              /* 从 AP 网段查一次，验证劫持生效 */

#endif /* DNS_HIJACK_H_ */
