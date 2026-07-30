# Skydimo "Rainbow Music LED" USB adapter — wire protocol

Reverse-engineered from `Rainbow-Music-Led-22.9.11-Windows-AMD64.exe`.

## What the device is

A CH340 USB-serial bridge (`1a86:7523`) in front of a microcontroller that drives
an addressable RGB strip. It shows up on Linux as `/dev/ttyUSB0`.

## What the vendor software is

An NSIS installer wrapping a Qt6 application (`rmlapp.exe`) that is a **rebranded
HyperHDR 14 fork**. The binary still carries its build paths:

```
F:\Documents\GitHub\HyperHDR-14\sources\led-drivers\serial\DriverSerialSkydimondUsb.cpp
F:\Documents\GitHub\HyperHDR-14\sources\led-drivers\serial\ProviderSerial.cpp
F:\Documents\GitHub\HyperHDR-14\sources\led-drivers\net\DriverNetSkydimondWiFi.cpp
```

`DriverSerialSkydimondUsb` (device type `skydimond-usb`) is a **vendor-private
driver**. It does not exist upstream, which is why no stock Hyperion or HyperHDR
build can drive this adapter.

The default device config embedded in the app's web resources:

```json
{"type":"skydimond-usb","colorOrder":"rgb","delayAfterConnect":2000,
 "usb_mode":"pro_max","white_channel_calibration":false,
 "refreshTime":0,"output":"auto","id_device":"auto","maxRetry":180}
```

## Link

**1 000 000 baud, 8N1.** Not 115200 — at 115200 the device is silent.

## Handshake

The MCU boots into a *command* mode and **latches** into *data* mode after a
successful handshake. Once latched it ignores commands until the MCU is reset, so
the driver pulses DTR low first:

```
DTR low, RTS low        ~300 ms
DTR high, RTS high
wait ~2 s                          (the vendor's delayAfterConnect: 2000)

-> b'r'      <- b'R2A2\r\n'        device id
-> b'm'      <- b'ok\r\n'          enter data mode
```

**This DTR reset is the whole reason the strip appears dead on Linux.** Windows'
serial stack toggles DTR when opening a port; pyserial and Hyperion do not, so
the handshake never completes, the device never leaves command mode, and it
discards every pixel frame you send.

Observed on real hardware:

| baud | `r` | `m` |
|---|---|---|
| 115200 | *(silence)* | *(silence)* |
| **1000000** | `R2A2\r\n` | `ok\r\n` |
| 921600, 500000, 460800, 250000, 57600, 9600 | *(silence)* | *(silence)* |

`ProviderSerial::performHandshake` also contains Vietnamese diagnostics for both
commands ("Không nhận được phản hồi từ lệnh r" / "…lệnh m"), confirming the two
commands and the `ok` reply.

## Frame

```
'T' 'n'  X   hi  lo  chk   RGB*N   [wcLimit wcR wcG wcB]   f1  f2  f3
└──── 6-byte header ─────┘ └payload┘ └── optional ──────┘ └ trailer ┘
```

| field | value |
|---|---|
| `[0]` | `0x54` `'T'` |
| `[1]` | `0x6E` `'n'` |
| `[2]` | `0x54` `'T'` when white-channel calibration is active, else `0x68` `'h'` |
| `[3]` | `((N - 1) >> 8) & 0xFF` |
| `[4]` | `(N - 1) & 0xFF` |
| `[5]` | `hi ^ lo ^ 0x55` |

`N` is the LED count. This is the Adalight/AWA header shape with a vendor magic
in place of `Ada`/`Awa`.

The payload is `N` raw RGB triples, in the configured colour order (`rgb` by
default). The four white-channel bytes are present only when
`white_channel_calibration` is on *and* `usb_mode == "pro_max"`.

The trailer is a Fletcher triple over the payload **and** the white-channel
bytes if present — not over the header:

```c
uint8_t f1 = 0, f2 = 0, ext = 0;
for (size_t i = 0; i < len; i++) {
    ext = (ext + (data[i] ^ (uint8_t)i)) % 255;
    f1  = (f1  +  data[i])              % 255;
    f2  = (f2  +  f1)                   % 255;
}
out[0] = f1;
out[1] = f2;
out[2] = (ext != 0x41) ? ext : 0xAA;   /* 0x41 is reserved */
```

The header is rebuilt whenever `N` changes (`SkydimondUsb led count has changed
(old: %d, new: %d). Rebuilding header.`). The firmware drives exactly the number
of LEDs named in the header — send a smaller `N` and the tail of the strip simply
stays dark, which is what the calibration wizard's optional bisection leans on.

## How it was recovered

1. `7z x` the NSIS installer; `rmlapp.exe` links `Qt6SerialPort.dll`.
2. `strings` on `rmlapp.exe` exposes the HyperHDR-14 build paths, naming the
   exact driver translation units to look at.
3. Compute the `.rdata` virtual addresses of the driver's log strings from the PE
   section table, then grep an `objdump -d` disassembly for RIP-relative
   references to them. That locates `DriverSerialSkydimondUsb::writeFiniteColors`
   at `0x1400f53a0` and the header builder at `0x1400f1570`.
4. Read those two functions. The header builder stores `0x54`, `0x6E`, then
   `0x54`/`0x68` depending on the calibration flag, `ror $8` on the 16-bit
   `count-1` (a big-endian store), and the `^ 0x55` check byte. The frame writer
   contains the `0x80808081` magic-multiply modulo-255 sequences of the Fletcher
   loop, and the `cmp $0x41 / cmovne` that substitutes `0xAA`.
5. Confirm against the hardware: probe the baud rates, find the handshake, and
   discover the DTR reset requirement when the device went silent after its first
   successful `m`.

## Also in the binary, not implemented here

`DriverNetSkydimondWiFi` — a Wi-Fi sibling using mDNS discovery, a MAC
handshake and a REST API, for the network version of the adapter. This project
only implements the USB device.
