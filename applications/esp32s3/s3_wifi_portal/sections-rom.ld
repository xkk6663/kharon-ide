/*
 * HTTP server 的资源段声明。
 *
 * HTTP_RESOURCE_DEFINE(portal_index, portal_service, ...) 会把每个资源塞进
 * `http_resource_desc_portal_service` 这个 iterable section；HTTP_SERVICE_DEFINE
 * 生成的 http_service_desc 里要引用它的 _start/_end 符号来遍历资源列表。
 *
 * 这个 section 名字是「按服务名动态生成」的，Zephyr 不可能预先知道，
 * 所以必须在应用侧用 ITERABLE_SECTION_ROM 显式声明一次。否则链接期报：
 *   undefined reference to `_http_resource_desc_portal_service_list_start'
 */
ITERABLE_SECTION_ROM(http_resource_desc_portal_service, 4)
