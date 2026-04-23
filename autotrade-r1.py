root@157.15.40.59's password: 
#!/usr/bin/env python3
"""
Automated Multi-Agent Trading System with Teknik Gustafsson
Agent Utama: Bot Polymarket-AgT41 (The Brain)
Sub-Agent 1: Bot Scraper (Right Brain - Sentimen)
Sub-Agent 2: Bot Collateral Engine (Technical Edge - Korelasi)

Teknik Gustafsson Phases:
- Fase Observasi (00-15): LOCK_TRADE - Monitor only
- Fase Golden Window (16-40): Allow execution based on technical & correlation
- Fase Agresif (41-60): Aggressive Momentum Mode

Menu Perintah:
- /cek_saldo_fase  : Cek saldo wallet dan fase jam saat ini
- /stop            : Emergency kill (matikan bot)
- /history         : Cek trade terakhir
"""

import asyncio
import logging
import ccxt
import feedparser
import requests
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from dotenv import load_dotenv
from web3 import Web3

# Telegram notification helper
def send_telegram_report(message: str):
    """Send trade report to Telegram"""
    try:
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        if not bot_token or not chat_id:
            return False
            
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logging.info(f"Telegram report sent successfully to chat {chat_id}")
            return True
        else:
            logging.warning(f"Telegram report failed: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logging.warning(f"Failed to send Telegram report: {e}")
        return False

# Load environment variables
load_dotenv()

# Konfigurasi Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/root/.openclaw/workspace/trading_system.log'),
        logging.StreamHandler()
    ]
)

# Conditional import for Polymarket CLOB real client
try:
    from py_clob_client import ClobClient
    PY_CLOB_CLIENT_AVAILABLE = True
    print("INFO: py-clob-client loaded successfully")
except ImportError as e:
    PY_CLOB_CLIENT_AVAILABLE = False
    print(f"ERROR: py-clob-client import failed: {e}. REAL MODE requires py-clob-client.")
    raise Exception("py-clob-client is required for REAL MODE. Please install it.")

# API Configuration from .env file
API_CONFIG = {
    'binance': {
        'api_key': os.getenv('BINANCE_API_KEY'),
        'api_secret': os.getenv('BINANCE_API_SECRET')
    },
    'polymarket': {
        'api_key': os.getenv('POLYMARKET_API_KEY'),
        'api_secret': os.getenv('POLYMARKET_API_SECRET'),
        'passphrase': os.getenv('POLYMARKET_PASSPHRASE'),
        'private_key': os.getenv('POLYMARKET_PRIVATE_KEY'),
        'wallet_address': os.getenv('POLYMARKET_WALLET_ADDRESS')
    },
    'twitter': {
        'api_key': os.getenv('TWITTER_API_KEY'),
        'api_secret': os.getenv('TWITTER_API_SECRET'),
        'bearer_token': os.getenv('TWITTER_BEARER_TOKEN')
    }
}

# Target Assets (untuk Binance)
TARGET_ASSETS = ['BTC', 'ETH', 'XRP', 'SOL', 'BNB', 'DOGE']

# Gamma Markets (Polymarket) Configuration
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Polymarket Token ID Mapping - DYNAMIC ONLY (no hardcoded IDs)
# Token IDs are fetched fresh from Gamma API setiap 15 menit
# If no valid market found, returns None and skips trade
POLYMARKET_TOKEN_IDS = {}

# Cache timestamp: kapan terakhir kali Gamma API berhasil di-fetch
_GAMMA_LAST_FETCH: Optional[datetime] = None
_GAMMA_CACHE_MINUTES = 15  # Refresh setiap 15 menit

# Token ID validation for REAL MODE
INVALID_TOKEN_IDS = []

def is_valid_token_id(token_id: str) -> bool:
    """Check if token ID is likely valid (not the default invalid ones)."""
    if not token_id:
        return False
    if token_id in INVALID_TOKEN_IDS:
        return False
    # Polymarket token IDs are large integers; basic check
    try:
        int(token_id)
        return True
    except ValueError:
        return False

def check_token_ids_valid() -> bool:
    """Check if any valid token IDs exist in the dynamic mapping.
    Since token IDs are now fetched dynamically each cycle, we assume they will be fetched.
    """
    return True

def _parse_end_date(end_date_str: str) -> Optional[datetime]:
    """Parse end date string dari Gamma API menjadi naive UTC datetime."""
    if not end_date_str:
        return None
    try:
        s = end_date_str
        if s.endswith('Z'):
            s = s[:-1]
        if '+' in s:
            s = s.split('+')[0]
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _extract_token_ids(market: dict) -> tuple:
    """
    Ekstrak token_id YES dan NO dari market Gamma API.

    Gamma API bisa memakai dua format:
    1. Field 'tokens': list of {token_id, outcome}  -> gunakan 'outcome' untuk identifikasi YES/NO
    2. Field 'clobTokenIds': JSON string ["<yes_id>", "<no_id>"]

    Kembalikan (token_yes, token_no) atau (None, None) jika tidak bisa diekstrak.
    """
    tokens = market.get('tokens', [])
    if tokens:
        yes_id = None
        no_id = None
        for t in tokens:
            outcome = t.get('outcome', '').strip().lower()
            tid = str(t.get('token_id', '')).strip()
            if not tid:
                continue
            if outcome == 'yes':
                yes_id = tid
            elif outcome == 'no':
                no_id = tid
        # Fallback: jika 'outcome' kosong, gunakan urutan index
        if not yes_id and not no_id and len(tokens) >= 2:
            yes_id = str(tokens[0].get('token_id', '')).strip() or None
            no_id  = str(tokens[1].get('token_id', '')).strip() or None
        if yes_id and no_id:
            return yes_id, no_id

    # Format 'clobTokenIds'
    clob_str = market.get('clobTokenIds', '')
    if clob_str:
        try:
            ids = json.loads(clob_str)
            if isinstance(ids, list) and len(ids) >= 2:
                return str(ids[0]).strip(), str(ids[1]).strip()
        except (json.JSONDecodeError, TypeError):
            pass

    return None, None


def _is_price_market(title: str, question: str) -> bool:
    """
    Verifikasi bahwa market adalah market harga (price prediction),
    bukan market opini/event seperti 'Will Saylor buy Bitcoin?'.

    Syarat: judul/pertanyaan harus mengandung indikator harga numerik
    seperti "above", "below", "higher", "lower", "over", "under",
    dan mengandung setidaknya satu angka (harga target).
    """
    price_indicators = ['above', 'below', 'higher than', 'lower than',
                        'over', 'under', 'exceed', 'reach', 'hit',
                        'more than', 'less than', 'at least', 'at most']
    text = (title + ' ' + question).lower()
    has_indicator = any(ind in text for ind in price_indicators)
    has_number = any(c.isdigit() for c in text)
    return has_indicator and has_number


def _match_asset(title: str, slug: str) -> Optional[str]:
    """
    Cocokkan market dengan aset kripto berdasarkan kata kunci spesifik.
    Gunakan word-boundary matching untuk mencegah false positive.
    Misalnya: 'sol' tidak boleh cocok dengan 'consolidation'.
    """
    text = ' ' + title.lower() + ' ' + slug.lower() + ' '
    # Urutan pengecekan: spesifik dulu, baru generik
    asset_patterns = {
        'BTC': [' bitcoin ', ' btc ', ' btc-usd ', ' btcusdt '],
        'ETH': [' ethereum ', ' eth ', ' eth-usd ', ' ethusdt '],
        'SOL': [' solana ', ' sol ', ' sol-usd ', ' solusdt '],
        'XRP': [' ripple ', ' xrp ', ' xrp-usd ', ' xrpusdt '],
        'BNB': [' binance coin ', ' bnb ', ' bnb-usd ', ' bnbusdt '],
        'DOGE': [' dogecoin ', ' doge ', ' doge-usd ', ' dogeusdt '],
    }
    for asset, patterns in asset_patterns.items():
        if any(p in text for p in patterns):
            return asset
    return None


def fetch_token_ids_from_gamma(force: bool = False) -> bool:
    """
    Fetch active token ID dari Gamma API untuk aset yang didukung.

    Perubahan dari versi lama:
    - Cache 15 menit: tidak perlu hit API setiap siklus 3 menit
    - Filter market HARGA: hanya ambil market dengan indikator harga + angka target
    - Word-boundary matching: mencegah false positive keyword
    - Identifikasi YES/NO via field 'outcome', bukan asumsi urutan index
    - Window expiry fleksibel: 1–72 jam (ada market daily & weekly)
    - Fallback ke CLOB API jika Gamma tidak menemukan market 1 jam
    """
    global _GAMMA_LAST_FETCH

    now_utc = datetime.utcnow()

    # Gunakan cache jika masih fresh dan tidak di-force
    if not force and _GAMMA_LAST_FETCH is not None:
        elapsed = (now_utc - _GAMMA_LAST_FETCH).total_seconds() / 60
        # Juga skip jika semua market yang ada masih punya > 30 menit sisa
        if elapsed < _GAMMA_CACHE_MINUTES:
            logging.info(f"[Market Discovery] Cache masih valid ({elapsed:.0f}/{_GAMMA_CACHE_MINUTES} menit). Skip fetch.")
            return bool(POLYMARKET_TOKEN_IDS)

    logging.info("[Market Discovery] Fetching markets dari Gamma API...")

    try:
        # Ambil lebih banyak market agar mencakup pasar 1 jam yang jumlahnya sedikit
        url = f"{GAMMA_HOST}/markets?limit=500&active=true&closed=false"
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        markets = response.json()

        if not markets:
            logging.warning("[Market Discovery] Gamma API returned empty list.")
            return False

        logging.info(f"[Market Discovery] Total markets dari API: {len(markets)}")

        best_markets: dict = {}

        for market in markets:
            # Pastikan market masih aktif
            if not market.get('active', True):
                continue
            if market.get('closed', False):
                continue

            question = market.get('question', '')
            title    = market.get('title', question)  # beberapa endpoint pakai 'title'
            slug     = market.get('slug', '')

            if not question and not slug:
                continue

            # --- Filter 1: Hanya market harga (bukan opini/event) ---
            if not _is_price_market(title, question):
                continue

            # --- Filter 2: Cocokkan aset ---
            matched_asset = _match_asset(title, slug)
            if not matched_asset:
                continue

            # --- Filter 3: Window expiry 0.5–72 jam ---
            end_date_str = market.get('endDate', '')
            end_date = _parse_end_date(end_date_str)
            if end_date is None:
                continue

            hours_until = (end_date - now_utc).total_seconds() / 3600

            # Prioritaskan market 1 jam, tapi terima hingga 72 jam jika tidak ada
            if hours_until < 0.33:   # < 20 menit: terlalu dekat, skip
                continue
            if hours_until > 72:     # > 3 hari: terlalu jauh, skip
                continue

            # --- Ekstrak token IDs (YES/NO dengan benar) ---
            token_yes, token_no = _extract_token_ids(market)
            if not token_yes or not token_no:
                continue

            # Validasi format token ID (harus berupa angka besar)
            if not is_valid_token_id(token_yes) or not is_valid_token_id(token_no):
                continue

            # --- Scoring: prioritaskan market 1 jam dan volume tinggi ---
            volume = (
                float(market.get('volume24hr') or 0) or
                float(market.get('volume1wk') or 0) or
                float(market.get('volume') or 0) or 0
            )

            # Skor: market 1 jam dapat bonus besar, sisanya proporsional
            # Semakin kecil hours_until (tapi > 0.33), semakin dekat ke 1 jam yang kita mau
            time_score = max(0, 3.0 - hours_until)  # 1h = 2.0, 2h = 1.0, 3h = 0.0
            score = (time_score * 1000) + volume     # time lebih penting dari volume

            if matched_asset not in best_markets or score > best_markets[matched_asset]['score']:
                best_markets[matched_asset] = {
                    'yes': token_yes,
                    'no': token_no,
                    'hours_until': hours_until,
                    'closes_at': end_date_str,
                    'market_title': question or title,
                    'volume': volume,
                    'score': score,
                }

        # Update global mapping
        POLYMARKET_TOKEN_IDS.clear()
        for asset, info in best_markets.items():
            POLYMARKET_TOKEN_IDS[asset] = {
                'yes': info['yes'],
                'no': info['no'],
                'closes_at': info['closes_at'],
                'market_title': info['market_title'],
                'volume': info['volume'],
            }
            logging.info(
                f"[Market Discovery] {asset}: \"{info['market_title'][:70]}\" "
                f"| expires in {info['hours_until']:.1f}h | vol={info['volume']:.0f} "
                f"| YES={info['yes'][:12]}... NO={info['no'][:12]}..."
            )

        _GAMMA_LAST_FETCH = now_utc

        if not POLYMARKET_TOKEN_IDS:
            logging.warning(
                "[Market Discovery] Tidak ada market harga valid yang ditemukan. "
                "Periksa apakah Polymarket sedang memiliki market 1–72 jam untuk BTC/ETH/SOL/XRP/BNB/DOGE."
            )
            return False

        logging.info(f"[Market Discovery] Berhasil mapping {len(POLYMARKET_TOKEN_IDS)} aset: {list(POLYMARKET_TOKEN_IDS.keys())}")
        return True

    except requests.exceptions.Timeout:
        logging.error("[Market Discovery] Gamma API timeout setelah 20 detik.")
        return False
    except requests.exceptions.HTTPError as e:
        logging.error(f"[Market Discovery] Gamma API HTTP error: {e}")
        return False
    except Exception as e:
        logging.error(f"[Market Discovery] Unexpected error: {e}")
        return False


@dataclass
class PriceData:
    """Data harga untuk analisis"""
    asset: str
    price: float
    timestamp: datetime
    volume: Optional[float] = None


@dataclass
class SignalData:
    """Data sinyal trading"""
    signal_type: str
    target_asset: str
    direction: str
    edge_bonus: float = 0
    urgency: int = 0
    sentiment: str = ""
    source: str = ""


@dataclass
class TradeEntry:
    """Data entry trade history"""
    timestamp: str
    asset: str
    signal_type: str
    direction: str
    edge: float
    bet_size: float
    reason: str
    status: str
    phase: str


class TradeHistory:
    """Manajemen history trade"""

    def __init__(self, filepath: str = "/root/.openclaw/workspace/trade_history.json"):
        self.filepath = filepath
        self.history = self.load_history()

    def load_history(self) -> List[dict]:
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"Error loading history: {e}")
        return []

    def save_history(self):
        try:
            with open(self.filepath, 'w') as f:
                json.dump(self.history, f, indent=2)
        except Exception as e:
            logging.warning(f"Error saving history: {e}")

    def add_trade(self, entry: TradeEntry):
        self.history.append(asdict(entry))
        if len(self.history) > 100:
            self.history = self.history[-100:]
        self.save_history()

    def get_last_trade(self) -> Optional[dict]:
        if self.history:
            return self.history[-1]
        return None

    def get_history_summary(self) -> str:
        if not self.history:
            return "No trades recorded yet."

        summary = f"📊 Trade History (Last {len(self.history)} trades):\n\n"
        for trade in reversed(self.history[-10:]):
            summary += f"[{trade['timestamp']}] {trade['asset']} | {trade['signal_type']} | {trade['direction']} | Edge: {trade['edge']}%\n"
        return summary


class PolymarketClobClient:
    """Client untuk Polymarket CLOB API"""

    def __init__(self, config: dict):
        self.logger = logging.getLogger("PolymarketClobClient")
        self.gamma_host = GAMMA_HOST
        self.clob_host = CLOB_HOST
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        self.api_key = config.get('api_key')
        self.api_secret = config.get('api_secret')
        self.passphrase = config.get('passphrase')
        self.private_key = config.get('private_key')
        self.wallet_address = config.get('wallet_address')
        
        self.client = None
        
        # Initialize real client if py-clob-client is available
        # NOTE: Polymarket CLOB API requires private key authentication for order placement
        if PY_CLOB_CLIENT_AVAILABLE and self.private_key:
            if check_token_ids_valid():
                try:
                    # Initialize client with ONLY private key first
                    self.client = ClobClient(
                        host=self.clob_host,
                        chain_id=137,  # Polygon Mainnet
                        key=self.private_key
                    )
                    
                    # Auto-derive L2 API Credentials
                    try:
                        creds = self.client.create_or_derive_api_creds()
                        self.client.set_api_creds(creds)
                        self.logger.info("✅ L2 API Credentials berhasil di-generate otomatis!")
                    except Exception as e:
                        self.logger.error(f"❌ Gagal generate L2 Creds: {e}")
                    
                    self.logger.info("Polymarket CLOB client initialized (REAL MODE with Private Key)")
                except Exception as e:
                    raise Exception(f"Failed to initialize real CLOB client: {e}")
            else:
                raise Exception("Private key provided but token IDs are invalid. Cannot proceed in REAL MODE.")
        else:
            raise Exception("py-clob-client not available or missing private key. REAL MODE requires private key.")
        
        # Fetch active token IDs from Gamma API
        try:
            fetch_token_ids_from_gamma()
        except Exception as e:
            self.logger.warning(f"Failed to fetch token IDs from Gamma API: {e}")

    def get_balance(self) -> dict:
        """
        Get the actual USDC balance from the Polygon network using Web3.
        Uses the Polymarket wallet address to query the USDC contract.
        """
        try:
            # Initialize Web3 connection to Polygon RPC
            w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))
            
            if not w3.is_connected():
                self.logger.warning("Failed to connect to Polygon RPC")
                return {
                    "status": "ok",
                    "wallet": self.wallet_address,
                    "balance": 0.0,
                    "currency": "USDC (connection failed)"
                }
            
            # USDC contract address on Polygon (native USDC)
            usdc_contract_address = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            
            # ERC-20 ABI for balanceOf function
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "account", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "type": "function"
                }
            ]
            
            # Create contract instance
            contract = w3.eth.contract(address=usdc_contract_address, abi=erc20_abi)
            
            # Query balance
            raw_balance = contract.functions.balanceOf(self.wallet_address).call()
            
            # Convert from Wei (6 decimals for USDC)
            balance = raw_balance / 10**6
            
            self.logger.info(f"[Balance Check] Real Wallet Balance: {balance} USDC")
            
            return {
                "status": "ok",
                "wallet": self.wallet_address,
                "balance": balance,
                "currency": "USDC"
            }
            
        except Exception as e:
            self.logger.warning(f"Balance check failed: {e}")
            return {
                "status": "ok",
                "wallet": self.wallet_address,
                "balance": 0.0,
                "currency": "USDC (error)"
            }

    def place_order(self, market_id: str, side: str, price: float, size: float) -> dict:
        """
        Place a real order on Polymarket CLOB using py-clob-client.
        
        Args:
            market_id: Polymarket market ID (e.g., "sol-usd", "btc-usd")
            side: "buy" or "sell"
            price: Order price in USDC
            size: Order size in conditional tokens
        
        Returns:
            dict: Order result with status and order_id
        """
        try:
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
            from py_clob_client.constants import ZERO_ADDRESS
            
            # Validate inputs
            if price <= 0:
                raise ValueError(f"Invalid price: {price}")
            if size <= 0:
                raise ValueError(f"Invalid size: {size}")
            if side.lower() not in ["buy", "sell"]:
                raise ValueError(f"Invalid side: {side}")
            
            # Convert side to CLOB format (BUY/SELL)
            order_side = "BUY" if side.lower() == "buy" else "SELL"
            
            # Convert market_id to token_id format
            # Token ID Polymarket sudah berupa angka/ID, jangan diubah
            token_id = market_id
            
            # Create OrderArgs with all required fields
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=order_side,
                fee_rate_bps=0,  # Will be auto-resolved by py-clob-client
                nonce=0,  # Auto-generated by py-clob-client
                expiration=0,  # No expiration
                taker=ZERO_ADDRESS,  # Public order (anyone can take)
            )
            
            # Optional: Create options for additional configuration
            # options = PartialCreateOrderOptions(
            #     tick_size=None,  # Auto-detect from market
            #     neg_risk=None,   # Auto-detect from market
            # )
            
            # Place real order using py-clob-client
            # This will:
            # 1. Validate price against tick size
            # 2. Resolve neg_risk for the market
            # 3. Calculate fee rate
            # 4. Sign the order with private key
            # 5. Submit to Polymarket CLOB API
            self.logger.info(f"Preparing order: {order_args}")
            try:
                # Auto-detect neg_risk
                options = PartialCreateOrderOptions(neg_risk=None)
                order_result = self.client.create_order(order_args, options)
                
                # Extract order_id from result (handle both dict and object)
                order_id = None
                if isinstance(order_result, dict):
                    order_id = order_result.get('order_id') or order_result.get('id')
                elif hasattr(order_result, 'order_id'):
                    order_id = order_result.order_id
                elif hasattr(order_result, 'id'):
                    order_id = order_result.id
                
                if not order_id:
                    order_id = str(order_result)
                
                # Log success
                self.logger.info(f"Real order placed: {token_id} {side} ${price} size {size}")
                self.logger.info(f"Order ID: {order_id}")
                self.logger.info(f"Order result: {order_result}")
                
                return {
                    "status": "ok",
                    "order_id": order_id,
                    "result": order_result,
                    "token_id": token_id,
                    "side": order_side,
                    "price": price,
                    "size": size,
                }
            except Exception as e:
                # Check if it's a 'market not found' error (likely invalid token_id)
                error_msg = str(e)
                self.logger.error(f"API ERROR placing order: {e}")
                print(f"API ERROR: {e}")
                if "market not found" in error_msg or "404" in error_msg:
                    self.logger.error(f"Market not found for token_id: {token_id}. Ensure token_id matches Polymarket market slug.")
                    return {
                        "status": "error",
                        "error": f"Market not found: {token_id}",
                        "market_id": market_id,
                        "side": side,
                        "price": price,
                        "size": size,
                    }
                
                # Re-raise other errors
                raise
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return {
                "status": "error",
                "error": str(e),
                "market_id": market_id,
                "side": side,
                "price": price,
                "size": size,
            }


class BotCollateralEngine:
    """Sub-Agent 2: Bot Collateral Engine - Korelasi"""

    def __init__(self):
        self.logger = logging.getLogger("CollateralEngine")
        self.binance = ccxt.binance({
            'apiKey': API_CONFIG['binance']['api_key'],
            'secret': API_CONFIG['binance']['api_secret'],
            'enableRateLimit': True
        })
        self.price_history = {}
        self.logger.info("Bot Collateral Engine initialized")

    async def get_price_data(self, asset: str) -> Optional[PriceData]:
        try:
            symbol = f"{asset.upper()}USDT"
            # Try direct HTTP request first (more reliable)
            try:
                url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
                response = await asyncio.to_thread(requests.get, url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    price = float(data['price'])
                    # Get volume from 24hr ticker
                    url2 = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
                    resp2 = await asyncio.to_thread(requests.get, url2, timeout=10)
                    volume = 0.0
                    if resp2.status_code == 200:
                        data2 = resp2.json()
                        volume = float(data2.get('baseVolume', 0))
                    return PriceData(
                        asset=asset,
                        price=price,
                        timestamp=datetime.now(),
                        volume=volume
                    )
            except Exception as e:
                self.logger.warning(f"Direct Binance API failed for {asset}: {e}")
            
            # Fallback to ccxt
            symbol_ccxt = f"{asset}/USDT"
            ticker = await asyncio.wait_for(
                asyncio.to_thread(self.binance.fetch_ticker, symbol_ccxt),
                timeout=30.0
            )
            return PriceData(
                asset=asset,
                price=ticker['last'],
                timestamp=datetime.now(),
                volume=ticker['baseVolume']
            )
        except asyncio.TimeoutError:
            self.logger.warning(f"Price data fetch timeout for {asset}")
            return None
        except Exception as e:
            self.logger.warning(f"Price data unavailable for {asset}: {e}")
            return None

    async def calculate_correlation_strike(self) -> Optional[SignalData]:
        try:
            try:
                btc_price = await asyncio.wait_for(self.get_price_data('BTC'), timeout=30.0)
            except asyncio.TimeoutError:
                return None

            if not btc_price:
                return None

            if 'BTC' not in self.price_history:
                self.price_history['BTC'] = []
            self.price_history['BTC'].append(btc_price)

            five_min_ago = datetime.now() - timedelta(minutes=5)
            self.price_history['BTC'] = [
                p for p in self.price_history['BTC']
                if p.timestamp > five_min_ago
            ]

            if len(self.price_history['BTC']) < 2:
                return None

            oldest_btc = self.price_history['BTC'][0]
            btc_delta = ((btc_price.price - oldest_btc.price) / oldest_btc.price) * 100

            for asset in TARGET_ASSETS:
                if asset == 'BTC':
                    continue

                try:
                    asset_price = await asyncio.wait_for(self.get_price_data(asset), timeout=10.0)
                except asyncio.TimeoutError:
                    continue

                if not asset_price:
                    continue

                if asset not in self.price_history:
                    self.price_history[asset] = []
                self.price_history[asset].append(asset_price)

                self.price_history[asset] = [
                    p for p in self.price_history[asset]
                    if p.timestamp > five_min_ago
                ]

                if len(self.price_history[asset]) < 2:
                    continue

                oldest_asset = self.price_history[asset][0]
                asset_delta = ((asset_price.price - oldest_asset.price) / oldest_asset.price) * 100

                if btc_delta > 1.0 and asset_delta < 0.2:
                    return SignalData(
                        signal_type="POSITIVE_LAG_DETECTED",
                        target_asset=asset,
                        direction="FOLLOW_BTC",
                        edge_bonus=15,
                        source="CollateralEngine"
                    )

                if btc_delta < -1.0 and asset_delta > -0.2:
                    return SignalData(
                        signal_type="POSITIVE_LAG_DETECTED",
                        target_asset=asset,
                        direction="FOLLOW_BTC",
                        edge_bonus=15,
                        source="CollateralEngine"
                    )

            return None

        except Exception as e:
            self.logger.warning(f"Correlation strike skipped: {e}")
            return None


class BotScraper:
    """Sub-Agent 1: Bot Scraper - Sentimen"""

    def __init__(self):
        self.logger = logging.getLogger("Scraper")
        self.twitter_accounts = [
            'Tree_of_Alpha', 'Tier10k', 'unfolded', 'WatcherGuru', 'saylor',
            'BitcoinMagazine', 'VitalikButerin', 'sassal0x', 'aeyakovenko',
            'solana', 'bgarlinghouse', 'EleanorTerrett', 'whale_alert',
            'lookonchain', 'cz_binance', 'BNBCHAIN', 'RichardTeng', 'WuBlockchain',
            'BillyM2k', 'dogecoin', 'elonmusk', 'chameleon_jeff', 'HyperliquidX',
            'mishaboar', 'Hedge_Fund_7'
        ]
        self.rss_feeds = [
            'https://www.theblock.co/rss',
            'https://www.coindesk.com/rss',
            'https://cryptobriefing.com/rss',
            'https://cointelegraph.com/rss'
        ]
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        self.twitter_client = None
        bearer_token = API_CONFIG['twitter']['bearer_token']
        if bearer_token and bearer_token not in ['YOUR_TWITTER_BEARER_TOKEN', '']:
            try:
                import tweepy
                self.twitter_client = tweepy.Client(bearer_token=bearer_token)
                self.logger.info("Twitter API v2 client initialized")
            except ImportError:
                self.logger.warning("tweepy not installed")
            except Exception as e:
                self.logger.warning(f"Twitter init failed: {e}")
        self.logger.info("Bot Scraper initialized")

    async def scan_rss_feeds(self) -> List[SignalData]:
        signals = []
        for feed_url in self.rss_feeds:
            try:
                response = await asyncio.to_thread(
                    requests.get,
                    feed_url,
                    headers=self.headers,
                    timeout=10
                )
                if response.status_code != 200:
                    continue
                feed = feedparser.parse(response.content)
                for entry in feed.entries[:3]:
                    title = entry.get('title', '').lower()
                    
                    # Keywords untuk menentukan arah
                    negative_keywords = ['hack', 'crash', 'ban', 'sec', 'fraud', 'sell', 'dump', 'fear', 'scam', 'leak', 'ban', 'down']
                    positive_keywords = ['approve', 'etf', 'buy', 'surge', 'moon', 'partnership', 'launch', 'up', 'bull', 'gain', 'profit']
                    
                    # Cek apakah ada kata kunci negatif
                    has_negative = any(kw in title for kw in negative_keywords)
                    has_positive = any(kw in title for kw in positive_keywords)
                    
                    if has_negative and not has_positive:
                        direction = "BUY_DOWN"
                        sentiment = "NEGATIVE"
                        urgency = 7
                        asset = "BTC"  # Default asset untuk berita negatif
                    elif has_positive and not has_negative:
                        direction = "BUY_UP"
                        sentiment = "POSITIVE"
                        urgency = 5
                        asset = "BTC"  # Default asset untuk berita positif
                    else:
                        # Tidak ada kata kunci jelas, skip
                        continue
                    
                    signals.append(SignalData(
                        signal_type="NEWS",
                        target_asset=asset,
                        direction=direction,
                        urgency=urgency,
                        sentiment=sentiment,
                        source="RSS"
                    ))
            except Exception as e:
                self.logger.warning(f"Skip RSS {feed_url}: {e}")
        return signals

    async def scan_twitter(self) -> List[SignalData]:
        signals = []
        if not self.twitter_client:
            return signals
        # Placeholder - implementasi lengkap membutuhkan error handling lebih lanjut
        return signals

    async def get_signals(self) -> List[SignalData]:
        signals = []
        rss_signals = await self.scan_rss_feeds()
        signals.extend(rss_signals)
        twitter_signals = await self.scan_twitter()
        signals.extend(twitter_signals)
        return signals


class BotPolymarket:
    """Agent Utama: Bot Polymarket-AgT41 (The Brain)"""

    def __init__(self):
        self.logger = logging.getLogger("Polymarket")
        self.scraper = BotScraper()
        self.collateral_engine = BotCollateralEngine()
        self.trade_history = TradeHistory()
        self.price_data = {}
        self.rsi_history = {}
        self.daily_loss = 0
        self.max_daily_loss = 8
        self.stop_requested = False
        
        # Cooldown tracking: prevent overtrading (40 minutes per asset)
        self.last_traded_time = {}  # asset -> timestamp

        poly_config = API_CONFIG['polymarket']
        private_key = poly_config.get('private_key')
        wallet_address = poly_config.get('wallet_address')
        
        # Check if we have enough credentials for real mode
        has_creds = (
            (poly_config.get('api_key') and poly_config.get('api_secret')) or
            (private_key and wallet_address)
        )
        
        if has_creds:
            self.clob_client = PolymarketClobClient(poly_config)
            self.wallet_address = wallet_address
            self.logger.info("Polymarket CLOB client initialized (REAL MODE)")
        else:
            self.clob_client = None
            self.wallet_address = None
            raise Exception("No Polymarket credentials configured. REAL MODE requires private key.")

        self.logger.info("Bot Polymarket initialized")

    def get_current_phase(self) -> tuple:
        """Get current phase based on minute of hour (Teknik Gustafsson)."""
        minute = datetime.now().minute
        if minute <= 15:
            return ("OBSERVASI", "LOCK_TRADE")
        elif minute <= 40:
            return ("GOLDEN_WINDOW", "ALLOW_TRADE")
        else:
            return ("AGRESIF", "AGGRESSIVE_MOMENTUM")

    async def check_balance(self):
        print("\n" + "="*60 + "\nPERIKSA SALDO ASLI\n" + "="*60)

        # Daftar RPC Publik Polygon (rotasi)
        rpc_urls = [
            'https://1rpc.io/matic',
            'https://polygon.llamarpc.com'
        ]

        w3 = None
        for url in rpc_urls:
            try:
                temp_w3 = Web3(Web3.HTTPProvider(url))
                if temp_w3.is_connected():
                    w3 = temp_w3
                    print(f"[Web3] Berhasil terhubung ke RPC: {url}")
                    break
            except:
                continue

        if not w3:
            print("[ERROR] Semua koneksi RPC Polygon publik gagal.")
            return

        try:
            # Alamat Dompet Target
            target_wallet = w3.to_checksum_address("0xe6A9bE1740d29F59D6e00C9a08Ee8eacBC299D0b")
            print(f"Memeriksa Alamat: {target_wallet}")

            # Kontrak USDC
            native_usdc = w3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
            bridged_usdc = w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

            abi = [{"constant": True,"inputs": [{"name": "account","type": "address"}],"name": "balanceOf","outputs": [{"name": "","type": "uint256"}],"type": "function"}]

            bal_n = w3.eth.contract(address=native_usdc, abi=abi).functions.balanceOf(target_wallet).call() / 10**6
            bal_b = w3.eth.contract(address=bridged_usdc, abi=abi).functions.balanceOf(target_wallet).call() / 10**6

            print(f"Native USDC : {bal_n}")
            print(f"USDC.e      : {bal_b}")
            print(f"Total       : {bal_n + bal_b} USDC")
            self.logger.info(f"Saldo Terdeteksi: Native={bal_n}, USDC.e={bal_b}, Total={bal_n + bal_b} USDC")
        except Exception as e:
            print(f"Gagal cek saldo: {e}")

    async def get_price(self, asset: str) -> Optional[PriceData]:
        try:
            return await asyncio.wait_for(self.collateral_engine.get_price_data(asset), timeout=30.0)
        except asyncio.TimeoutError:
            self.logger.warning(f"Price fetch timeout for {asset}")
            return None

    async def get_midpoint_price(self, token_id: str) -> Optional[float]:
        """Get midpoint price from Polymarket CLOB API for a token."""
        try:
            url = f"{CLOB_HOST}/midpoint?token_id={token_id}"
            response = await asyncio.to_thread(requests.get, url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                midpoint = float(data.get('mid', 0))
                return midpoint
            else:
                self.logger.warning(f"Midpoint API error for {token_id}: {response.status_code}")
                return None
        except Exception as e:
            self.logger.warning(f"Failed to get midpoint for {token_id}: {e}")
            return None

    async def calculate_rsi(self, asset: str, period: int = 14) -> Optional[float]:
        try:
            exchange = ccxt.binance()
            symbol = f"{asset}/USDT"
            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '1m', limit=period + 1)
            if len(ohlcv) < period + 1:
                return None
            closes = [c[4] for c in ohlcv]
            gains, losses = [], []
            for i in range(1, len(closes)):
                change = closes[i] - closes[i - 1]
                if change > 0:
                    gains.append(change)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(abs(change))
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                return 100
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            return rsi
        except Exception as e:
            self.logger.warning(f"RSI unavailable for {asset}: {e}")
            return None

    async def calculate_ema(self, asset: str, period: int = 50) -> Optional[float]:
        try:
            exchange = ccxt.binance()
            symbol = f"{asset}/USDT"
            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '1m', limit=period)
            if len(ohlcv) < period:
                return None
            closes = [c[4] for c in ohlcv]
            multiplier = 2 / (period + 1)
            ema = closes[0]
            for close in closes[1:]:
                ema = (close * multiplier) + (ema * (1 - multiplier))
            return ema
        except Exception as e:
            self.logger.warning(f"EMA unavailable for {asset}: {e}")
            return None

    async def _calculate_ema_slope(self, asset: str) -> Optional[float]:
        try:
            exchange = ccxt.binance()
            symbol = f"{asset}/USDT"
            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '1m', limit=100)
            if len(ohlcv) < 100:
                return None
            closes = [c[4] for c in ohlcv]
            ema_values = []
            multiplier = 2 / 51
            ema = closes[0]
            for close in closes[1:]:
                ema = (close * multiplier) + (ema * (1 - multiplier))
                ema_values.append(ema)
            if len(ema_values) >= 10:
                slope = (ema_values[-1] - ema_values[-10]) / ema_values[-10]
                return slope
            return None
        except Exception as e:
            self.logger.warning(f"Error calculating EMA slope for {asset}: {e}")
            return None

    async def _rsi_was_below_30(self, asset: str, current_rsi: float) -> bool:
        if asset not in self.rsi_history:
            self.rsi_history[asset] = []
        self.rsi_history[asset].append(current_rsi)
        if len(self.rsi_history[asset]) > 5:
            self.rsi_history[asset].pop(0)
        return any(rsi < 30 for rsi in self.rsi_history[asset])

    async def _rsi_was_above_75(self, asset: str, current_rsi: float) -> bool:
        if asset not in self.rsi_history:
            self.rsi_history[asset] = []
        self.rsi_history[asset].append(current_rsi)
        if len(self.rsi_history[asset]) > 5:
            self.rsi_history[asset].pop(0)
        return any(rsi > 75 for rsi in self.rsi_history[asset])

    async def check_technical_signal(self, asset: str) -> Optional[str]:
        try:
            price_data = await self.get_price(asset)
            if not price_data:
                return None
            price = price_data.price
            rsi = await self.calculate_rsi(asset)
            ema = await self.calculate_ema(asset, 50)
            if rsi is None or ema is None:
                return None
            price_ema_diff = abs((price - ema) / ema) * 100
            if price_ema_diff < 0.05:
                return None
            if rsi < 25:
                return "BUY_UP_INSTAN"
            if rsi > 75:
                return "BUY_DOWN_INSTAN"
            ema_slope = await self._calculate_ema_slope(asset)
            if ema_slope is None:
                return None
            if rsi < 35 and price > ema and ema_slope > 0:
                return "BUY_UP_TREND"
            if rsi > 65 and price < ema and ema_slope < 0:
                return "BUY_DOWN_TREND"
            if rsi < 30 and await self._rsi_was_below_30(asset, rsi):
                return "BUY_UP_RECOVERY"
            if rsi > 75 and await self._rsi_was_above_75(asset, rsi):
                return "BUY_DOWN_RECOVERY"
            return None
        except Exception as e:
            self.logger.warning(f"Error checking technical signal for {asset}: {e}")
            return None

    async def process_signals(self, asset: str, phase: str):
        try:
            print(f"[SCAN] Processing {asset}...")
            # Add timeout to prevent hanging on network issues
            try:
                price_data = await asyncio.wait_for(self.get_price(asset), timeout=30.0)
            except asyncio.TimeoutError:
                print(f"[TIMEOUT] {asset} price fetch timed out")
                return
            
            if price_data:
                print(f"  Price: ${price_data.price:.6f} (Volume: {price_data.volume:.2f})")
            else:
                print(f"  Price: unavailable")

            tech_signal = await self.check_technical_signal(asset)
            scraper_signals = await self.scraper.get_signals()
            scraper_signal = None
            for signal in scraper_signals:
                if signal.target_asset == asset:
                    scraper_signal = signal
                    break
            collateral_signal = await self.collateral_engine.calculate_correlation_strike()

            final_signal = None
            reason = ""

            if scraper_signal and (scraper_signal.urgency > 8 or scraper_signal.signal_type == "NEWS_OVERRIDE"):
                final_signal = scraper_signal
                reason = f"News Override - {scraper_signal.source}: {scraper_signal.sentiment}"
                self.logger.info(f"NEWS OVERRIDE for {asset}: {scraper_signal.sentiment}")
            elif collateral_signal and collateral_signal.target_asset == asset:
                veto = False
                for sig in scraper_signals:
                    if sig.target_asset == asset and sig.sentiment == "NEGATIVE":
                        veto = True
                        reason = "SKIPPED: Veto dari Scraper (Negative sentiment)"
                        break
                if not veto:
                    final_signal = collateral_signal
                    reason = f"Correlation Strike - Edge bonus: {collateral_signal.edge_bonus}%"
                    self.logger.info(f"CORRELATION STRIKE for {asset}")
            elif tech_signal:
                final_signal = SignalData(
                    signal_type=tech_signal,
                    target_asset=asset,
                    direction="BUY_UP" if "UP" in tech_signal else "BUY_DOWN",
                    source="Technical Analysis"
                )
                reason = f"Technical Signal: {tech_signal}"
                self.logger.info(f"TECHNICAL SIGNAL for {asset}: {tech_signal}")

            if final_signal:
                await self.execute_trade(asset, final_signal, reason, phase)
            else:
                self.logger.info(f"No signal for {asset}")
        except Exception as e:
            self.logger.error(f"Error processing signals for {asset}: {e}")
            print(f"[ERROR] Failed to process {asset}: {e}")

    async def execute_trade(self, asset: str, signal: SignalData, reason: str, phase: str):
        try:
            if phase == "OBSERVASI":
                print(f"\n{'='*60}")
                print(f"🔒 FASE OBSERVASI - LOCK_TRADE")
                print(f"Asset: {asset} | Sinyal: {signal.signal_type}")
                print(f"Alasan: {reason}")
                print(f"Status: TIDAK EKSEKUSI (Locked)")
                print(f"{'='*60}\n")
                self.logger.info(f"LOCKED: {asset} - Fase Observasi")
                return

            # Cooldown check: prevent overtrading (40 minutes per asset)
            current_time = time.time()
            if asset in self.last_traded_time:
                time_diff = current_time - self.last_traded_time[asset]
                if time_diff < 2400:  # 40 minutes = 2400 seconds
                    self.logger.warning(f"[Risk Management] Eksekusi {asset} dilewati. Masih dalam masa cooldown 1 jam.")
                    return
            
            # Update last traded time
            self.last_traded_time[asset] = current_time

            edge = signal.edge_bonus if signal.edge_bonus > 0 else 10
            if phase == "AGRESIF":
                edge += 5
                print(f"⚡ AGGRESSIVE MODE: Edge +5% = {edge}%")

            if edge >= 10 and edge < 18:
                bet_size = 1.0
            elif edge >= 18 and edge < 25:
                bet_size = 1.5
            else:
                bet_size = 2.0

            price_data = await self.get_price(asset)
            # Note: price_data.price is Binance price in USDT (e.g., $100,000 for BTC)
            # The previous check "price > 0.40" was incorrect for Binance prices.
            # We skip this check for real trading.

            if self.daily_loss >= self.max_daily_loss:
                self.logger.warning("EMERGENCY BRAKE: Daily loss limit reached")
                return

            # Convert direction format
            direction_display = signal.direction.replace('BUY_', '')
            


            trade_entry = TradeEntry(
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                asset=asset,
                signal_type=signal.signal_type,
                direction=signal.direction,
                edge=edge,
                bet_size=bet_size,
                reason=reason,
                status="AUTO-EXECUTED",
                phase=phase
            )
            self.trade_history.add_trade(trade_entry)

            if self.clob_client:
                try:
                    # Determine Polymarket token ID based on asset and direction
                    # BUY_UP / FOLLOW_BTC -> Buy "Yes" token (price goes up)
                    # BUY_DOWN -> Buy "No" token (price goes down)
                    token_type = 'yes' if signal.direction in ["BUY_UP", "FOLLOW_BTC"] else 'no'
                    
                    # Get token ID from mapping
                    token_id = None
                    closes_at = None
                    if asset in POLYMARKET_TOKEN_IDS and token_type in POLYMARKET_TOKEN_IDS[asset]:
                        token_id = POLYMARKET_TOKEN_IDS[asset][token_type]
                        closes_at = POLYMARKET_TOKEN_IDS[asset].get('closes_at')
                    else:
                        # No token ID mapping available - skip trade execution for this asset
                        print(f"[Market Discovery] Tidak ada market jangka pendek untuk {asset}. Skip trade siklus ini.")
                        self.logger.info(f"[Market Discovery] Tidak ada market jangka pendek untuk {asset}. Skip trade siklus ini.")
                        return
                    
                    # Validate token ID before placing order
                    if not is_valid_token_id(token_id):
                        error_msg = f"Invalid token ID for {asset} {token_type}: {token_id[:20]}..."
                        self.logger.error(error_msg)
                        # Send error report to Telegram
                        error_message = f"[ORDER GAGAL] Alasan: {error_msg}"
                        send_telegram_report(error_message)
                        return
                    
                    # Validate market expiration: ensure market closes in > 5 minutes
                    if closes_at:
                        try:
                            # Parse as naive UTC
                            if closes_at.endswith('Z'):
                                closes_at_clean = closes_at[:-1]
                            else:
                                closes_at_clean = closes_at
                            end_date = datetime.fromisoformat(closes_at_clean)
                            now_utc = datetime.utcnow().replace(tzinfo=None)
                            minutes_remaining = (end_date - now_utc).total_seconds() / 60
                            if minutes_remaining < 5:
                                self.logger.warning(f"Market for {asset} closes in {minutes_remaining:.1f} minutes (< 5 min). Skipping trade.")
                                return
                        except Exception as e:
                            self.logger.warning(f"Could not parse market expiration: {e}")
                    
                    # Get midpoint price from Polymarket CLOB API
                    midpoint_price = await self.get_midpoint_price(token_id)
                    if midpoint_price is None or midpoint_price <= 0:
                        self.logger.warning(f"Could not get midpoint price for {token_id}. Using fallback price 0.40.")
                        midpoint_price = 0.40
                    
                    print(f"[Debug] Mencoba Real Trade untuk {asset} di alamat {self.wallet_address}...")
                    self.logger.info(f"[Debug] Mencoba Real Trade untuk {asset} di alamat {self.wallet_address}...")
                    self.logger.info(f"Placing order for {asset} at midpoint price: {midpoint_price}")
                    
                    order_result = self.clob_client.place_order(
                        market_id=token_id,
                        side="buy",
                        price=midpoint_price,
                        size=bet_size
                    )
                    
                    if order_result.get('status') == 'ok':
                        order_id = order_result.get('order_id', 'Unknown')
                        self.logger.info(f"Successfully placed Polymarket order: {asset} {token_type} @ ${midpoint_price} size {bet_size}")
                        
                        # Send Telegram report ONLY after getting real Order ID
                        direction_display = signal.direction.replace('BUY_', '')
                        telegram_message = (
                            "NEW TRADE REPORT\n\n"
                            "Trade Details:\n"
                            f"- Waktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"- Aset: {asset}\n"
                            f"- Arah: {direction_display}\n"
                            f"- Edge: {edge}%\n"
                            f"- Bet size: ${bet_size}\n"
                            f"- Order ID: {order_id}\n"
                            "- Status: SUKSES ✅\n"
                            f"- Alasan sinyal: {reason}"
                        )
                        send_telegram_report(telegram_message)
                        print(f"[DEBUG] Order ID: {order_id} - Status: SUKSES")
                    else:
                        error_msg = order_result.get('error', 'Unknown error')
                        self.logger.error(f"Failed to place Polymarket order: {error_msg}")
                        # Send error report to Telegram
                        error_message = f"[ORDER GAGAL] Alasan: {error_msg}"
                        send_telegram_report(error_message)
                        print(f"[DEBUG] Order GAGAL: {error_msg}")
                except Exception as e:
                    self.logger.error(f"Error placing order on Polymarket: {e}")

        except Exception as e:
            self.logger.error(f"Error executing trade for {asset}: {e}")

    def stop(self):
        """Emergency kill - stop monitoring."""
        print("DEBUG: stop() called! Setting stop_requested=True")
        self.stop_requested = True
        self.logger.info("Stop requested by user")
        print("\n" + "!"*60)
        print("🛑 EMERGENCY STOP ACTIVATED")
        print("!"*60)

    async def run_monitoring(self):
        """Jalankan monitoring loop dengan Teknik Gustafsson."""
        self.logger.info("Starting monitoring loop...")
        
        # Try to import psutil for memory monitoring
        try:
            import psutil
            process = psutil.Process()
            has_psutil = True
        except ImportError:
            has_psutil = False
            print("[DEBUG] psutil not installed, skipping memory monitoring")

        cycle_count = 0
        while not self.stop_requested:
            print(f"DEBUG: Loop start. stop_requested={self.stop_requested}")
            try:
                cycle_count += 1
                # Refresh active token IDs from Gamma API each cycle
                try:
                    fetch_token_ids_from_gamma()
                except Exception as e:
                    self.logger.warning(f"Failed to refresh token IDs from Gamma API: {e}")
                
                current_minute = datetime.now().minute
                phase_name, phase_mode = self.get_current_phase()

                print("\n" + "="*60)
                print(f"CYCLE #{cycle_count} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"TEKNIK GUSTAFSSON PHASE: {phase_name} ({phase_mode})")
                print(f"Minute: {current_minute} | Target: {', '.join(TARGET_ASSETS)}")
                print("="*60)
                
                if has_psutil:
                    mem_info = process.memory_info()
                    print(f"[DEBUG] Memory usage: {mem_info.rss / 1024 / 1024:.2f} MB")

                self.logger.info(f"Cycle #{cycle_count} - {phase_name} phase")
                
                tasks = [self.process_signals(asset, phase_name) for asset in TARGET_ASSETS]
                print(f"[DEBUG] Running {len(tasks)} tasks...")
                results = await asyncio.gather(*tasks, return_exceptions=True)
                print(f"[DEBUG] Tasks completed: {len(results)} results")
                for i, r in enumerate(results):
                    if isinstance(r, Exception):
                        print(f"[DEBUG] Task {i} exception: {r}")

                print("\n" + "-"*60)
                print(f"Phase: {phase_name} | Next scan in 3 minutes")
                print("-"*60)

                print(f"[DEBUG] Sleeping for 180 seconds at {datetime.now()}...")
                sys.stdout.flush()
                try:
                    await asyncio.sleep(180)  # 3 minutes
                    print(f"[DEBUG] Woke up from sleep at {datetime.now()}")
                except asyncio.CancelledError:
                    print(f"[DEBUG] Sleep cancelled at {datetime.now()}")
                    raise
                except Exception as e:
                    print(f"[DEBUG] Sleep error: {e}")
                    raise

            except asyncio.CancelledError:
                print("DEBUG: run_monitoring cancelled!")
                self.logger.info("Monitoring loop cancelled")
                break
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                print(f"\nERROR: {e}")
                print("Retrying in 60 seconds...\n")
                await asyncio.sleep(60)


async def command_listener(bot: BotPolymarket):
    """Command listener untuk perintah dari user."""
    import sys
    
    # Check if running in interactive mode
    if not sys.stdin.isatty():
        print("[INFO] Non-interactive mode detected. Command listener disabled.")
        return

    print("\n" + "="*60)
    print("🤖 COMMAND LISTENER STARTED")
    print("="*60)
    print("📋 Available Commands:")
    print("   /cek_saldo_fase  - Check wallet balance & current phase")
    print("   /stop            - Emergency kill (stop bot)")
    print("   /history         - Check last trade")
    print("="*60 + "\n")

    loop = asyncio.get_event_loop()
    while not bot.stop_requested:
        try:
            cmd = await loop.run_in_executor(None, input, "Enter command: ")
            cmd = cmd.strip()

            if cmd == "/stop":
                bot.stop()
                break
            elif cmd == "/history":
                print("\n" + bot.trade_history.get_history_summary())
            elif cmd == "/cek_saldo_fase":
                await bot.check_balance()
                phase_name, phase_mode = bot.get_current_phase()
                minute = datetime.now().minute
                print("\n" + "="*60)
                print("FASE JAM SAAT INI")
                print("="*60)
                print(f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"📊 Minute of Hour: {minute}")
                print(f"📌 Current Phase: {phase_name}")
                print(f"⚙️ Mode: {phase_mode}")
                if minute <= 15:
                    print("🔒 LOCK_TRADE - No execution allowed")
                elif minute <= 40:
                    print("✅ ALLOW_TRADE - Execution based on signals")
                else:
                    print("⚡ AGGRESSIVE_MOMENTUM - Aggressive mode active")
                print("="*60 + "\n")
            elif cmd == "" or cmd == "help":
                print("\n📋 Available Commands:")
                print("   /cek_saldo_fase  - Check wallet balance & current phase")
                print("   /stop            - Emergency kill (stop bot)")
                print("   /history         - Check last trade")
            else:
                print(f"❌ Unknown command: {cmd}")
                print("Type 'help' to see available commands.")
        except EOFError:
            print("\n[INFO] End of input reached. Command listener stopping.")
            break
        except KeyboardInterrupt:
            print("\n🛑 Stopping command listener...")
            break
        except Exception as e:
            print(f"❌ Error: {e}")


async def main():
    """Main function - Jalankan semua agen secara asinkron."""
    sys.stdout.flush()

    print("\n" + "="*60)
    print("AUTOMATED MULTI-AGENT TRADING SYSTEM")
    print("Teknik Gustafsson Implementation")
    print("="*60)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target Assets: {', '.join(TARGET_ASSETS)}")
    print(f"Scan Interval: 3 minutes")
    print("="*60)
    print("DEBUG: Entering main function...")
    sys.stdout.flush()

    agent_main = BotPolymarket()
    print("DEBUG: BotPolymarket initialized")
    sys.stdout.flush()
    await agent_main.check_balance()
    print("DEBUG: Balance checked")
    sys.stdout.flush()

    # Start monitoring directly (command listener disabled for non-interactive mode)
    print("DEBUG: Starting run_monitoring...")
    sys.stdout.flush()
    try:
        await agent_main.run_monitoring()
    except KeyboardInterrupt:
        print("DEBUG: KeyboardInterrupt caught")
        agent_main.stop()
    except Exception as e:
        print(f"DEBUG: Unhandled exception in main: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("DEBUG: Main function finished")
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
