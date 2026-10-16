# Security Policy

## Supported versions

Iris is in early public release. Only the latest tagged version is
supported. There are no LTS branches.

## Threat model in one paragraph

Iris is a desktop control MCP server. By design it can read pixels off
your screen, synthesize mouse and keyboard input, kill processes, and
write to the Windows registry (the last two only with explicit
confirmation flags). It is intended to be driven by an LLM you trust,
running locally. **Do not expose it over a network without an
authentication layer between you and untrusted callers.** The provided
`mcp serve` is stdio-only for this reason.

## What we consider in scope

- Privilege escalation via Iris (e.g. registry writes that bypass
  `confirm=True`, kill_process that bypasses `force=True`).
- Information disclosure beyond what the caller's user account can already
  see (e.g. reading another user's clipboard, capturing the lock screen).
- Code execution via crafted YAML in recipes / apps.yaml.
- Argument injection in tool parameters that escapes Win32 API
  expectations.

## What we don't consider in scope

- An LLM you authorized doing something with Iris that you didn't expect.
  That's an LLM-trust problem, not an Iris bug.
- "Iris can click on UI" - that's the point of the tool.
- Tesseract/uiautomation/pywin32 vulnerabilities. Please report those
  upstream.

## How to report

Open a private security advisory on GitHub (Security tab, "Report a
vulnerability"). Please do not file a public issue. Include:

- A minimal repro
- Your OS version, Python version, Iris version
- What you expected to happen
- What actually happened

Please give us 14 days before public disclosure. We aim to acknowledge
within 72 hours and patch within 30 days, sooner for critical issues.

## Hardening recommendations for operators

- Iris should run as your normal user account, not Administrator.
  Registry writes that need admin will then fail gracefully rather than
  succeeding silently.
- If you're driving Iris from a network-exposed LLM (Claude Desktop is
  local, but custom setups may not be), put it behind an
  authentication-aware proxy.
- The `kill_process` and registry-write tools require explicit
  `force=True` / `confirm=True` flags. Configure your LLM client to
  require human confirmation for any tool call that includes those.
