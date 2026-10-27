# usb-serial modules for FlashForge kernel 2.0.8

FlashForge firmware 2.0.0 (kernel package 2.0.8) ships a kernel without
usb-serial. `c5_socat.py` loads these two modules when the running kernel
has no usb-serial driver, so the gadget can bind to `flashloader` and appear
as `/dev/ttyUSB*`. Kernels with usb-serial built in (2.0.7, firmware 1.9.9)
never load them.

| File | SHA-256 |
|---|---|
| `usbserial.ko` | `149db538fda3d1f2896dd52a8b277ecc623f7556ee31f4bd8c0afc05e3ac73ec` |
| `usb-serial-simple.ko` | `f00d724c00304ecf6828650b3f15b9b5885ce9af24fa496cef78130d780b4fd8` |

Built by `FlashForge-C5-Modding-Group/c5-usb-serial-modules` from Ingenic's
X2600 Linux 5.10.186 source. Vermagic:
`5.10.186+ SMP preempt mod_unload MIPS32_R5 32BIT`. GPL-2.0, like the kernel.

Tested on a printer running 2.0.0: both load, `flashloader` binds the gadget,
and Klipper on the SBC reaches all four MCUs.

If loading fails, `c5_socat.py` logs why and falls back to usbfs:
`falling back to usbfs instead of usb-serial: <reason>` in
`/usr/data/logs/c5-socat.log`.
