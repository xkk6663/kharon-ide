/*
 * cloudhub 的根 CA（scau-hub-ca，自签，服务器证书由它签发）。
 * 生成方式：tools/zephyr/gen_ca_inc.py，从 VPS /etc/mosquitto/certs/ca.crt 转出。
 * 内嵌进固件 = 证书固定（certificate pinning）：设备只信这一把根，
 * 中间人换证书必死。CA 有效期见证书本身，过期前要换发并重新刷板。
 */
#ifndef HUB_CA_INC_
#define HUB_CA_INC_
static const char hub_ca_pem[] =
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDDTCCAfWgAwIBAgIUBw3MpfxFd2cWfWr/H3Z+gNG9BTYwDQYJKoZIhvcNAQEL\n"
    "BQAwFjEUMBIGA1UEAwwLc2NhdS1odWItY2EwHhcNMjYxMTE2MDIzMTA0WhcNMzYx\n"
    "MTEzMDIzMTA0WjAWMRQwEgYDVQQDDAtzY2F1LWh1Yi1jYTCCASIwDQYJKoZIhvcN\n"
    "AQEBBQADggEPADCCAQoCggEBALXpIAK6yh0sqJF7sj6ZtjW9IwlgfQeddE0D77se\n"
    "LWd+f7LVQptOIOBW2uYqa+69QQb5D8fJPAs2kk7p0g6ilKfwRrhQOEIuCMpmhfev\n"
    "eWUE6J/U/3bUuAwrWTdeRGsJ02+BTkwrqPNsK2+CTHDwUTTrUmJiyERR0KrsfvUw\n"
    "u045O8TWLZatC+kQjw5lVyBk5AKsBx4qoyXwhVKVXEum/gfm8rMUt2lozsscshsA\n"
    "zv1Geue+bcwF4KKOENpjJ2zmcj4JzTSJ+y9MKZc4CKMfLtMmO93dv/MoENJP3Ei8\n"
    "FKaOECYFOfwhX2lQyrtCddLsHpX3LXp8BdJ7WONm0XMLIvUCAwEAAaNTMFEwHQYD\n"
    "VR0OBBYEFF5lAz7uFhRyNpDaZemqEfRKrloXMB8GA1UdIwQYMBaAFF5lAz7uFhRy\n"
    "NpDaZemqEfRKrloXMA8GA1UdEwEB/wQFMAMBAf8wDQYJKoZIhvcNAQELBQADggEB\n"
    "AJJrSy7yXeoyq3NCt26PHn67gjSZ1/bAM2vgdDFzwzKYnJsv39hWBtgk+pI9xQLT\n"
    "i0jBrPDM75jkQXQhQK4swR/OopTJMSqEkkd0z/vSsWBAi6g6YiKXN1HHsBSRePbs\n"
    "CR/SyhPErDbxc0jBz5lRanC80+/spqQXoqriogwSJQf3Usn913yxhnjH0aC6Rxqs\n"
    "OHbyncHwdXfXP2eIszyl7QyjNsuRz+dxkXqC58Zox2+PdU1l8k4qAPIYz5TWH25f\n"
    "V/GVD74Og5w/DrcL1hbcda6gORZbnuBu98i+0QuWLBwtKkB2er4To+J+gS4Be3Js\n"
    "DzjFInmfHcSROVeG7b7zCkk=\n"
    "-----END CERTIFICATE-----\n"
    "\0";
static const unsigned int hub_ca_pem_len = sizeof(hub_ca_pem) - 1;
#endif
