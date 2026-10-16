# Assets

Real captures from the reference machine. Refreshed manually before each
tagged release. If you reproduce these on your own machine and see
materially different numbers, please open an issue with your own
`bench-output.txt` attached.

## Files

- `doctor-output.txt` - what `iris-mcp-doctor` prints on a properly
  configured Windows 11 install. Three monitors at mixed DPI scales
  (100% / 125% / 150%).
- `bench-output.txt` - full live accuracy bench run, all 5 scenarios x
  3 monitors, geometric click path (`--no-invoke`).

The reference machine for these captures has:
- Windows 11 Pro 26100
- Python 3.12.7
- A primary 1440p @ 125% scale, a 1080p @ 100% scale, and a 4K @ 150% scale

Your numbers will differ on different hardware (different DPI scales,
different display fonts). What matters is the relative shape: per-scenario
correct-button rates, miss-distance percentiles, OCR -> UIA upgrade count.
