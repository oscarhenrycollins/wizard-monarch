# Monarch for Wizard

A read-only Monarch connector. Sign in through Monarch's official OAuth + PKCE flow; other apps and your agent can read a private, locally synced copy of accounts and transactions through `wizard.bank-connection/1`, plus budget, cash flow, net worth, recurring bills, categories and investment snapshots.

## Install

Find **Monarch** in the [Wizard App Store](https://www.aiwizards.com/wizard-apps), or run:

```sh
wam install wizard-monarch
```

The catalog pins the checked Git commit. The runtime uses a digest-pinned Python base and packaged source; it needs no external dashboard, image build, or companion app.

## Connect

Open Monarch in Wizard. Tap **Sign in to Monarch**, sign in on Monarch's own page and allow **Read** access. After Allow, the browser lands on a loopback address and may show an error; copy that entire address into the connector's second step. Never paste credentials or the callback address in agent chat.

A Monarch account/subscription with official MCP access is required. Accounts linked through Finicity are not available through Monarch MCP. Monarch applies weekly MCP usage limits, so routine reads use the local copy; sync runs every 12 hours. The setup screen shows connection status and last sync, not a financial dashboard.

Disconnect revokes access where supported and deletes tokens and the app's local copy. It does not change your Monarch account or finances.

## Privacy and boundaries

- Requests only `mcp:read`; no financial write endpoints or tools.
- Tokens and local database stay in `/data/private` (0700 directory, 0600 files).
- MCP is box-only and refuses forwarded requests. Web requests enforce trusted hosts/origins.
- OAuth uses generic task labels for Monarch's required `intent`; no chat context is sent.
- No telemetry or financial fixtures are included. This is an independent connector, not an official Monarch product.

## Development

Python standard library only. Run `python3 tools/test_contract.py` and `python3 tools/test_release.py` (synthetic provider only). With the Wizard builder on PATH, run `python3 tools/package.py`, `wam check . --proposal`, then `python3 tools/package.py --check` to validate the checked-in package. Runtime metadata is in `wizard.md`, API in `openapi.json`, and the agent guide in `skill/SKILL.md`.

GPL-3.0-only; see LICENSE and NOTICE.md. Publisher identity signing, if desired, happens only in Wizard's owner signing sheet; this repository does not claim a signed publisher identity.
