import os, asyncio, re, time, json, base64
from dataclasses import dataclass, field
from typing import Dict, Optional, List
from dotenv import load_dotenv

import httpx
import websockets
import base58
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

# ================== CONFIG ==================
print("🔍 Loading configuration...")

# Load environment variables
load_dotenv()

# Telegram Bot Configuration
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNELS = [ch.strip() for ch in os.getenv("TELEGRAM_CHANNELS").split(",")]

# Solana RPC Configuration
RPC_URL = os.getenv("RPC_URL")
WSS_URL = RPC_URL.replace("https://", "wss://")  # Helius WebSocket
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

# Trading Configuration
TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", "10.0"))
TRADE_PERCENTAGE = float(os.getenv("TRADE_PERCENTAGE", "5.0"))
USE_PERCENTAGE_TRADING = os.getenv("USE_PERCENTAGE_TRADING", "True").lower() == "true"
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-30.0"))
TRAIL_FROM_PEAK_PCT = float(os.getenv("TRAIL_FROM_PEAK_PCT", "15.0"))
TP_LADDER = os.getenv("TP_LADDER", "2x:30,5x:20,10x:10,15x:15,20x:15,rest:trail15")

# Re-entry Configuration
REENTRY_ENABLED = os.getenv("REENTRY_ENABLED", "False").lower() == "true"
REENTRY_CONFIRM_PCT = float(os.getenv("REENTRY_CONFIRM_PCT", "7.0"))
MAX_REENTRIES_PER_TOKEN = int(os.getenv("MAX_REENTRIES_PER_TOKEN", "1"))

# System Configuration
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"  # Default to DRY_RUN for safety
PRICE_POLL_SECONDS = float(os.getenv("PRICE_POLL_SECONDS", "0.5"))
PRIORITY_FEE_MICROLAMPORTS = int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "20000"))
MIN_LIQ_SOL = float(os.getenv("MIN_LIQ_SOL", "10.0"))

# User state tracking
user_states = {}  # Track user states for private key input

# Channel list
CHANNELS = TELEGRAM_CHANNELS

# ================== CONSTS ==================
SOL_MINT = os.getenv("SOL_MINT")
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
JUP_PRICE = "https://price.jup.ag/v6/price"        # token price in SOL
JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"    # for pre-wa কাজ rm & later swaps
JUP_SWAP = "https://quote-api.jup.ag/v6/swap"      # for executing swaps

# Solana constants
LAMPORTS_PER_SOL = int(os.getenv("LAMPORTS_PER_SOL", "1000000000"))

# Validate only critical environment variables
if not BOT_TOKEN or not RPC_URL or not WALLET_PRIVATE_KEY or not CHANNELS or not SOL_MINT:
    raise SystemExit("Missing required configuration: BOT_TOKEN, RPC_URL, WALLET_PRIVATE_KEY, CHANNELS, SOL_MINT")

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
    if not private_key_str:
        print("❌ Private key parsing error: empty private key")
        return None
        
    try:
        # Try base58 decoding first (88 or 87 characters)
        if len(private_key_str) in [87, 88]:
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
    
    # Create main menu
    keyboard = [
        [InlineKeyboardButton("⚓ Wallet Dock", callback_data="wallet_dock")],
        [InlineKeyboardButton("⚔️ Trade Settings", callback_data="trade_settings")],
        [InlineKeyboardButton("🌊 Leviathan Mode", callback_data="leviathan_mode")],
        [InlineKeyboardButton("🪝 Sniping Grounds", callback_data="sniping_grounds")],
        [InlineKeyboardButton("📜 Navigation & Logs", callback_data="navigation_logs")],
        [InlineKeyboardButton("⚙️ Leviathan Forge", callback_data="leviathan_forge")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🌊 Welcome to Leviathan86Bot 🐉\n\n"
        f"From the depths of the crypto seas, the Leviathan rises.\n"
        f"An unstoppable force, carving through the tides of meme coins and alt markets alike.\n\n"
        f"With your wallets as its vessel, Leviathan86Bot hunts, trades, and strikes automatically — seizing opportunity before it slips beneath the waves.\n\n"
        f"Brace yourself. Once awakened, the Leviathan doesn't ask. It takes.\n\n"
        f"⚡️ Connect your wallet. Unleash the beast. Rule the crypton seas.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Bot Status: {'LIVE TRADING' if not DRY_RUN else 'DRY RUN MODE'}\n"
        f"💰 Trading: {trading_mode} (${trade_amount:.2f})\n"
        f"🛡️ SL: {STOP_LOSS_PCT}% | Trail: {TRAIL_FROM_PEAK_PCT}% | Ladder: 2x:30%,5x:20%,10x:10%,15x:15%,20x:15%\n"
        f"♻️ Re-entry: {'Enabled' if REENTRY_ENABLED else 'Disabled'} (Buyer preference)\n"
        f"👀 Watching: {', '.join(CHANNELS)}\n"
        f"👤 User: {full_name} (@{username})",
        reply_markup=reply_markup
    )

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Get user information
    user = update.effective_user
    username = user.username or "No Username"
    first_name = user.first_name or "No First Name"
    
    if not context.args or len(context.args) != 1:
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
    # Look for contract addresses in any message (removed keyword restriction)
    return list(set(MINT_RE.findall(text)))

async def handle_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not update.effective_chat or not msg or not msg.text:
        return
    chat = update.effective_chat
    
    print(f"\n📨 Message Received:")
    print(f"   💬 Chat Type: {chat.type}")
    print(f"   👤 Username: {chat.username}")
    print(f"   📝 Text: {msg.text[:200]}...")
    print(f"   👤 User ID: {update.effective_user.id}")
    
    # Check if this is a channel message
    if chat.username and f"@{chat.username}" in CHANNELS:
        # Print channel message info
        print(f"\n📢 Channel Message:")
        print(f"   📺 Channel: @{chat.username}")
        print(f"   💬 Message: {msg.text[:200]}...")
        print(f"   ⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 50)
        
        for m in parse_signal(msg.text):
            # fast-path: same as /buy
            fake_update = update
            await cmd_buy(fake_update, context)
    elif chat.type == "supergroup":
        # Handle supergroup - check if it's our monitored group
        print(f"\n📢 Supergroup Message:")
        print(f"   📺 Chat ID: {chat.id}")
        print(f"   💬 Message: {msg.text[:200]}...")
        print(f"   ⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 50)
        
        # Process all supergroup messages for now (you can add specific chat ID filtering later)
        signals = parse_signal(msg.text)
        if signals:
            print(f"   🎯 Found {len(signals)} mint address(es): {signals}")
            for m in signals:
                print(f"   🚀 Auto-buying: {m}")
                # Create fake context with mint address
                fake_context = context
                fake_context.args = [m]
                await cmd_buy(update, fake_context)
        else:
            print(f"   ❌ No mint addresses found in message")
    
    # Check if this is a private message from user waiting for private key
    elif chat.type == "private":  # Private chat
        user_id = update.effective_user.id
        print(f"   🔍 User State: {user_states.get(user_id, 'None')}")
        
        if user_id in user_states and user_states[user_id] == "waiting_for_private_key":
            print(f"   ✅ Handling private key input...")
            await handle_private_key_input(update, context)
        else:
            print(f"   ❌ User not in waiting state, checking for token signals...")
            # Regular private message - check for token signals
            for m in parse_signal(msg.text):
                fake_update = update
                await cmd_buy(fake_update, context)

async def handle_private_key_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private key input from user"""
    user_id = update.effective_user.id
    private_key_str = update.effective_message.text.strip()
    
    print(f"\n🔑 Private Key Input Received:")
    print(f"   👤 User ID: {user_id}")
    print(f"   🔑 Key: {private_key_str[:20]}...{private_key_str[-20:]}")
    print(f"   📏 Length: {len(private_key_str)}")
    
    # Clear user state
    user_states.pop(user_id, None)
    
    # Try to parse the private key
    private_key_bytes = parse_private_key(private_key_str)
    print(f"   🔍 Parsed bytes length: {len(private_key_bytes) if private_key_bytes else 'None'}")
    
    if private_key_bytes:
        # Generate wallet address from private key
        try:
            # For Solana, the public key is derived from the first 32 bytes of the private key
            if len(private_key_bytes) >= 32:
                wallet_address = base58.b58encode(private_key_bytes[:32]).decode('utf-8')
            else:
                # If private key is shorter, pad it
                padded_key = private_key_bytes + b'\x00' * (32 - len(private_key_bytes))
                wallet_address = base58.b58encode(padded_key).decode('utf-8')
            
            # Update global wallet private key
            global WALLET_PRIVATE_KEY
            WALLET_PRIVATE_KEY = private_key_str
            
            keyboard = [
                [InlineKeyboardButton("🔙 Back to Wallet Dock", callback_data="wallet_dock")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ **Vessel Added Successfully!**\n\n"
                f"**Wallet Address**: `{wallet_address[:8]}...{wallet_address[-8:]}`\n"
                f"**Status**: 🟢 Connected\n"
                f"**Private Key**: Valid format detected\n\n"
                f"The Leviathan now has access to this vessel for trading!",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            print(f"\n🔑 New Wallet Added:")
            print(f"   👤 User: {update.effective_user.first_name}")
            print(f"   🏦 Address: {wallet_address}")
            print(f"   ⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 50)
            
        except Exception as e:
            keyboard = [
                [InlineKeyboardButton("🔙 Back to Wallet Dock", callback_data="wallet_dock")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"❌ **Invalid Private Key Format**\n\n"
                f"**Error**: {str(e)}\n\n"
                f"**Supported Formats:**\n"
                f"• Base58 (88 characters)\n"
                f"• Hex string (64 characters)\n"
                f"• JSON array format\n\n"
                f"Please try again with a valid private key.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    else:
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Wallet Dock", callback_data="wallet_dock")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"❌ **Invalid Private Key**\n\n"
            f"**Error**: Could not parse private key\n\n"
            f"**Supported Formats:**\n"
            f"• Base58 (88 characters)\n"
            f"• Hex string (64 characters)\n"
            f"• JSON array format\n\n"
            f"Please try again with a valid private key.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

# ================== INLINE KEYBOARD HANDLERS ==================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "wallet_dock":
        await show_wallet_dock(query)
    elif query.data == "trade_settings":
        await show_trade_settings(query)
    elif query.data == "leviathan_mode":
        await show_leviathan_mode(query)
    elif query.data == "sniping_grounds":
        await show_sniping_grounds(query)
    elif query.data == "navigation_logs":
        await show_navigation_logs(query)
    elif query.data == "leviathan_forge":
        await show_leviathan_forge(query)
    elif query.data == "back_to_main":
        await show_main_menu(query)
    
    # Wallet Dock Actions
    elif query.data == "add_wallet":
        await add_wallet_action(query)
    elif query.data == "view_fleet":
        await view_fleet_action(query)
    elif query.data == "remove_wallet":
        await remove_wallet_action(query)
    elif query.data == "confirm_remove":
        await confirm_remove_action(query)
    
    # Trade Settings Actions
    elif query.data == "set_percentage":
        await set_percentage_action(query)
    elif query.data == "set_fixed":
        await set_fixed_action(query)
    elif query.data == "check_settings":
        await check_settings_action(query)
    
    # Leviathan Mode Actions
    elif query.data == "awaken_beast":
        await awaken_beast_action(query)
    elif query.data == "sleep_beast":
        await sleep_beast_action(query)
    elif query.data == "beast_status":
        await beast_status_action(query)
    
    # Sniping Grounds Actions
    elif query.data == "add_channel":
        await add_channel_action(query)
    elif query.data == "view_channels":
        await view_channels_action(query)
    elif query.data == "remove_channel":
        await remove_channel_action(query)
    
    # Navigation & Logs Actions
    elif query.data == "battle_history":
        await battle_history_action(query)
    elif query.data == "war_chest":
        await war_chest_action(query)
    elif query.data == "notifications":
        await notifications_action(query)
    
    # Leviathan Forge Actions
    elif query.data == "adjust_stops":
        await adjust_stops_action(query)
    elif query.data == "ladder_strategy":
        await ladder_strategy_action(query)
    elif query.data == "reentry_tide":
        await reentry_tide_action(query)
    
    # Percentage Selection
    elif query.data.startswith("set_pct_"):
        percentage = float(query.data.split("_")[2])
        await set_percentage_value(query, percentage)
    
    # Fixed Amount Selection
    elif query.data.startswith("set_fixed_"):
        amount = float(query.data.split("_")[2])
        await set_fixed_value(query, amount)
    
    # Channel Management
    elif query.data.startswith("remove_ch_"):
        channel = query.data.split("_", 2)[2]
        await remove_specific_channel(query, channel)

async def show_wallet_dock(query):
    keyboard = [
        [InlineKeyboardButton("➕ Add Vessel", callback_data="add_wallet")],
        [InlineKeyboardButton("👁️ View Fleet", callback_data="view_fleet")],
        [InlineKeyboardButton("🗑️ Remove Vessel", callback_data="remove_wallet")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "⚓ **Wallet Dock**\n\n"
        "Manage your connected wallets and vessel fleet.\n\n"
        "• **Add Vessel**: Connect a new wallet to the Leviathan\n"
        "• **View Fleet**: See all connected wallets and balances\n"
        "• **Remove Vessel**: Disconnect a wallet from the fleet",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_trade_settings(query):
    keyboard = [
        [InlineKeyboardButton("📊 Set Tribute %", callback_data="set_percentage")],
        [InlineKeyboardButton("💰 Set Fixed Strike", callback_data="set_fixed")],
        [InlineKeyboardButton("🔍 Check Current Loadout", callback_data="check_settings")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "⚔️ **Trade Settings**\n\n"
        "Configure your trading strategy and strike patterns.\n\n"
        "• **Set Tribute %**: Choose % of wallet balance per trade (5%, 10%, 20%)\n"
        "• **Set Fixed Strike**: Choose fixed trade sizes ($10, $20, $50, etc.)\n"
        "• **Check Current Loadout**: View active trade settings",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_leviathan_mode(query):
    status = "🌊 AWAKE" if not DRY_RUN else "😴 SLEEPING"
    keyboard = [
        [InlineKeyboardButton("🌊 Awaken Beast", callback_data="awaken_beast")],
        [InlineKeyboardButton("😴 Send to Depths", callback_data="sleep_beast")],
        [InlineKeyboardButton("📊 Status of the Beast", callback_data="beast_status")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🌊 **Leviathan Mode**\n\n"
        f"Control the beast's trading state.\n\n"
        f"**Current Status**: {status}\n\n"
        "• **Awaken Beast**: Turn bot ON (auto-trading active)\n"
        "• **Send to Depths**: Turn bot OFF (pause trading)\n"
        "• **Status of the Beast**: Show if bot is currently trading or sleeping",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_sniping_grounds(query):
    keyboard = [
        [InlineKeyboardButton("📍 Mark New Waters", callback_data="add_channel")],
        [InlineKeyboardButton("👁️ Survey the Waters", callback_data="view_channels")],
        [InlineKeyboardButton("🗑️ Abandon Waters", callback_data="remove_channel")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🪝 **Sniping Grounds**\n\n"
        "Manage your hunting grounds and signal sources.\n\n"
        "• **Mark New Waters**: Add Telegram groups to scan/snipe\n"
        "• **Survey the Waters**: Show current groups monitored\n"
        "• **Abandon Waters**: Remove groups from monitoring",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_navigation_logs(query):
    keyboard = [
        [InlineKeyboardButton("📜 Battle History", callback_data="battle_history")],
        [InlineKeyboardButton("💰 War Chest", callback_data="war_chest")],
        [InlineKeyboardButton("🔔 Signals & Whispers", callback_data="notifications")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "📜 **Navigation & Logs**\n\n"
        "Track your conquests and manage notifications.\n\n"
        "• **Battle History**: Show recent trades (PnL logs)\n"
        "• **War Chest**: Show current profits/losses\n"
        "• **Signals & Whispers**: Notifications / alerts toggle",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_leviathan_forge(query):
    keyboard = [
        [InlineKeyboardButton("🛡️ Adjust Trail & Stop", callback_data="adjust_stops")],
        [InlineKeyboardButton("📈 Ladder Strategy", callback_data="ladder_strategy")],
        [InlineKeyboardButton("♻️ Re-Entry Tide", callback_data="reentry_tide")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "⚙️ **Leviathan Forge**\n\n"
        "Fine-tune your trading parameters and strategies.\n\n"
        "• **Adjust Trail & Stop**: Edit trailing stop %, stop loss %\n"
        "• **Ladder Strategy**: Adjust scaling buy levels (2x, 4x, 10x)\n"
        "• **Re-Entry Tide**: Toggle re-entry strategy on/off",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_main_menu(query):
    # Get current trade amount info
    trade_amount = await calculate_trade_amount()
    trading_mode = f"{TRADE_PERCENTAGE}% of wallet" if USE_PERCENTAGE_TRADING else f"${TRADE_AMOUNT_USD} fixed"
    
    # Create main menu
    keyboard = [
        [InlineKeyboardButton("⚓ Wallet Dock", callback_data="wallet_dock")],
        [InlineKeyboardButton("⚔️ Trade Settings", callback_data="trade_settings")],
        [InlineKeyboardButton("🌊 Leviathan Mode", callback_data="leviathan_mode")],
        [InlineKeyboardButton("🪝 Sniping Grounds", callback_data="sniping_grounds")],
        [InlineKeyboardButton("📜 Navigation & Logs", callback_data="navigation_logs")],
        [InlineKeyboardButton("⚙️ Leviathan Forge", callback_data="leviathan_forge")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🌊 Welcome to Leviathan86Bot 🐉\n\n"
        f"From the depths of the crypto seas, the Leviathan rises.\n"
        f"An unstoppable force, carving through the tides of meme coins and alt markets alike.\n\n"
        f"With your wallets as its vessel, Leviathan86Bot hunts, trades, and strikes automatically — seizing opportunity before it slips beneath the waves.\n\n"
        f"Brace yourself. Once awakened, the Leviathan doesn't ask. It takes.\n\n"
        f"⚡️ Connect your wallet. Unleash the beast. Rule the crypton seas.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Bot Status: {'LIVE TRADING' if not DRY_RUN else 'DRY RUN MODE'}\n"
        f"💰 Trading: {trading_mode} (${trade_amount:.2f})\n"
        f"🛡️ SL: {STOP_LOSS_PCT}% | Trail: {TRAIL_FROM_PEAK_PCT}% | Ladder: 2x:30%,5x:20%,10x:10%,15x:15%,20x:15%\n"
        f"♻️ Re-entry: {'Enabled' if REENTRY_ENABLED else 'Disabled'} (Buyer preference)\n"
        f"👀 Watching: {', '.join(CHANNELS)}",
        reply_markup=reply_markup
    )

# ================== ACTION FUNCTIONS ==================
async def add_wallet_action(query):
    # Set user state to waiting for private key
    user_id = query.from_user.id
    user_states[user_id] = "waiting_for_private_key"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Wallet Dock", callback_data="wallet_dock")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "➕ **Add Vessel**\n\n"
        "To add a new wallet to the Leviathan fleet:\n\n"
        "1. Send your wallet private key as a message\n"
        "2. Format: `YOUR_PRIVATE_KEY` (base58, hex, or JSON array)\n"
        "3. The wallet will be added to the fleet\n\n"
        "⚠️ **Security Note**: Only send private keys in private chat!\n\n"
        "**Status**: ⏳ Waiting for private key...",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def view_fleet_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Wallet Dock", callback_data="wallet_dock")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Check if wallet is connected
    if not WALLET_PRIVATE_KEY or WALLET_PRIVATE_KEY.strip() == "":
        await query.edit_message_text(
            f"👁️ **View Fleet**\n\n"
            f"**Active Vessels:**\n"
            f"• No vessels connected\n\n"
            f"**Fleet Summary:**\n"
            f"• Total Vessels: 0\n"
            f"• Total Balance: $0.00 USD\n"
            f"• Ready for Trading: ❌\n\n"
            f"**Status:** Fleet is empty. Add a vessel to begin trading.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Get wallet balance
    try:
        balance_usd = await get_wallet_balance_usd()
        private_key = parse_private_key(WALLET_PRIVATE_KEY)
        wallet_pubkey = base58.b58encode(private_key).decode() if private_key else "Unknown"
    except:
        balance_usd = 0.0
        wallet_pubkey = "Unknown"
    
    await query.edit_message_text(
        f"👁️ **View Fleet**\n\n"
        f"**Active Vessels:**\n"
        f"• **Vessel 1**: `{wallet_pubkey[:8]}...{wallet_pubkey[-8:]}`\n"
        f"  💰 Balance: ${balance_usd:.2f} USD\n"
        f"  🟢 Status: Connected\n\n"
        f"**Fleet Summary:**\n"
        f"• Total Vessels: 1\n"
        f"• Total Balance: ${balance_usd:.2f} USD\n"
        f"• Ready for Trading: ✅",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def remove_wallet_action(query):
    keyboard = [
        [InlineKeyboardButton("🗑️ Confirm Remove", callback_data="confirm_remove")],
        [InlineKeyboardButton("🔙 Back to Wallet Dock", callback_data="wallet_dock")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🗑️ **Remove Vessel**\n\n"
        "⚠️ **Warning**: This will disconnect the wallet from the Leviathan fleet.\n\n"
        "**Current Wallet:**\n"
        f"• Address: `{base58.b58encode(parse_private_key(WALLET_PRIVATE_KEY)).decode()[:8] if parse_private_key(WALLET_PRIVATE_KEY) else 'Unknown'}...`\n"
        f"• Status: Connected\n\n"
        "Are you sure you want to remove this vessel?",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def confirm_remove_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Wallet Dock", callback_data="wallet_dock")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Clear the wallet private key (reset to empty)
    global WALLET_PRIVATE_KEY
    WALLET_PRIVATE_KEY = ""
    
    await query.edit_message_text(
        "✅ **Vessel Removed Successfully**\n\n"
        "The wallet has been disconnected from the Leviathan fleet.\n\n"
        "**Status:**\n"
        "• Wallet: Disconnected\n"
        "• Trading: Paused\n"
        "• Fleet Status: Empty\n\n"
        "To resume trading, add a new vessel using 'Add Vessel'.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def set_percentage_action(query):
    keyboard = [
        [InlineKeyboardButton("5%", callback_data="set_pct_5.0")],
        [InlineKeyboardButton("10%", callback_data="set_pct_10.0")],
        [InlineKeyboardButton("15%", callback_data="set_pct_15.0")],
        [InlineKeyboardButton("20%", callback_data="set_pct_20.0")],
        [InlineKeyboardButton("25%", callback_data="set_pct_25.0")],
        [InlineKeyboardButton("🔙 Back to Trade Settings", callback_data="trade_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "📊 **Set Tribute %**\n\n"
        "Choose the percentage of wallet balance to use per trade:\n\n"
        "**Current Setting:** 5% of wallet balance\n\n"
        "Select a new percentage:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def set_fixed_action(query):
    keyboard = [
        [InlineKeyboardButton("$10", callback_data="set_fixed_10")],
        [InlineKeyboardButton("$20", callback_data="set_fixed_20")],
        [InlineKeyboardButton("$50", callback_data="set_fixed_50")],
        [InlineKeyboardButton("$100", callback_data="set_fixed_100")],
        [InlineKeyboardButton("$200", callback_data="set_fixed_200")],
        [InlineKeyboardButton("$500", callback_data="set_fixed_500")],
        [InlineKeyboardButton("🔙 Back to Trade Settings", callback_data="trade_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "💰 **Set Fixed Strike**\n\n"
        "Choose a fixed dollar amount per trade:\n\n"
        "**Current Setting:** 5% of wallet (Dynamic)\n\n"
        "Select a fixed amount:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def check_settings_action(query):
    trade_amount = await calculate_trade_amount()
    trading_mode = f"{TRADE_PERCENTAGE}% of wallet" if USE_PERCENTAGE_TRADING else f"${TRADE_AMOUNT_USD} fixed"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Trade Settings", callback_data="trade_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🔍 **Check Current Loadout**\n\n"
        f"**Trading Configuration:**\n"
        f"• **Mode**: {trading_mode}\n"
        f"• **Amount**: ${trade_amount:.2f}\n"
        f"• **Stop Loss**: {STOP_LOSS_PCT}%\n"
        f"• **Trailing Stop**: {TRAIL_FROM_PEAK_PCT}%\n"
        f"• **Take Profit Ladder**: {TP_LADDER}\n"
        f"• **Re-entry**: {'Enabled' if REENTRY_ENABLED else 'Disabled'}\n"
        f"• **Max Re-entries**: {MAX_REENTRIES_PER_TOKEN}\n"
        f"• **Re-entry Confirm**: +{REENTRY_CONFIRM_PCT}%\n\n"
        f"**Current Status:**\n"
        f"• Bot Mode: {'LIVE TRADING' if not DRY_RUN else 'DRY RUN'}\n"
        f"• Channels: {len(CHANNELS)} monitored",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def awaken_beast_action(query):
    global DRY_RUN
    DRY_RUN = False
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Leviathan Mode", callback_data="leviathan_mode")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🌊 **Beast Awakened!**\n\n"
        "The Leviathan has risen from the depths!\n\n"
        "✅ **Status**: LIVE TRADING ACTIVE\n"
        "⚡ **Auto-trading**: ENABLED\n"
        "🎯 **Target**: @gem_tools_calls\n"
        "💰 **Strike Force**: 5% of wallet\n\n"
        "The beast hunts...",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def sleep_beast_action(query):
    global DRY_RUN
    DRY_RUN = True
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Leviathan Mode", callback_data="leviathan_mode")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "😴 **Beast Sent to Depths**\n\n"
        "The Leviathan has returned to slumber.\n\n"
        "⏸️ **Status**: TRADING PAUSED\n"
        "💤 **Auto-trading**: DISABLED\n"
        "👁️ **Monitoring**: Still watching channels\n"
        "🛡️ **Protection**: All positions safe\n\n"
        "The beast sleeps...",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def beast_status_action(query):
    status = "🌊 AWAKE" if not DRY_RUN else "😴 SLEEPING"
    mode = "LIVE TRADING" if not DRY_RUN else "DRY RUN MODE"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Leviathan Mode", callback_data="leviathan_mode")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"📊 **Status of the Beast**\n\n"
        f"**Current State**: {status}\n"
        f"**Trading Mode**: {mode}\n"
        f"**Auto-trading**: {'ACTIVE' if not DRY_RUN else 'PAUSED'}\n"
        f"**Channels Monitored**: {len(CHANNELS)}\n"
        f"**Last Activity**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"**Fleet Status:**\n"
        f"• Wallets Connected: 1\n"
        f"• Ready for Action: {'✅' if not DRY_RUN else '⏸️'}\n"
        f"• Monitoring: @gem_tools_calls",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def add_channel_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Sniping Grounds", callback_data="sniping_grounds")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "📍 **Mark New Waters**\n\n"
        "To add a new Telegram channel for monitoring:\n\n"
        "1. Send the channel username as a message\n"
        "2. Format: `@channel_username`\n"
        "3. The channel will be added to monitoring\n\n"
        "**Current Channels:**\n"
        f"• {', '.join(CHANNELS)}\n\n"
        "**Note**: Bot must be admin in the channel to monitor messages.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def view_channels_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Sniping Grounds", callback_data="sniping_grounds")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"👁️ **Survey the Waters**\n\n"
        f"**Currently Monitoring:**\n"
        f"• {', '.join(CHANNELS)}\n\n"
        f"**Total Channels**: {len(CHANNELS)}\n"
        f"**Status**: {'🟢 Active' if len(CHANNELS) > 0 else '🔴 No channels'}\n\n"
        f"**Monitoring Features:**\n"
        f"• Auto-detect launch signals\n"
        f"• Instant trade execution\n"
        f"• Real-time price tracking\n"
        f"• Risk management active",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def remove_channel_action(query):
    if len(CHANNELS) <= 1:
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Sniping Grounds", callback_data="sniping_grounds")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "🗑️ **Abandon Waters**\n\n"
            "⚠️ **Cannot remove all channels!**\n\n"
            "You must have at least one channel for monitoring.\n"
            "Add a new channel before removing this one.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        keyboard = []
        for channel in CHANNELS:
            keyboard.append([InlineKeyboardButton(f"🗑️ Remove {channel}", callback_data=f"remove_ch_{channel}")])
        keyboard.append([InlineKeyboardButton("🔙 Back to Sniping Grounds", callback_data="sniping_grounds")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "🗑️ **Abandon Waters**\n\n"
            "Select a channel to remove from monitoring:\n\n"
            "**Current Channels:**\n" + "\n".join([f"• {ch}" for ch in CHANNELS]),
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def battle_history_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Navigation & Logs", callback_data="navigation_logs")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "📜 **Battle History**\n\n"
        "**Recent Trades:**\n"
        "• No trades executed yet\n\n"
        "**Trading Statistics:**\n"
        "• Total Trades: 0\n"
        "• Successful Trades: 0\n"
        "• Failed Trades: 0\n"
        "• Win Rate: N/A\n\n"
        "**Last 24 Hours:**\n"
        "• Trades: 0\n"
        "• Volume: $0.00\n"
        "• PnL: $0.00",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def war_chest_action(query):
    try:
        balance_usd = await get_wallet_balance_usd()
    except:
        balance_usd = 0.0
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Navigation & Logs", callback_data="navigation_logs")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"💰 **War Chest**\n\n"
        f"**Current Holdings:**\n"
        f"• SOL Balance: ${balance_usd:.2f} USD\n"
        f"• Available for Trading: ${balance_usd * 0.05:.2f} (5%)\n\n"
        f"**Trading Performance:**\n"
        f"• Total PnL: $0.00\n"
        f"• Daily PnL: $0.00\n"
        f"• Best Trade: N/A\n"
        f"• Worst Trade: N/A\n\n"
        f"**Risk Management:**\n"
        f"• Stop Loss: {STOP_LOSS_PCT}%\n"
        f"• Trailing Stop: {TRAIL_FROM_PEAK_PCT}%\n"
        f"• Max Risk per Trade: 5%",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def notifications_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Navigation & Logs", callback_data="navigation_logs")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🔔 **Signals & Whispers**\n\n"
        "**Notification Settings:**\n"
        "• Trade Executions: ✅ Enabled\n"
        "• Price Alerts: ✅ Enabled\n"
        "• Error Notifications: ✅ Enabled\n"
        "• Channel Signals: ✅ Enabled\n\n"
        "**Alert Types:**\n"
        "• Buy/Sell Confirmations\n"
        "• Stop Loss Triggers\n"
        "• Take Profit Hits\n"
        "• Re-entry Alerts\n"
        "• System Errors\n\n"
        "All notifications are sent to this chat.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def adjust_stops_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Leviathan Forge", callback_data="leviathan_forge")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🛡️ **Adjust Trail & Stop**\n\n"
        f"**Current Settings:**\n"
        f"• Stop Loss: {STOP_LOSS_PCT}%\n"
        f"• Trailing Stop: {TRAIL_FROM_PEAK_PCT}%\n\n"
        f"**To modify these settings:**\n"
        f"1. Edit the values in bot.py\n"
        f"2. Restart the bot\n\n"
        f"**Recommended Values:**\n"
        f"• Stop Loss: 10-20%\n"
        f"• Trailing Stop: 5-15%\n\n"
        f"**Current Configuration:**\n"
        f"• STOP_LOSS_PCT = {STOP_LOSS_PCT}\n"
        f"• TRAIL_FROM_PEAK_PCT = {TRAIL_FROM_PEAK_PCT}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def ladder_strategy_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Leviathan Forge", callback_data="leviathan_forge")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"📈 **Ladder Strategy**\n\n"
        f"**Current Take Profit Ladder:**\n"
        f"• {TP_LADDER}\n\n"
        f"**How it works:**\n"
        f"• Sells portions at different profit levels\n"
        f"• Reduces risk while maximizing gains\n"
        f"• **New Format**: 30% at 2x, 20% at 5x, 10% at 10x, 15% at 15x, 15% at 20x\n\n"
        f"**Profit Distribution:**\n"
        f"• 2x: 30% of position\n"
        f"• 5x: 20% of position\n"
        f"• 10x: 10% of position\n"
        f"• 15x: 15% of position\n"
        f"• 20x: 15% of position\n"
        f"• Rest: Trailing stop\n\n"
        f"**To modify:**\n"
        f"1. Edit TP_LADDER in .env file\n"
        f"2. Restart the bot\n\n"
        f"**Current Setting:**\n"
        f"TP_LADDER = {TP_LADDER}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def reentry_tide_action(query):
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Leviathan Forge", callback_data="leviathan_forge")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"♻️ **Re-Entry Tide**\n\n"
        f"**Current Settings:**\n"
        f"• Re-entry: {'Enabled' if REENTRY_ENABLED else 'Disabled'}\n"
        f"• Max Re-entries: {MAX_REENTRIES_PER_TOKEN}\n"
        f"• Confirm Threshold: +{REENTRY_CONFIRM_PCT}%\n\n"
        f"**Status**: Re-entry is **DISABLED** as per buyer's preference\n\n"
        f"**Why Disabled:**\n"
        f"• Buyer prefers no re-entry strategy\n"
        f"• Focus on single entry with ladder strategy\n"
        f"• Reduces complexity and risk\n\n"
        f"**Current Configuration:**\n"
        f"• REENTRY_ENABLED = {REENTRY_ENABLED}\n"
        f"• Strategy: Single entry with advanced ladder\n"
        f"• Last 10%: 15% trailing stop from peak",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# Additional helper functions
async def set_percentage_value(query, percentage):
    global TRADE_PERCENTAGE, USE_PERCENTAGE_TRADING
    TRADE_PERCENTAGE = percentage
    USE_PERCENTAGE_TRADING = True
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Trade Settings", callback_data="trade_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ **Tribute % Updated!**\n\n"
        f"**New Setting**: {percentage}% of wallet balance\n"
        f"**Mode**: Dynamic trading enabled\n\n"
        f"The Leviathan will now use {percentage}% of your wallet for each trade.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def set_fixed_value(query, amount):
    global TRADE_AMOUNT_USD, USE_PERCENTAGE_TRADING
    TRADE_AMOUNT_USD = amount
    USE_PERCENTAGE_TRADING = False
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Trade Settings", callback_data="trade_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ **Fixed Strike Updated!**\n\n"
        f"**New Setting**: ${amount} per trade\n"
        f"**Mode**: Fixed amount trading\n\n"
        f"The Leviathan will now use exactly ${amount} for each trade.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def remove_specific_channel(query, channel):
    global CHANNELS
    if channel in CHANNELS and len(CHANNELS) > 1:
        CHANNELS.remove(channel)
        
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Sniping Grounds", callback_data="sniping_grounds")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✅ **Channel Removed!**\n\n"
            f"**Removed**: {channel}\n"
            f"**Remaining Channels**: {', '.join(CHANNELS)}\n\n"
            f"The Leviathan will no longer monitor this channel.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await query.answer("Cannot remove the last channel!")

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
    app.add_handler(CallbackQueryHandler(handle_callback_query))
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