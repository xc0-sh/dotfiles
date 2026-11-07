# Test layers

- `iso/`: deterministic disk-identity, destructive confirmation and failure tests
  using temporary synthetic sysfs/debconf fixtures. Never uses a real disk.
- `bootstrap/`: package/profile and configuration regression tests.
- `vm/`: fresh UEFI libvirt installs, graphical-session and reboot acceptance.

Use `make check` for the fast local/CI layer. Keep real VM evidence separate:
mocked unit tests do not prove a compositor starts or that a production prompt
is reachable. Final acceptance must use the exact ISO and Git ref being released.
