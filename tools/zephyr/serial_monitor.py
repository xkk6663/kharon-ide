"""串口监视：把 COM 口上的启动/运行日志原样落盘，便于对齐"telnet 连上之后发生了什么"。

用法：
    python tools\\serial_monitor.py COM11 120 logs\\serial-telnet.log

注意：打开 USB-Serial-JTAG（S3 原生 USB 口）时 DTR/RTS 的默认电平会让板子复位一次，
所以这条命令的前几行一定是启动日志 —— 正好用来确认它到底跑到哪一步。
"""
import datetime
import sys
import time

import serial


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM11"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    out = sys.argv[3] if len(sys.argv) > 3 else "logs/serial.log"

    # dsrdtr/rtscts 必须显式关掉：否则 pyserial 打开端口时会把两条控制线拉低，
    # 表现成"一开监视板子就重启"，容易把真正的现象盖掉。
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    ser.dsrdtr = False
    ser.rtscts = False
    ser.open()

    t_end = time.time() + secs
    lines = 0

    with open(out, "w", encoding="utf-8", buffering=1) as f:
        head = f"# serial monitor {port} @115200, {secs:.0f}s, started {datetime.datetime.now():%H:%M:%S}\n"
        f.write(head)
        print(head.strip())
        while time.time() < t_end:
            data = ser.read(4096)
            if not data:
                continue
            text = data.decode("utf-8", "replace")
            f.write(text)
            lines += text.count("\n")
            print(text, end="", flush=True)

    ser.close()
    print(f"\n# done: {lines} lines -> {out}")


if __name__ == "__main__":
    main()
