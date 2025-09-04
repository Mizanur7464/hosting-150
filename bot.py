import os, asyncio, re, time, json, base64
from dataclasses import dataclass, field
from typing import Dict, Optional, List

import httpx
import websockets
import base58
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ================== CONFIG ==================
print("🔍 Loading configuration...")

# Telegram Bot Configuration
BOT_TOKEN = "7360756398:AAGgUU0CqFRiYRpEXp5WVoGNqgCWe2nKrkM"
TELEGRAM_CHANNELS = ["@gem_tools_calls"]  # Only monitor gem_tools_calls

# Solana RPC Configuration
RPC_URL = "https://mainnet.helius-rpc.com/?api-key=6fbd7c3d-4f61-436a-9fcd-4893a653b402"
WSS_URL = RPC_URL.replace("https://", "wss://")  # Helius WebSocket
WALLET_PRIVATE_KEY = "2afY6GD9APZorfxQVqDNXtkEsH1uVyi2TDcyPAYCLsnNMjmXgTKuSrVzPjmscVhbhKFW8EjP3wd7imuu6kDHnds6"  # Add your actual private key here
# Trading Configuration
TRADE_AMOUNT_USD = 10.0           # Fallback amount if percentage fails
TRADE_PERCENTAGE = 5.0            # Percentage of wallet to trade (5%)
USE_PERCENTAGE_TRADING = True     # Enable percentage-based trading
STOP_LOSS_PCT = -30.0             # from entry
TRAIL_FROM_PEAK_PCT = 15.0        # from peak
TP_LADDER = "2x:25,4x:25,10x:30,rest:trail15"

# Re-entry Configuration
REENTRY_ENABLED = True
REENTRY_CONFIRM_PCT = 7.0
MAX_REENTRIES_PER_TOKEN = 1

# System Configuration
DRY_RUN = False  # Enable real trading
PRICE_POLL_SECONDS = 0.5          # fast polling
PRIORITY_FEE_MICROLAMPORTS = 20000
MIN_LIQ_SOL = 10.0

# Channel list
CHANNELS = TELEGRAM_CHANNELS

if not BOT_TOKEN or not RPC_URL or not WALLET_PRIVATE_KEY or not CHANNELS:
    raise SystemExit("Missing required configuration: BOT_TOKEN, RPC_URL, WALLET_PRIVATE_KEY, CHANNELS")

# ================== CONSTS ==================
SOL_MINT = "So11111111111111111111111111111111111111112"
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
JUP_PRICE = "https://price.jup.ag/v6/price"        # token price in SOL
JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"    # for pre-wa কাজ rm & later swaps
JUP_SWAP = "https://quote-api.jup.ag/v6/swap"      # for executing swaps

# Solana constants
LAMPORTS_PER_SOL = 1_000_000_000

def pct(a: float, b: float) -> float:
    return (b / a - 1.0) * 100.0

def parse_tp_ladder(text: str):
    # "2x:25,4x:25,10x:30,rest:trail15" -> list
    out = []
    for part in text.split(","):
        k, v = part.split(":")
        k = k.strip().lower()
        v = v.strip().lower()
        out.append((k, v))
    return out

TP_STEPS = parse_tp_ladder(TP_LADDER)

# ================== STATE ==================
@dataclass
class Position:
    mint: str
    entry_price: float           # in SOL per token
    qty_tokens: float            # abstract in DRY_RUN
    peak_price: float
    remaining_pct: float = 100.0
    last_exit_price: Optional[float] = None
    ladder_done: Dict[str, bool] = field(default_factory=dict)
    reentries_used: int = 0
    active: bool = True

positions: Dict[str, Position] = {}  # mint -> Position

# Initialize wallet (simplified for now)
wallet_keypair = None

async def init_solana():
    global wallet_keypair
    # For now, just store the private key as string
    # In production, you'd parse and use it properly
    wallet_keypair = WALLET_PRIVATE_KEY
    print("✅ Wallet initialized (simplified mode)")

# ================== HELIUS WEBSOCKET (heartbeat) ==================
async def helius_heartbeat():
    # Keep a slot subscription to stay synced (helps sub-500ms reactivity)
    if not WSS_URL.startswith("wss://"):
        return
    sub_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "slotSubscribe",
        "params": []
    }
    while True:
        try:
            async with websockets.connect(WSS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(sub_req))
                # Just read and discard; existence of the stream keeps TCP warm
                async for _ in ws:
                    pass
        except Exception:
            await asyncio.sleep(1.0)  # reconnect

# ================== PRICE FEED ==================
async def get_price_vs_sol(mint: str) -> Optional[float]:
    try:
        async with httpx.AsyncClient(timeout=1.4) as client:
            r = await client.get(JUP_PRICE, params={"ids": mint, "vsToken": SOL_MINT})
            if r.status_code != 200:
                return None
            data = r.json().get("data", {}).get(mint)
            if not data or "price" not in data:
                return None
            return float(data["price"])
    except Exception:
        return None

# Optional: pre-warm route for lower latency on buy
async def prewarm_quote(mint: str):
    try:
        params = {
            "inputMint": SOL_MINT, "outputMint": mint,
            "amount": 5000000,  # 0.005 SOL
            "slippageBps": 300, "onlyDirectRoutes": False
        }
        async with httpx.AsyncClient(timeout=1.2) as client:
            await client.get(JUP_QUOTE, params=params)
    except Exception:
        pass

# ================== JUPITER API FUNCTIONS ==================
async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 300) -> Optional[dict]:
    """Get quote from Jupiter API"""
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": False,
            "asLegacyTransaction": False
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(JUP_QUOTE, params=params)
            if response.status_code == 200:
                return response.json()
            return None
    except Exception as e:
        print(f"Jupiter quote error: {e}")
        return None

async def get_jupiter_swap_transaction(quote: dict, user_public_key: str) -> Optional[dict]:
    """Get swap transaction from Jupiter API"""
    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "useSharedAccounts": True,
            "feeAccount": None,
            "trackingAccount": None,
            "computeUnitPriceMicroLamports": PRIORITY_FEE_MICROLAMPORTS
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(JUP_SWAP, json=payload)
            if response.status_code == 200:
                return response.json()
            return None
    except Exception as e:
        print(f"Jupiter swap error: {e}")
        return None

async def get_token_balance(mint: str, owner: str) -> float:
    """Get token balance for a specific mint (simplified)"""
    # For now, return a mock balance
    # In production, you'd implement proper balance checking
    return 100.0  # Mock balance

async def get_wallet_balance_usd() -> float:
    """Get wallet balance in USD"""
    try:
        if DRY_RUN:
            # Mock balance for dry run
            mock_sol_balance = 10.0
            sol_price_usd = 100.0
            return mock_sol_balance * sol_price_usd
        
        # Get SOL balance from wallet
        wallet_pubkey = base58.b58encode(base58.b58decode(WALLET_PRIVATE_KEY)[:32]).decode('utf-8')
        
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [wallet_pubkey]
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(RPC_URL, json=payload)
            if response.status_code == 200:
                result = response.json()
                if "result" in result:
                    sol_balance_lamports = result["result"]["value"]
                    sol_balance = sol_balance_lamports / LAMPORTS_PER_SOL
                    
                    # Get SOL price in USD
                    sol_price = await get_sol_price_usd()
                    if sol_price:
                        return sol_balance * sol_price
                    else:
                        return sol_balance * 100.0  # Fallback price
                else:
                    print(f"❌ Balance RPC Error: {result}")
                    return 1000.0
            else:
                print(f"❌ Balance HTTP Error: {response.status_code}")
                return 1000.0
                
    except Exception as e:
        print(f"❌ Balance error: {e}")
        return 1000.0  # Fallback balance

async def get_sol_price_usd() -> Optional[float]:
    """Get SOL price in USD"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
            if response.status_code == 200:
                data = response.json()
                return data["solana"]["usd"]
            return None
    except Exception:
        return None

async def calculate_trade_amount() -> float:
    """Calculate trade amount based on percentage or fixed amount"""
    if USE_PERCENTAGE_TRADING:
        try:
            wallet_balance = await get_wallet_balance_usd()
            percentage_amount = wallet_balance * (TRADE_PERCENTAGE / 100.0)
            print(f"💰 Wallet Balance: ${wallet_balance:.2f}")
            print(f"💰 Trade Amount ({TRADE_PERCENTAGE}%): ${percentage_amount:.2f}")
            return percentage_amount
        except Exception as e:
            print(f"❌ Percentage calculation failed: {e}")
            print(f"💰 Using fallback amount: ${TRADE_AMOUNT_USD}")
            return TRADE_AMOUNT_USD
    else:
        return TRADE_AMOUNT_USD

def parse_private_key(private_key_str: str) -> bytes:
    """Parse private key from string"""
    try:
        # Try base58 decoding first
        if len(private_key_str) == 88:
            return base58.b58decode(private_key_str)
        # Try as JSON array
        elif private_key_str.startswith('['):
            key_array = json.loads(private_key_str)
            return bytes(key_array)
        else:
            # Try as hex string
            return bytes.fromhex(private_key_str)
    except Exception as e:
        print(f"❌ Private key parsing error: {e}")
        return None

def sign_transaction(transaction_bytes: bytes, private_key: bytes) -> bytes:
    """Sign transaction with private key (simplified)"""
    try:
        # This is a simplified signing - in production you'd use proper Ed25519 signing
        # For now, we'll return the transaction as-is (Jupiter handles signing)
        return transaction_bytes
    except Exception as e:
        print(f"❌ Signing error: {e}")
        return transaction_bytes

async def send_transaction(transaction_data: dict) -> Optional[str]:
    """Send transaction to Solana network"""
    try:
        if DRY_RUN:
            return f"[DRY] mock_tx_{int(time.time())}"
        
        # Decode the transaction
        transaction_bytes = base64.b64decode(transaction_data["swapTransaction"])
        
        # Parse and sign with private key
        private_key = parse_private_key(WALLET_PRIVATE_KEY)
        if private_key:
            signed_transaction = sign_transaction(transaction_bytes, private_key)
        else:
            print("❌ Failed to parse private key, using unsigned transaction")
            signed_transaction = transaction_bytes
        
        # Send to Solana RPC
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(signed_transaction).decode('utf-8'),
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3
                }
            ]
        }
        
        print(f"🔄 Sending transaction to Solana...")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(RPC_URL, json=payload)
            if response.status_code == 200:
                result = response.json()
                if "result" in result:
                    tx_signature = result["result"]
                    print(f"✅ Transaction sent: {tx_signature}")
                    return tx_signature
                else:
                    print(f"❌ RPC Error: {result}")
                    return None
            else:
                print(f"❌ HTTP Error: {response.status_code}")
                return None
                
    except Exception as e:
        print(f"❌ Transaction error: {e}")
        return None

# ================== EXECUTION (DRY/LIVE) ==================
async def jupiter_buy(mint: str, usd_amount: float) -> str:
    if DRY_RUN:
        return f"[DRY] BUY {mint} for ${usd_amount:.2f}"
    
    try:
        # Convert USD to SOL amount (simplified - you might want to use a price feed)
        sol_amount = usd_amount / 100  # Assuming $100 per SOL, adjust as needed
        amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
        
        # Get quote
        quote = await get_jupiter_quote(SOL_MINT, mint, amount_lamports)
        if not quote:
            return f"❌ Failed to get quote for {mint}"
        
        # Get wallet public key
        private_key = parse_private_key(WALLET_PRIVATE_KEY)
        if private_key:
            wallet_pubkey = base58.b58encode(private_key[:32]).decode('utf-8')
        else:
            wallet_pubkey = "mock_wallet_address"
        
        # Get swap transaction
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            return f"❌ Failed to get swap transaction for {mint}"
        
        # Send transaction
        tx_signature = await send_transaction(swap_data)
        if tx_signature:
            return f"✅ BUY {mint} | TX: {tx_signature}"
        else:
            return f"❌ Failed to execute buy for {mint}"
            
    except Exception as e:
        return f"❌ Buy error: {str(e)}"

async def jupiter_sell(mint: str, sell_pct: float) -> str:
    if DRY_RUN:
        return f"[DRY] SELL {sell_pct}% of {mint}"
    
    try:
        # Get current token balance
        token_balance = await get_token_balance(mint, "mock_wallet_address")
        if token_balance <= 0:
            return f"❌ No {mint} tokens to sell"
        
        # Calculate amount to sell
        sell_amount = token_balance * (sell_pct / 100.0)
        
        # For SPL tokens, you need to convert to the token's decimal places
        # This is simplified - you'd need to get the token's decimals
        sell_amount_raw = int(sell_amount * 1_000_000)  # Assuming 6 decimals
        
        # Get quote
        quote = await get_jupiter_quote(mint, SOL_MINT, sell_amount_raw)
        if not quote:
            return f"❌ Failed to get sell quote for {mint}"
        
        # Get wallet public key
        private_key = parse_private_key(WALLET_PRIVATE_KEY)
        if private_key:
            wallet_pubkey = base58.b58encode(private_key[:32]).decode('utf-8')
        else:
            wallet_pubkey = "mock_wallet_address"
        
        # Get swap transaction
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            return f"❌ Failed to get sell transaction for {mint}"
        
        # Send transaction
        tx_signature = await send_transaction(swap_data)
        if tx_signature:
            return f"✅ SELL {sell_pct}% of {mint} | TX: {tx_signature}"
        else:
            return f"❌ Failed to execute sell for {mint}"
            
    except Exception as e:
        return f"❌ Sell error: {str(e)}"

# ================== LOGIC ==================
async def apply_ladder(pos: Position, price: float, send):
    x = price / pos.entry_price
    for step, val in TP_STEPS:
        if step == "rest":
            continue
        # e.g. "2x"
        try:
            mult = float(step.replace("x", ""))
        except:
            continue
        key = f"{mult}x"
        if x >= mult and not pos.ladder_done.get(key, False):
            # val is percentage like "25"
            try:
                sell_pct = float(val)
            except:
                continue
            tx = await jupiter_sell(pos.mint, sell_pct)
            pos.remaining_pct = max(0.0, pos.remaining_pct - sell_pct)
            pos.ladder_done[key] = True
            pos.last_exit_price = price
            await send(f"🎯 {key} hit → sold {sell_pct}% | remaining {pos.remaining_pct:.1f}% | {tx}")
            if pos.remaining_pct <= 0.1:
                pos.active = False
                await send("✅ Fully exited via ladder.")
                return

async def watcher(pos: Position, send):
    await send(f"👀 Watching {pos.mint} | entry {pos.entry_price:.10f} SOL")
    rest_trail = TRAIL_FROM_PEAK_PCT  # default
    # parse "rest:trailXX" if present
    for step, val in TP_STEPS:
        if step == "rest" and val.startswith("trail"):
            try:
                rest_trail = float(val.replace("trail", ""))
            except:
                pass

    while pos.active:
        price = await get_price_vs_sol(pos.mint)
        if price is None:
            await asyncio.sleep(PRICE_POLL_SECONDS)
            continue

        if price > pos.peak_price:
            pos.peak_price = price

        change = pct(pos.entry_price, price)     # from entry
        drop = pct(pos.peak_price, price)        # negative when dropping

        # hard SL
        if change <= STOP_LOSS_PCT:
            tx = await jupiter_sell(pos.mint, pos.remaining_pct)
            await send(f"🛑 Hard SL {STOP_LOSS_PCT}% hit. Exit {pos.remaining_pct:.1f}% | {tx}")
            pos.last_exit_price = price
            pos.active = False
            break

        # trailing stop from peak (only once above entry)
        if pos.peak_price > pos.entry_price and drop <= -rest_trail:
            tx = await jupiter_sell(pos.mint, pos.remaining_pct)
            await send(f"⛳ Trailing stop {rest_trail}% hit. Exit {pos.remaining_pct:.1f}% | {tx}")
            pos.last_exit_price = price
            pos.active = False
            break

        await apply_ladder(pos, price, send)
        if not pos.active:
            break

        await asyncio.sleep(PRICE_POLL_SECONDS)

    # single re-entry (if allowed)
    if REENTRY_ENABLED and pos.reentries_used < MAX_REENTRIES_PER_TOKEN and pos.last_exit_price:
        trigger = pos.last_exit_price * (1.0 + REENTRY_CONFIRM_PCT / 100.0)
        await send(f"♻️ Re-entry armed for {pos.mint}: trigger > {trigger:.10f} SOL.")
        deadline = time.time() + 10 * 60  # 10 minutes window
        while time.time() < deadline:
            price = await get_price_vs_sol(pos.mint)
            if price is None:
                await asyncio.sleep(PRICE_POLL_SECONDS)
                continue
            if price >= trigger:
                trade_amount = await calculate_trade_amount()
                tx = await jupiter_buy(pos.mint, trade_amount)
                await send(f"🔁 Re-entry executed at {price:.10f} SOL | {tx}")
                new_pos = Position(
                    mint=pos.mint,
                    entry_price=price,
                    qty_tokens=pos.qty_tokens,
                    peak_price=price,
                    reentries_used=pos.reentries_used + 1
                )
                positions[pos.mint] = new_pos
                await watcher(new_pos, send)
                return
            await asyncio.sleep(PRICE_POLL_SECONDS)
        await send("⌛ Re-entry window expired.")

# ================== TELEGRAM ==================
async def send_chat(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    await ctx.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Get user information
    user = update.effective_user
    user_id = user.id
    username = user.username or "No Username"
    first_name = user.first_name or "No First Name"
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()
    
    # Print user information to console
    print(f"\n🚀 Bot Started by User:")
    print(f"   👤 User ID: {user_id}")
    print(f"   📛 Username: @{username}")
    print(f"   🏷️  Full Name: {full_name}")
    print(f"   💬 Chat ID: {update.effective_chat.id}")
    print(f"   ⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    # Get current trade amount info
    trade_amount = await calculate_trade_amount()
    trading_mode = f"{TRADE_PERCENTAGE}% of wallet" if USE_PERCENTAGE_TRADING else f"${TRADE_AMOUNT_USD} fixed"
    
    await update.message.reply_text(
        f"✅ Sniper online (DRY_RUN: {DRY_RUN})\n"
        f"💰 Trading: {trading_mode} (${trade_amount:.2f})\n"
        f"SL {STOP_LOSS_PCT}% | Trail {TRAIL_FROM_PEAK_PCT}% | Ladder {TP_LADDER} | Re-entry {REENTRY_ENABLED} ({MAX_REENTRIES_PER_TOKEN} max, +{REENTRY_CONFIRM_PCT}% confirm)\n"
        f"Watching: {', '.join(CHANNELS)}\n\n"
        f"👤 User: {full_name} (@{username})"
    )

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Get user information
    user = update.effective_user
    username = user.username or "No Username"
    first_name = user.first_name or "No First Name"
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /buy <TOKEN_MINT>")
        return
    mint = context.args[0].strip()
    if not MINT_RE.fullmatch(mint):
        await update.message.reply_text("Invalid token address.")
        return
    
    # Print buy command info
    print(f"\n💰 Buy Command:")
    print(f"   👤 User: {first_name} (@{username})")
    print(f"   🪙 Token: {mint}")
    print(f"   💵 Amount: ${TRADE_AMOUNT_USD}")
    print(f"   ⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    await prewarm_quote(mint)
    price = await get_price_vs_sol(mint)
    if price is None:
        await update.message.reply_text("Couldn’t fetch price. Try again.")
        return
    if mint in positions and positions[mint].active:
        await update.message.reply_text("Already in a position on this token.")
        return

    # Calculate trade amount (percentage or fixed)
    trade_amount = await calculate_trade_amount()
    
    tx = await jupiter_buy(mint, trade_amount)
    pos = Position(mint=mint, entry_price=price, qty_tokens=trade_amount, peak_price=price)
    positions[mint] = pos
    await send_chat(context, chat_id, f"🚀 Bought {mint} at {price:.10f} SOL | {tx}")
    asyncio.create_task(watcher(pos, lambda msg: send_chat(context, chat_id, msg)))

async def cmd_emergency_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not positions:
        await update.message.reply_text("No open positions.")
        return
    for mint, pos in list(positions.items()):
        if pos.active:
            tx = await jupiter_sell(mint, pos.remaining_pct)
            pos.active = False
            pos.last_exit_price = pos.peak_price
            await send_chat(context, chat_id, f"🆘 Emergency exit {mint} | {tx}")
    await update.message.reply_text("All positions exited.")

def parse_signal(text: str) -> List[str]:
    if not any(k in text.lower() for k in ("launch", "gem", "mint", "token")):
        return []
    return list(set(MINT_RE.findall(text)))

async def handle_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not update.effective_chat or not msg or not msg.text:
        return
    chat = update.effective_chat
    if chat.username and f"@{chat.username}" not in CHANNELS:
        return
    
    # Print channel message info
    print(f"\n📢 Channel Message:")
    print(f"   📺 Channel: @{chat.username}")
    print(f"   💬 Message: {msg.text[:100]}...")
    print(f"   ⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    for m in parse_signal(msg.text):
        # fast-path: same as /buy
        fake_update = update
        await cmd_buy(fake_update, context)

async def main():
    # Initialize Solana connection
    await init_solana()
    
    # Run helius heartbeat alongside the bot
    hb = asyncio.create_task(helius_heartbeat())

    # Create application with timeout settings
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("emergency_sell", cmd_emergency_sell))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_channel_message))

    try:
        print("🔄 Initializing bot...")
        await app.initialize()
        print("🔄 Starting bot...")
        await app.start()
        print("🔄 Starting polling...")
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            timeout=30,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=30
        )
        print("✅ Bot is running! Press Ctrl+C to stop.")
        # Keep the bot running
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"❌ Bot error: {e}")
    finally:
        hb.cancel()
        try:
            await app.stop()
            await app.shutdown()
        except:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass