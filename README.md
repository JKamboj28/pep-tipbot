# PEP TipBot for Telegram

Commands implemented:
- `/start` — Initialize your account and show available commands
- `/tip amount` — Tip a random active user in the current group
- `/tip @username amount` — Tip a specific user
- `/tip active amount` — Split a tip equally among active users in the last 30 minutes
- `/balance` — Check your balance
- `/deposit` — Get your deposit address
- `/withdraw amount address` — Withdraw PEP (a {WITHDRAW_FEE} PEP fee applies)
- `/faucet` — Request faucet (default {FAUCET_AMOUNT} PEP per {FAUCET_INTERVAL_SECONDS}s)
- `/faucetinfo` — Show faucet deposit address and current on-chain balance
- `/active` — Show a list of users active in the last 30 minutes
- `/help` — Show help message

Notes:
- `/start`, `/balance`, `/deposit`, `/withdraw`, and `/help` are private-only
- `/tip`, `/faucetinfo`, and `/active` are group-only; `/faucet` works in both
- Withdrawals incur a fee (`WITHDRAW_FEE`); deposits require `MIN_CONF` confirmations

## Prereqs

1. **Telegram Bot token** from @BotFather.
2. **PEP node** reachable via JSON‑RPC (typical bitcoind/dogecoind-style). Example daemon args:
   ```
   rpcuser=pepuser
   rpcpassword=peppass
   server=1
   txindex=1
   rpcbind=127.0.0.1
   rpcallowip=127.0.0.1
   # rpcport set by your chain; configure RPC_URL accordingly
   ```
3. **Fund the bot wallet** (and optionally the faucet address) with some PEP.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# copy and edit env
cp .env.example .env

# run
python pep_tipbot.py
```

The bot uses SQLite (`tipbot.db`) and periodically scans your node for
credited deposits (>= `MIN_CONF`) to per-user addresses. Tips are off‑chain
ledger transfers; withdrawals are on‑chain via `sendtoaddress`.

## Environment

See `.env.example` for all configuration, including faucet amount/interval,
confirmation threshold, and fee.

## Security

- This is a custodial wallet. Protect your node and host. Consider separate wallet
  for the bot with limited funds.
- Never log secrets. Rotate `rpcuser/rpcpassword` regularly.
- Rate-limit `/faucet` and withdrawals; this bot includes per‑user faucet cooldown
  and minimum/maximum amounts checks. Add your own AML/KYC if needed for your region.
