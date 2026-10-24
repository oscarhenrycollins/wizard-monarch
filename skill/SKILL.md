---
name: wizard-monarch
description: Use when the owner asks about his Monarch data (accounts, net worth, spending, budget, cash flow, recurring bills, transactions).
version: 0.1.1
author: Hermes Agent
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [monarch, finance, transactions, budget, net-worth]
    related_skills: [wizard-teller, card-benefits]
---
# Monarch

The box's Monarch connector (`bank-connection` spot, same contract as Teller). Read-only. It keeps a local copy, so prefer the local tools:

- `bank_status`: sign-in state, accounts by institution, last sync, Monarch calls used this week.
- `bank_accounts`: every Monarch account with type, last four and balance.
- `bank_transactions`: search the copy by date range, account (id, last four or name), merchant text, category or group, amount range, posted/pending. Costs no Monarch quota.
- `monarch_snapshot` kind=budget|cashflow|net_worth|recurring|categories|investments: Monarch's own numbers as of the last daily refresh.
- `bank_sync`: pull fresh data now (also runs every 12 hours).
- `monarch_call`: live pass-through to Monarch's official read tools (GetSpendingByCategory, GetGoals, GetMerchants, ...). Omit `tool` to list them. Each call uses the weekly quota, so use it only when the copy can't answer.

Sign-in happens only in the Monarch app screen (/wizard-monarch/). Never ask the owner for his Monarch password, the sign-in address or a code in chat. If `bank_status` shows `not_connected` or `needs_login`, tell him to open the Monarch app and tap Sign in. If it shows `rate_limited`, the weekly limit is used up; answer from the copy and say how old it is.

Amounts follow Monarch: negative is money out, positive is money in. Categories are Monarch's as of the last sync. Data from Finicity-linked institutions isn't available through Monarch's MCP.
