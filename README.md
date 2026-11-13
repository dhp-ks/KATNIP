# K.ATNIP

## Overview

K.ATNIP (Keystone Automated Network Traffic Inspection Platform) is a
macOS-based automation framework for repeatable website privacy and
network traffic analysis.

Each run can:

-   Connect to a ProtonVPN WireGuard profile.
-   Launch an isolated Chrome instance using your `mitmchrome`
    launcher.
-   Record the Chrome window using Apple's ScreenCaptureKit.
-   Capture detailed network traffic with mitmproxy.
-   Execute a configurable browser journey.
-   Generate third-party domain reports.

## Requirements

-   macOS 13+
-   Python 3.11+
-   Google Chrome
-   Xcode Command Line Tools
-   Swift
-   ProtonVPN Plus
-   wireguard-tools (`wg`, `wg-quick`)
-   mitmproxy
-   Playwright

Grant Screen Recording permission to the terminal that launches KATNIP.

## Install

``` bash
make install

cd native/window-recorder
swift build -c release

cd ../..
cp configs/example.yaml configs/local.yaml
```

## WireGuard

Install:

``` bash
brew install wireguard-tools
```

Download Proton WireGuard configs into:

``` text
vpn/
    us-ca.conf
    us-ny.conf
```

Verify setup:

``` bash
.venv/bin/katnip doctor --config configs/local.yaml
```

Run KATNIP normally:

``` bash
.venv/bin/katnip run --config configs/local.yaml
```

Only `wg-quick` should require sudo. Recommended to setup passwordless sudo for wg-quick.


## Output

``` text
output/
└── <run>/
    ├── logs/
    ├── mitm/
    ├── recording/browser.mp4
    └── report/
```