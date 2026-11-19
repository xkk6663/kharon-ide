#!/usr/bin/env python3
"""读签名镜像头（mcuboot image_header v1）里的版本，不依赖 imgtool。

用法: python img_hdr.py <xxx.signed.bin>
"""
import struct
import sys

MAGIC = 0x96F3B83D

p = sys.argv[1]
with open(p, "rb") as f:
    data = f.read(32)

magic, load_addr, hdr_size_pad, img_size, flags = struct.unpack("<IIIII", data[:20])
home = struct.unpack("<BBHI", data[20:28])

print(f"file      : {p}")
print(f"magic     : 0x{magic:08x} {'OK' if magic == MAGIC else 'BAD'}")
print(f"load_addr : 0x{load_addr:08x}")
print(f"hdr_size  : {hdr_size_pad & 0xFFFF}  pad={hdr_size_pad >> 16}")
print(f"img_size  : {img_size}")
print(f"flags     : 0x{flags:08x}")
print(f"version   : {home[0]}.{home[1]}.{home[2]}.{home[3]}")
