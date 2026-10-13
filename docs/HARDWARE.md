# Hardware

## What the reference setup uses

| Part | Used | Why |
|---|---|---|
| Single-board computer | **Raspberry Pi 4 Model B, 2 GB** | Sentry USB's recommended board; 2.4 + 5 GHz Wi-Fi; its USB-C port can act as a USB *device* (what the car needs). 1 GB is enough; more RAM buys nothing here. |
| microSD | **256 GB high-endurance** (SanDisk High Endurance or Samsung PRO Endurance) | the car writes continuously; ordinary cards wear out. Size sets how many weeks of events the Pi can hold if it can't reach Wi-Fi. |
| Cable | **CableCreation 1 ft USB-A → USB-C, 10 Gbps, 3 A** | must carry data, not just charge. A USB 3 cable has heavier power wires, which keeps the Pi stable on a car port. A-to-C, not C-to-C. |
| Case | **Argon NEO** (aluminium, passive) | cools well in a hot glovebox, but metal hurts Wi-Fi; see below. |

2026 prices (memory shortage): Pi 4 2 GB ~$45–70, card $21–80, cable ~$10,
case $12–20. Buy the board from an approved reseller (PiShop, CanaKit,
Adafruit, Micro Center); third-party marketplace listings were well above list.

## Alternatives

* **Pi 5**: supported and faster; mind its higher power draw on a car port.
* **Pi Zero 2 W**: cheap and tiny, but slower and 2.4 GHz-only (which can
  actually help reach; see below).
* **Pi 3 Model B/B+**: not supported (no USB device mode).
* **Commercial boxes**: nothing off the shelf archives unattended to a NAS as
  of 2026. TesCam Hub (~$189) sits between the car and a stick and syncs to its
  own app; travel routers that "read a USB drive" (e.g. NewQ Filehub) are USB
  *hosts* and cannot stand in for the drive the car needs.

## Where it plugs in

* Model 3 / Model Y from about 2021: the **USB-A port in the glovebox**, which
  Tesla intends for the dashcam drive. Earlier cars: a front console port.
  Rear ports are charge-only.
* Use the Pi's **USB-C port** (next to the micro-HDMI ports). It powers the Pi
  and makes it appear as a USB drive. The Pi's four USB-A ports are for
  plugging things *into* the Pi and cannot act as the car's drive.
* The drive's name doesn't matter to the car; it looks for a `TeslaCam` folder.

## Power

The car powers its USB ports while it is awake: driving, for a while after
parking, while charging (including scheduled charging), and in Sentry Mode. The
Pi archives during those windows. Unplugging is safe: the car cuts USB power
every time it sleeps and Sentry USB is built for that (its system disk is
read-only).

## Wi-Fi reach

The Pi's antenna is much weaker than a phone's or the car's, and in the
reference car it sits in a glovebox, inside a metal case, some distance from
the router. Measured:

| Where | Band | Signal |
|---|---|---|
| Indoors, near the router | 5 GHz | −63 dBm |
| same spot | **2.4 GHz** | **−44 dBm** |
| Glovebox, car parked at home | 5 GHz | never connected |
| Glovebox, car parked at home | **2.4 GHz** | **−67 dBm**, 65 Mbit/s link: uploads fine |

2.4 GHz travels farther and through walls, glass and metal much better. If
your router uses one name for both bands, the Pi may pick 5 GHz on its own;
lock it to 2.4 GHz ([docs/PI_SETUP.md](PI_SETUP.md#wi-fi-reach)). If that's not
enough: take the Pi out of a metal case, use a plastic or vented case with
stick-on heatsinks, or add a mesh node / extender near where the car parks.
A phone held at the open glovebox showing two bars was not enough for the Pi
on 5 GHz.

## Sources

* Sentry USB (Rusty): https://github.com/Sentry-Six/Sentry-USB-Rusty
* Tesla USB drive requirements:
  https://www.tesla.com/ownersmanual/2012_2020_models/en_gb/GUID-F311BBCA-2532-4D04-B88C-DBA784ADEE21.html
* USB power while parked: https://notateslaapp.com/news/2186/how-to-keep-your-teslas-usb-ports-powered-on
