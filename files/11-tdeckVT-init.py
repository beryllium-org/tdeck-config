rename_process("tdeck display init")
from drivers.tdeckVT import tdeckVT

pv[0]["consoles"]["tty1"] = tdeckVT()
be.based.run("mknod DISPLAY")
be.devices["DISPLAY"][0] = be.devices["gpiochip"][0].pin("DISPLAY", force=True)
del tdeckVT
