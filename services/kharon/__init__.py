"""kharon —— Kharon（Zephyr 模式）产品入口的命名空间。

与 ``elab`` 的关系（方案 §2.2「单仓双发行」）
--------------------------------------------
内核（``services/elab``）只有一套；两个产品入口并列：

    elab.cmd    classic（CubeMX / AT32 / ESP-IDF 原生链）—— 只修不扩
    kharon.cmd  Kharon（Zephyr 一条链）—— 演进方向

``kharon`` 是**薄壳**：选主机事实源（``kharon.host.yaml``）+ 三条自有子命令
（``deps`` / ``where`` / ``doctor``），其余参数原样转给 ``python -m elab``。
刻意不做"另起一套内核"——那样立刻出现两份 progress/run/plan 的真相。
"""
