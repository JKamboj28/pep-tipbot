import asyncio
import logging
import os
import random
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN, getcontext

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.markdown import hbold
from dotenv import load_dotenv

# High precision for coins
getcontext().prec = 18

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN missing")

RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:22555")
RPC_USER = os.getenv("RPC_USER", "")
RPC_PASSWORD = os.getenv("RPC_PASSWORD", "")

COIN = os.getenv("COIN_SYMBOL", "PEP")
FAUCET_AMOUNT = Decimal(os.getenv("FAUCET_AMOUNT", "50"))
FAUCET_INTERVAL = int(os.getenv("FAUCET_INTERVAL_SECONDS", "7200"))
WITHDRAW_FEE = Decimal(os.getenv("WITHDRAW_FEE", "1.0"))
MIN_CONF = int(os.getenv("MIN_CONF", "5"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
ACTIVE_WINDOW = int(os.getenv("ACTIVE_WINDOW_SECONDS", "1800"))
WALLET_LABEL_PREFIX = os.getenv("WALLET_LABEL_PREFIX", "u_")
FAUCET_LABEL = os.getenv("FAUCET_LABEL", "faucet")
DB_PATH = os.getenv("DB_PATH", "tipbot.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pep_tipbot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Storage (SQLite) --------------------------------------------------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS users (
  tg_id INTEGER PRIMARY KEY,
  username TEXT,
  deposit_address TEXT,
  credited_total TEXT DEFAULT '0',
  balance TEXT DEFAULT '0',
  last_faucet_ts INTEGER DEFAULT 0,
  last_active_ts INTEGER DEFAULT 0,
  created_ts INTEGER DEFAULT 0
);
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS transfers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, -- deposit|tip|withdraw|faucet
  from_tg INTEGER,
  to_tg INTEGER,
  amount TEXT,
  txid TEXT,
  ts INTEGER
);
""")
conn.commit()


def now() -> int:
    return int(time.time())


def db_get_user(tg_id: int):
    cur = conn.execute("SELECT tg_id, username, deposit_address, credited_total, balance, last_faucet_ts, last_active_ts FROM users WHERE tg_id=?",
                       (tg_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "tg_id": row[0], "username": row[1], "deposit_address": row[2],
        "credited_total": Decimal(row[3]), "balance": Decimal(row[4]),
        "last_faucet_ts": row[5], "last_active_ts": row[6]
    }


def db_upsert_user(tg_id: int, username: str):
    u = db_get_user(tg_id)
    if u:
        conn.execute("UPDATE users SET username=?, last_active_ts=? WHERE tg_id=?",
                     (username, now(), tg_id))
    else:
        conn.execute("INSERT INTO users(tg_id, username, created_ts, last_active_ts) VALUES(?,?,?,?)",
                     (tg_id, username, now(), now()))
    conn.commit()


def db_update_balance(tg_id: int, new_balance: Decimal):
    conn.execute("UPDATE users SET balance=? WHERE tg_id=?", (str(new_balance), tg_id))
    conn.commit()


def db_add_transfer(kind: str, from_tg, to_tg, amount: Decimal, txid: str | None):
    conn.execute("INSERT INTO transfers(kind, from_tg, to_tg, amount, txid, ts) VALUES(?,?,?,?,?,?)",
                 (kind, from_tg, to_tg, str(amount), txid, now()))
    conn.commit()


def db_set_deposit_address(tg_id: int, addr: str):
    conn.execute("UPDATE users SET deposit_address=? WHERE tg_id=?", (addr, tg_id))
    conn.commit()


def db_set_credited_total(tg_id: int, total: Decimal):
    conn.execute("UPDATE users SET credited_total=? WHERE tg_id=?", (str(total), tg_id))
    conn.commit()


def db_set_last_faucet(tg_id: int, ts: int):
    conn.execute("UPDATE users SET last_faucet_ts=? WHERE tg_id=?", (ts, tg_id))
    conn.commit()


def db_set_active(tg_id: int):
    conn.execute("UPDATE users SET last_active_ts=? WHERE tg_id=?", (now(), tg_id))
    conn.commit()


def db_get_active_users(chat_member_ids: list[int]) -> list[int]:
    cutoff = now() - ACTIVE_WINDOW
    qmarks = ",".join("?" for _ in chat_member_ids)
    cur = conn.execute(f"SELECT tg_id FROM users WHERE last_active_ts>=? AND tg_id IN ({qmarks})",
                       (cutoff, *chat_member_ids))
    return [row[0] for row in cur.fetchall()]


# --- RPC ---------------------------------------------------------------------
class RPC:
    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.user = user
        self.password = password
        self._id = 0

    def call(self, method: str, params=None):
        if params is None:
            params = []
        self._id += 1
        payload = {"jsonrpc": "1.0", "id": self._id, "method": method, "params": params}
        r = requests.post(self.url, json=payload, auth=(self.user, self.password), timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data["result"]


rpc = RPC(RPC_URL, RPC_USER, RPC_PASSWORD)


def get_or_create_deposit_address(tg_id: int) -> str:
    u = db_get_user(tg_id)
    if u and u["deposit_address"]:
        return u["deposit_address"]
    label = f"{WALLET_LABEL_PREFIX}{tg_id}"
    addr = rpc.call("getnewaddress", [label])
    db_set_deposit_address(tg_id, addr)
    return addr


def query_received_confirmed(addr: str, minconf=MIN_CONF) -> Decimal:
    # Works on bitcoind/dogecoind forks
    val = rpc.call("getreceivedbyaddress", [addr, minconf])
    return Decimal(str(val))


def faucet_address() -> str:
    return rpc.call("getnewaddress", [FAUCET_LABEL])


def faucet_balance_confirmed(minconf=MIN_CONF) -> Decimal:
    # Sum confirmed received to the faucet address label (approximation)
    # If your node supports label balance APIs, replace accordingly.
    addr = faucet_address()
    return query_received_confirmed(addr, minconf)


# --- Bot text ----------------------------------------------------------------
HELP_TEXT = f"""Welcome to the Pepecoin TipBot!

Available commands:
/start - Initialize your account and show available commands

/tip amount - Tip online lucky users
/tip @username amount - Tip users
/tip active amount - Tip active users

/balance - Check your balance

/deposit - Get your deposit address
/withdraw amount address - Withdraw {COIN}

/faucet - Request {FAUCET_AMOUNT} {COIN} per {FAUCET_INTERVAL//3600 if FAUCET_INTERVAL%3600==0 else FAUCET_INTERVAL//60} {'hours' if FAUCET_INTERVAL>=3600 else 'minutes'}
/faucetinfo - Show faucet deposit address and balance
/active - Show a list of users active in the last 30 minutes
/help - Show help message

Notes:
- /start, /balance, /deposit, /withdraw, and /help are private-only
- /tip, /faucetinfo, and /active are group-only; /faucet works in both
- Withdrawals incur a {WITHDRAW_FEE} {COIN} fee
- Deposits require {MIN_CONF} confirmations
"""


def fmt_amt(x: Decimal) -> str:
    q = Decimal("0.00000001")  # 8 dp
    return str(x.quantize(q, rounding=ROUND_DOWN)).rstrip("0").rstrip(".") if "." in str(x) else str(x)


# --- Handlers ----------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    if m.chat.type != ChatType.PRIVATE:
        return  # private-only
    db_upsert_user(m.from_user.id, m.from_user.username or "")
    addr = get_or_create_deposit_address(m.from_user.id)
    await m.answer(HELP_TEXT + f"\nYour deposit address: `{addr}`", parse_mode="Markdown")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    if m.chat.type != ChatType.PRIVATE:
        return
    await m.answer(HELP_TEXT)


@dp.message(Command("deposit"))
async def cmd_deposit(m: Message):
    if m.chat.type != ChatType.PRIVATE:
        return
    db_upsert_user(m.from_user.id, m.from_user.username or "")
    addr = get_or_create_deposit_address(m.from_user.id)
    await m.answer(f"Your deposit address:\n`{addr}`", parse_mode="Markdown")


@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    if m.chat.type != ChatType.PRIVATE:
        return
    db_upsert_user(m.from_user.id, m.from_user.username or "")
    u = db_get_user(m.from_user.id)
    await m.answer(f"Your balance is {fmt_amt(u['balance'])} {COIN}")


@dp.message(Command("withdraw"))
async def cmd_withdraw(m: Message, command: CommandObject):
    if m.chat.type != ChatType.PRIVATE:
        return
    db_upsert_user(m.from_user.id, m.from_user.username or "")
    args = (command.args or "").split()
    if len(args) != 2:
        return await m.answer("Usage: /withdraw amount address")
    try:
        amount = Decimal(args[0])
        if amount <= 0:
            raise ValueError
    except Exception:
        return await m.answer("Invalid amount")
    address = args[1]
    u = db_get_user(m.from_user.id)
    total_cost = amount
    if u["balance"] < total_cost:
        return await m.answer("Insufficient balance")
    send_amount = amount - WITHDRAW_FEE
    if send_amount <= 0:
        return await m.answer(f"Amount must be > fee ({WITHDRAW_FEE} {COIN})")
    try:
        txid = rpc.call("sendtoaddress", [address, float(send_amount)])
    except Exception as e:
        return await m.answer(f"RPC error: {e}")
    new_bal = u["balance"] - total_cost
    db_update_balance(m.from_user.id, new_bal)
    db_add_transfer("withdraw", m.from_user.id, None, amount, txid)
    await m.answer(f"Withdrawal submitted. TXID: `{txid}`\nFee: {WITHDRAW_FEE} {COIN}\nNew balance: {fmt_amt(new_bal)} {COIN}",
                   parse_mode="Markdown")


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def mark_active(m: Message):
    # Track activity
    if m.from_user and not m.from_user.is_bot:
        db_upsert_user(m.from_user.id, m.from_user.username or "")
        db_set_active(m.from_user.id)


def parse_tip_args(s: str):
    parts = s.strip().split()
    # matches: "@username amount" OR "active amount" OR "amount"
    if len(parts) == 1:
        # amount only
        try:
            amt = Decimal(parts[0])
            return {"mode": "lucky", "amount": amt, "username": None}
        except Exception:
            return None
    elif len(parts) == 2:
        target, amount_raw = parts
        try:
            amt = Decimal(amount_raw)
        except Exception:
            return None
        if target.lower() == "active":
            return {"mode": "active", "amount": amt, "username": None}
        if target.startswith("@"):
            return {"mode": "direct", "amount": amt, "username": target[1:]}
    return None


@dp.message(Command("tip"))
async def cmd_tip(m: Message, command: CommandObject):
    if m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await m.answer("Use /tip in a group chat.")
    if not command.args:
        return await m.answer("Usage:\n/tip amount\n/tip @username amount\n/tip active amount")
    args = parse_tip_args(command.args)
    if not args:
        return await m.answer("Invalid arguments")
    sender = db_get_user(m.from_user.id)
    if not sender:
        return await m.answer("Please DM me and /start first.")
    amount = args["amount"]
    if amount <= 0:
        return await m.answer("Amount must be > 0")
    if sender["balance"] < amount:
        return await m.answer("Insufficient balance")
    # Determine recipients
    recipients = []
    if args["mode"] == "direct":
        # find user by username
        uname = args["username"].lower()
        cur = conn.execute("SELECT tg_id, username FROM users")
        for uid, u_name in cur.fetchall():
            if u_name and u_name.lower() == uname:
                recipients = [uid]
                break
        if not recipients:
            return await m.answer("Target user not found or hasn't /start'ed.")
    elif args["mode"] == "active":
        # active users in chat excluding bots and sender
        chat_member_ids = []
        # We can't enumerate all members via API without extra permissions;
        # approximate using the DB + recent activity.
        cur = conn.execute("SELECT tg_id FROM users WHERE last_active_ts>=?", (now() - ACTIVE_WINDOW,))
        chat_member_ids = [row[0] for row in cur.fetchall()]
        recipients = [uid for uid in chat_member_ids if uid != m.from_user.id]
        if not recipients:
            return await m.answer("No active users found.")
    else:  # lucky
        cur = conn.execute("SELECT tg_id FROM users WHERE last_active_ts>=?", (now() - ACTIVE_WINDOW,))
        candidates = [row[0] for row in cur.fetchall() if row[0] != m.from_user.id]
        if not candidates:
            return await m.answer("No active users to tip.")
        recipients = [random.choice(candidates)]
    # Execute tip
    if args["mode"] == "active":
        share = (amount / Decimal(len(recipients))).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        total = share * Decimal(len(recipients))
        if sender["balance"] < total:
            return await m.answer("Insufficient balance for split tip")
        db_update_balance(m.from_user.id, sender["balance"] - total)
        for uid in recipients:
            u = db_get_user(uid)
            db_update_balance(uid, u["balance"] + share)
            db_add_transfer("tip", m.from_user.id, uid, share, None)
        await m.answer(f"Tipped {len(recipients)} active users {fmt_amt(share)} {COIN} each.")
    else:
        uid = recipients[0]
        db_update_balance(m.from_user.id, sender["balance"] - amount)
        u = db_get_user(uid)
        db_update_balance(uid, u["balance"] + amount)
        db_add_transfer("tip", m.from_user.id, uid, amount, None)
        await m.answer(f"Tipped {fmt_amt(amount)} {COIN}.")
    # mark sender active
    db_set_active(m.from_user.id)


@dp.message(Command("active"))
async def cmd_active(m: Message):
    if m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    # show count of active users in last 30 minutes
    cur = conn.execute("SELECT username FROM users WHERE last_active_ts>=? ORDER BY last_active_ts DESC",
                       (now() - ACTIVE_WINDOW,))
    users = [f"@{row[0]}" for row in cur.fetchall() if row[0]]
    if not users:
        return await m.answer("No active users in the last 30 minutes.")
    await m.answer("Active users (last 30 minutes):\n" + ", ".join(users[:50]))


@dp.message(Command("faucetinfo"))
async def cmd_faucetinfo(m: Message):
    if m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    try:
        addr = faucet_address()
        bal = faucet_balance_confirmed(MIN_CONF)
    except Exception as e:
        return await m.answer(f"RPC error: {e}")
    await m.answer(f"Faucet deposit address: `{addr}`\nConfirmed balance (approx): {fmt_amt(bal)} {COIN}",
                   parse_mode="Markdown")


@dp.message(Command("faucet"))
async def cmd_faucet(m: Message):
    db_upsert_user(m.from_user.id, m.from_user.username or "")
    u = db_get_user(m.from_user.id)
    if now() - u["last_faucet_ts"] < FAUCET_INTERVAL:
        wait = FAUCET_INTERVAL - (now() - u["last_faucet_ts"])
        mins = wait // 60
        return await m.answer(f"Faucet available in {mins} minutes.")
    new_bal = u["balance"] + FAUCET_AMOUNT
    db_update_balance(m.from_user.id, new_bal)
    db_set_last_faucet(m.from_user.id, now())
    db_add_transfer("faucet", None, m.from_user.id, FAUCET_AMOUNT, None)
    await m.answer(f"You received {fmt_amt(FAUCET_AMOUNT)} {COIN} from the faucet!\nNext request available in {FAUCET_INTERVAL//3600 if FAUCET_INTERVAL%3600==0 else FAUCET_INTERVAL//60} {'hours' if FAUCET_INTERVAL>=3600 else 'minutes'}.\n\nYour balance is {fmt_amt(new_bal)} {COIN}")


# --- Scanner -----------------------------------------------------------------
async def scanner_loop():
    await asyncio.sleep(3)
    log.info("Deposit scanner started")
    while True:
        try:
            cur = conn.execute("SELECT tg_id, deposit_address, credited_total, balance FROM users WHERE deposit_address IS NOT NULL")
            for tg_id, addr, credited_total, bal in cur.fetchall():
                credited_total = Decimal(credited_total)
                try:
                    total_recv = query_received_confirmed(addr, MIN_CONF)
                except Exception as e:
                    log.warning("RPC getreceivedbyaddress failed: %s", e)
                    continue
                if total_recv > credited_total:
                    diff = total_recv - credited_total
                    # credit to internal balance
                    new_bal = Decimal(bal) + diff
                    db_update_balance(tg_id, new_bal)
                    db_set_credited_total(tg_id, total_recv)
                    db_add_transfer("deposit", None, tg_id, diff, None)
                    try:
                        await bot.send_message(tg_id, f"Deposit confirmed: {fmt_amt(diff)} {COIN}\nNew balance: {fmt_amt(new_bal)} {COIN}")
                    except Exception:
                        pass
        except Exception as e:
            log.error("Scanner error: %s", e)
        await asyncio.sleep(SCAN_INTERVAL)


async def main():
    asyncio.create_task(scanner_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
