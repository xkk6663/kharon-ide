import time
import serial

PORT = "COM9"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=1)
ser.setRTS(True)          # EN low -> reset
time.sleep(0.15)
ser.setRTS(False)         # release reset
time.sleep(0.05)

buf = []
deadline = time.time() + 8
while time.time() < deadline:
    line = ser.readline()
    if line:
        buf.append(line.decode("utf-8", "replace").rstrip())

ser.close()
print("---- captured %d lines ----" % len(buf))
for l in buf:
    print(l)
