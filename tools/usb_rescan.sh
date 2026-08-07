#!/usr/bin/env bash
# Rebind the Intel xHCI controller. Use when lsusb shows only root hubs after
# a USB tree crash (often after SIGKILL mid-dd). Requires root.
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi
echo "Before:"; lsusb | sed 's/^/  /'
for link in /sys/bus/pci/drivers/xhci_hcd/0000:*; do
  [[ -e "$link" ]] || continue
  addr=$(basename "$link")
  echo "unbind $addr"
  echo "$addr" > /sys/bus/pci/drivers/xhci_hcd/unbind
  sleep 2
  echo "bind $addr"
  echo "$addr" > /sys/bus/pci/drivers/xhci_hcd/bind
done
sleep 3
echo "After:"; lsusb | sed 's/^/  /'
lsblk -o NAME,SIZE,MODEL,TRAN,TYPE | sed 's/^/  /'
