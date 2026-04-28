#!/usr/bin/env python3
"""
Automated Multi-Agent Trading System with Teknik Gustafsson (PRO VERSION)
Agent Utama: Bot Polymarket-AgT41 (The Brain)
Sub-Agent: Bot Collateral Engine (Latency Sniper & Correlation Edge)

🚀 STRATEGI KEUNGGULAN (THE EDGE):
1. Latency Sniping: Membaca lonjakan volume/harga di Binance (1-menit) untuk
   menculik likuiditas Polymarket sebelum Market Maker menyesuaikan spread.
2. Premium Pricing Model: Mengkalkulasi apakah Odds (harga Polymarket) "murah".
   Jika probabilitas teknikal kuat tapi harga Polymarket < 40 sen: SIKAT!
3. Dynamic Memory & Strict Approvals.
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
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from dotenv import load_dotenv
from web3 import Web3

# ==========================================
# 1. KONFIGURASI DASAR & API
# ==========================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/root/.openclaw/workspace/trading_system.log'),
        logging.StreamHandler()
    ]
)

# === Polymarket CLOB V2 (live sejak 28 April 2026) ===
# V1 SDK lama (`py-clob-client`) sudah TIDAK didukung lagi oleh production CLOB.
# Kita pakai `py-clob-client-v2`. Install: pip install py-clob-client-v2
try:
    from py_clob_client_v2 import ClobClient
    PY_CLOB_CLIENT_AVAILABLE = True
    PY_CLOB_CLIENT_VERSION = 2
except ImportError:
    # Fallback: kalau hanya punya V1 yang lama, pakai itu (akan kena order_version_mismatch).
    try:
        from py_clob_client.client import ClobClient
        PY_CLOB_CLIENT_AVAILABLE = True
        PY_CLOB_CLIENT_VERSION = 1
        logging.warning(
            "⚠️ py-clob-client-v2 TIDAK ditemukan, fallback ke V1 lama. "
            "Polymarket sudah upgrade ke CLOB V2 — order via V1 akan ditolak "
            "(`order_version_mismatch`). Jalankan: pip install py-clob-client-v2"
        )
    except ImportError:
        PY_CLOB_CLIENT_AVAILABLE = False
        PY_CLOB_CLIENT_VERSION = 0
        logging.warning("py-clob-client-v2 is missing. Trading real mode will strictly simulate or crash.")

API_CONFIG = {
    'binance': {
        'api_key': os.getenv('BINANCE_API_KEY'),
        'api_secret': os.getenv('BINANCE_API_SECRET')
    },
    'polymarket': {
        'api_key': os.getenv('POLYMARKET_API_KEY'),
        'api_secret': os.getenv('POLYMARKET_API_SECRET'),
        # Optional builder code (bytes32). Set hanya kalau kamu sudah daftar
        # Builder Program Polymarket — kalau belum biarkan kosong (default zero).
        'builder_code': os.getenv('POLYMARKET_BUILDER_CODE'),
        # Tipe signer CLOB V2:
        #   0 = EOA                (private key kamu langsung — tanpa proxy)
        #   1 = POLY_PROXY         (proxy wallet default Polymarket untuk user email-login)
        #   2 = POLY_GNOSIS_SAFE   (Gnosis safe milik Polymarket)
        # Kalau tidak di-set, auto-detect dari ada/tidaknya proxy_address.
        'signature_type': os.getenv('POLYMARKET_SIGNATURE_TYPE'),
        'passphrase': os.getenv('POLYMARKET_PASSPHRASE'),
        'private_key': os.getenv('POLYMARKET_PRIVATE_KEY'),
        'wallet_address': os.getenv('POLYMARKET_WALLET_ADDRESS'),
        'proxy_address': os.getenv('POLYMARKET_PROXY_ADDRESS') or os.getenv('POLYMARKET_PROXY_WALLET')
    },
    'telegram': {
        'token': os.getenv('TELEGRAM_BOT_TOKEN'),
        'chat_id': os.getenv('TELEGRAM_CHAT_ID')
    },
    'twitter': {
        'bearer_token': os.getenv('TWITTER_BEARER_TOKEN')
    }
}

TARGET_ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'HYPE']
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

POLYMARKET_TOKEN_IDS = {}
_GAMMA_LAST_FETCH: Optional[datetime] = None
_GAMMA_CACHE_MINUTES = 15

# ==========================================
# Polygon RPC helper (shared) & CTF Exchange V2 kontrak
# ==========================================
# Daftar RPC Polygon publik yang reliable (28 April 2026). Endpoint
# `rpc-mainnet.maticvigil.com` sudah tidak aktif — diganti dengan mirror
# tambahan yang masih up.
POLYGON_RPC_LIST = [
    'https://polygon-rpc.com',
    'https://1rpc.io/matic',
    'https://polygon.llamarpc.com',
    'https://rpc.ankr.com/polygon',
    'https://polygon-bor-rpc.publicnode.com',
    'https://polygon.drpc.org',
]

# Alamat kontrak Polymarket V2 (live 28 April 2026, lihat https://docs.polymarket.com/v2-migration)
POLYMARKET_PUSD = '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'                # pUSD
POLYMARKET_CTF_EXCHANGE_V2 = '0xE111180000d2663C0091e4f400237545B87B996B'     # standard
POLYMARKET_NEG_RISK_EXCHANGE_V2 = '0xe2222d279d744050d28e00520010520000310F59'  # neg-risk


def connect_polygon_rpc():
    """Coba daftar RPC Polygon secara berurutan, kembalikan Web3 yang connected
    atau None kalau semua gagal.
    """
    for rpc in POLYGON_RPC_LIST:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
            if w3.is_connected():
                return w3, rpc
        except Exception:
            continue
    return None, None


def _signed_raw_tx(signed) -> bytes:
    """web3.py 7+ mengganti `.rawTransaction` → `.raw_transaction` (snake_case).
    Helper ini pakai mana saja yang tersedia supaya kompatibel dua versi.
    """
    return getattr(signed, 'raw_transaction', None) or getattr(signed, 'rawTransaction')


# ==========================================
# 2. STRUKTUR DATA
# ==========================================
@dataclass
class PriceData:
    asset: str
    price: float
    timestamp: datetime
    volume: float = 0.0

@dataclass
class SignalData:
    signal_type: str
    target_asset: str
    direction: str  # "BUY_YES" or "BUY_NO"
    edge_bonus: float
    confidence: float
    source: str

class PortfolioTracker:
    """Manajer Portofolio untuk PnL, Win Rate, dan Saldo Posisi"""
    def __init__(self, filepath='bot_portfolio.json'):
        self.filepath = filepath
        self.stats = self.load_stats()

    def load_stats(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, 'r') as f:
                try:
                    data = json.load(f)
                    if "seen_positions" not in data:
                        data["seen_positions"] = {}
                    if "wins" not in data: data["wins"] = 0
                    if "losses" not in data: data["losses"] = 0
                    if "total_bets" not in data: data["total_bets"] = 0
                    if "accumulated_loss_for_pause" not in data: data["accumulated_loss_for_pause"] = 0.0
                    if "paused_until" not in data: data["paused_until"] = 0.0
                    return data
                except:
                    pass
        # Database default jika baru pertama kali jalan
        return {
            "total_bets": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl": 0.0,
            "in_positions": 0.0,
            "seen_positions": {},
            "accumulated_loss_for_pause": 0.0,
            "paused_until": 0.0
        }

    def save_stats(self):
        with open(self.filepath, 'w') as f:
            json.dump(self.stats, f, indent=4)

    def sync_with_polymarket(self, proxy_wallet: str):
        """Singkronisasi live portfolio PnL & posisi terbuka dari Polymarket Data API"""
        if not proxy_wallet: return
        try:
            url = f"https://data-api.polymarket.com/positions?user={proxy_wallet}"
            import requests
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                positions = res.json()
                total_in_positions = 0.0
                total_cash_pnl = 0.0
                
                if "seen_positions" not in self.stats:
                    self.stats["seen_positions"] = {}
                
                for pos in positions:
                    asset = pos.get("asset", "")
                    if not asset: continue
                    
                    current_val = pos.get("currentValue", 0.0)
                    cash_pnl = pos.get("cashPnl", 0.0)
                    
                    total_in_positions += current_val
                    total_cash_pnl += cash_pnl
                    
                    # Logic estimasi win/loss: 
                    # Jika currentValue sangat kecil (mendekati 0) berarti market sudah resolve/uang ditarik/kalah
                    status = "ACTIVE"
                    if current_val < 0.01:
                        if cash_pnl > 0.01:
                            status = "WIN"
                        elif cash_pnl < -0.01:
                            status = "LOSS"

                    # Update ke stats hanya jika belum dicatat untuk menghindari penghapusan riwayat lama
                    old_status = self.stats["seen_positions"].get(asset)
                    if old_status != status:
                        if status == "WIN":
                            self.stats["wins"] += 1
                        elif status == "LOSS":
                            self.stats["losses"] += 1
                            loss_amount = abs(cash_pnl)
                            self.stats["accumulated_loss_for_pause"] = self.stats.get("accumulated_loss_for_pause", 0.0) + loss_amount
                            if self.stats["accumulated_loss_for_pause"] >= 30.0:
                                self.stats["paused_until"] = time.time() + 7200
                                self.stats["accumulated_loss_for_pause"] = 0.0
                        
                        if old_status == "WIN" and status != "WIN":
                            self.stats["wins"] = max(0, self.stats["wins"] - 1)
                        if old_status == "LOSS" and status != "LOSS":
                            self.stats["losses"] = max(0, self.stats["losses"] - 1)
                            
                        self.stats["seen_positions"][asset] = status

                self.stats["in_positions"] = total_in_positions
                self.stats["realized_pnl"] = total_cash_pnl
                
                # Menyimpan total history bet berdasarkan yang terekam
                self.stats["total_bets"] = max(self.stats["total_bets"], len(self.stats["seen_positions"]))
                
                self.save_stats()
        except Exception as e:
            pass # Silent fail untuk sinkronisasi

    def record_new_bet(self, amount_pusd: float):
        """Mencatat setiap kali bot selesai membeli posisi"""
        self.stats["total_bets"] += 1
        self.stats["in_positions"] += amount_pusd
        self.save_stats()

    def record_settlement(self, is_win: bool, pnl: float, investment_returned: float):
        """Mencatat saat market sudah selesai."""
        if is_win:
            self.stats["wins"] += 1
        else:
            self.stats["losses"] += 1
            
        self.stats["realized_pnl"] += pnl
        self.stats["in_positions"] = max(0.0, self.stats["in_positions"] - investment_returned)
        self.save_stats()

    def get_win_rate(self) -> float:
        resolved = self.stats["wins"] + self.stats["losses"]
        if resolved == 0: 
            return 0.0
        return round((self.stats["wins"] / resolved) * 100, 2)

    def generate_report(self, available_balance: float, action_msg: str) -> str:
        """Membuat template laporan Telegram yang cantik"""
        win_rate = self.get_win_rate()
        return (
            f"🚨 **EKSEKUSI TRADE BERHASIL** 🚨\n"
            f"{action_msg}\n\n"
            f"📊 **LIVE PORTFOLIO & P&L**\n"
            f"💵 Tersedia: `${available_balance:.2f}` pUSD\n"
            f"🔒 Di Posisi: `${self.stats['in_positions']:.2f}` pUSD\n"
            f"📈 PnL Bersih: `${self.stats['realized_pnl']:.2f}` pUSD\n"
            f"🏆 Win Rate: `{win_rate}%`\n"
            f"📝 Detail: (Total Bet: {self.stats['total_bets']} | Menang: {self.stats['wins']} | Kalah: {self.stats['losses']})"
        )

# ==========================================
# 3. HELPER TEKNIKAL & MATEMATIKA
# ==========================================
def calc_rsi(closes: List[float], period: int = 14) -> float:
    """Manual RSI calculation to avoid heavy dependencies."""
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [abs(d) if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calc_ema(closes: List[float], period: int = 50) -> float:
    if len(closes) < period: return closes[-1] if closes else 0.0
    multiplier = 2 / (period + 1)
    ema = closes[0]
    for close in closes[1:]:
        ema = (close * multiplier) + (ema * (1 - multiplier))
    return ema

# ==========================================
# 4. GAMA MARKET DISCOVERY (DINAMIS & TEPAT)
# ==========================================
# Pola regex untuk penemuan market 1 jam (1H) yang akurat
TF_1H_PATTERNS = [
    r"\b1[\s\-]?h\b", r"\b1[\s\-]?hour\b", r"\bnext[\s\-]hour\b", r"\b60[\s\-]?min", r"\bhourly\b"
]

TF_EXCLUDE_PATTERNS = [
    r"\b1[\s\-]?m\b(?!.*hour)", r"\b1[\s\-]?min(?:ute)?\b", r"\b5[\s\-]?m\b", r"\b5[\s\-]?min(?:ute)?s?\b",
    r"\b15[\s\-]?m\b", r"\b15[\s\-]?min(?:ute)?s?\b", r"\b30[\s\-]?m\b", r"\b30[\s\-]?min(?:ute)?s?\b",
    r"\b4[\s\-]?h\b", r"\b4[\s\-]?hour\b", r"\b8[\s\-]?h\b", r"\b12[\s\-]?h\b", r"\b24[\s\-]?h\b",
    r"\bdail(?:y|ies)\b", r"\bweekl(?:y|ies)\b", r"\bmonth(?:ly)?\b", r"\b(?:2|3|10|20|45)[\s\-]?min"
]

def is_valid_1h_market(question: str, target_asset: str, description: str = "") -> bool:
    norm = re.sub(r"\s+", " ", question.lower().strip())
    desc = re.sub(r"\s+", " ", description.lower().strip())
    
    # 1. Pastikan asset ada di dalam pertanyaan
    asset_keywords = {
        'BTC': ['bitcoin', 'btc'],
        'ETH': ['ethereum', 'eth'],
        'SOL': ['solana', 'sol'],
        'XRP': ['ripple', 'xrp'],
        'BNB': ['binance coin', 'bnb'],
        'DOGE': ['dogecoin', 'doge'],
        'HYPE': ['hype', 'hyperliquid']
    }
    matched = False
    for kw in asset_keywords.get(target_asset, [target_asset.lower()]):
        if re.search(rf"\b{re.escape(kw)}\b", norm, re.IGNORECASE):
            matched = True
            break
    if not matched: return False

    # 2. Pastikan tipe up-or-down
    up_down_patterns = [r"up\s+or\s+down", r"up-or-down", r"go\s+up\s+or", r"higher\s+or\s+lower", r"above\s+or\s+below", r"pump\s+or\s+dump", r"bullish\s+or\s+bearish", r"rise\s+or\s+fall"]
    if not any(re.search(p, norm, re.IGNORECASE) for p in up_down_patterns):
        return False
        
    # 3. Pastikan 1-hour market menggunakan deskripsi dari Polymarket
    if "1 hour candle" not in desc:
        return False
        
    return True

def _extract_token_ids(market: dict) -> Tuple[Optional[str], Optional[str]]:
    # Support both old API format (tokens array) and new API format (clobTokenIds + outcomes)
    tokens = market.get('tokens', [])
    yes_id, no_id = None, None
    if tokens:
        for t in tokens:
            outcome = str(t.get('outcome', '')).strip().lower()
            tid = str(t.get('token_id', '')).strip()
            if outcome == 'yes': yes_id = tid
            elif outcome == 'no': no_id = tid
    else:
        clob_ids = market.get('clobTokenIds', '')
        outcomes = market.get('outcomes', '')
        if clob_ids and outcomes:
            try:
                import json
                ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                outs = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                if len(ids) >= 2 and len(outs) >= 2:
                    for i, outcome in enumerate(outs):
                        if i < len(ids):
                            outcome_lower = str(outcome).strip().lower()
                            if outcome_lower in ['yes', 'up']: yes_id = str(ids[i]).strip()
                            elif outcome_lower in ['no', 'down']: no_id = str(ids[i]).strip()
            except: pass
    return yes_id, no_id

def fetch_token_ids_from_gamma(force: bool = False):
    global _GAMMA_LAST_FETCH
    now_utc = datetime.utcnow()

    if not force and _GAMMA_LAST_FETCH is not None:
        if (now_utc - _GAMMA_LAST_FETCH).total_seconds() / 60 < _GAMMA_CACHE_MINUTES:
            return bool(POLYMARKET_TOKEN_IDS)

    logging.info("[Discovery] 🔍 Memulai pencarian mendalam Gamma API menggunakan Prediksi Slug untuk 1H Market (Up/Down) ...")
    try:
        best_markets = {}
        session = requests.Session()
        
        # Mapping nama asset ke slug format
        asset_slugs = {
            'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana', 'XRP': 'xrp', 
            'DOGE': 'dogecoin', 'BNB': 'bnb', 'HYPE': 'hype'
        }
        
        current_utc_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        
        for asset in TARGET_ASSETS:
            asset_str = asset_slugs.get(asset)
            if not asset_str:
                logging.warning(f"⚠️ [WARNING] No slug mapping for {asset}. Skipping.")
                continue
            
            # Cari dari current hour sampai 5 jam ke depan untuk cari market yg paling dekat atau cocok
            for i in range(5):
                target_dt = current_utc_hour + timedelta(hours=i)
                # Secara kasar Waktu Timur (ET) adalah UTC-4
                dt_et = target_dt - timedelta(hours=4)
                
                month_name = dt_et.strftime('%B').lower()
                day = dt_et.day
                year = dt_et.year
                hour_12 = int(dt_et.strftime('%I'))
                am_pm = dt_et.strftime('%p').lower()
                
                slug = f"{asset_str}-up-or-down-{month_name}-{day}-{year}-{hour_12}{am_pm}-et"
                
                try:
                    req = session.get(f"{GAMMA_HOST}/events?slug={slug}", timeout=5)
                    if not req.ok: continue
                    data = req.json()
                    if not data: continue
                    
                    for event in data:
                        markets = event.get('markets', [])
                        for m in markets:
                            if not m.get('active') or m.get('closed'):
                                continue
                            
                            end_str = m.get('endDate', '')
                            if not end_str: continue
                            
                            clean_str = end_str[:-1] if end_str.endswith('Z') else end_str
                            clean_str = clean_str.split('+')[0]
                            try:
                                end_dt = datetime.fromisoformat(clean_str)
                                hours_left = (end_dt - now_utc).total_seconds() / 3600
                                if hours_left < 0 or hours_left > 12: continue
                            except: continue
                            
                            yes_id, no_id = _extract_token_ids(m)
                            if not (yes_id and no_id): continue
                            
                            if asset not in best_markets or hours_left < best_markets[asset]['hours']:
                                best_markets[asset] = {
                                    'yes': yes_id, 'no': no_id, 'hours': hours_left, 'closes_at': end_str,
                                    'title': m.get('question', '')
                                }
                except Exception as e:
                    pass

        POLYMARKET_TOKEN_IDS.clear()
        for k, v in best_markets.items():
            POLYMARKET_TOKEN_IDS[k] = v
            logging.info(f"🟢 [Locked 1H Market] {k}: {v['title'][:60]}... ({v['hours']:.1f}h left)")
        
        _GAMMA_LAST_FETCH = now_utc
        return True
    except Exception as e:
        logging.error(f"[Discovery] Fetch error: {e}")
        return False

# ==========================================
# 5. CORE ENGINES: TECHNICAL, CORRELATION, CLOB
# ==========================================
class BotScraper:
    """Sub-Agent 1: Bot Scraper - Sentimen (UPGRADED VERSION)"""

    def __init__(self):
        self.logger = logging.getLogger("Scraper")
        
        # Target assets disamakan dengan Bot Utama
        self.target_assets = {
            'BTC': [r'\bbitcoin\b', r'\bbtc\b'],
            'ETH': [r'\bethereum\b', r'\beth\b'],
            'SOL': [r'\bsolana\b', r'\bsol\b'],
            'XRP': [r'\bripple\b', r'\bxrp\b'],
            'BNB': [r'\bbinance\b', r'\bbnb\b'],
            'DOGE': [r'\bdogecoin\b', r'\bdoge\b'],
            'HYPE': [r'\bhype\b', r'\bhyperliquid\b']
        }
        
        # Regex (Regular Expression) agar hanya membaca kata yang utuh (\b)
        self.positive_patterns = [r'\bapprove', r'\betf\b', r'\bbull', r'\bsurge', r'\bmoon', r'\bpartnership', r'\blaunch', r'\bgain', r'\bprofit']
        self.negative_patterns = [r'\bhack', r'\bcrash', r'\bban', r'\bsec\b', r'\bfraud', r'\bdump', r'\bscam', r'\bleak', r'\bfear', r'\bsue']

        self.rss_feeds = [
            'https://www.theblock.co/rss',
            'https://www.coindesk.com/rss',
            'https://cryptobriefing.com/rss',
            'https://cointelegraph.com/rss'
        ]
        
        self.twitter_accounts = [
            "Tree_of_Alpha", "Tier10k", "unfolded", "WatcherGuru", "saylor", 
            "BitcoinMagazine", "VitalikButerin", "sassal0x", "aeyakovenko", "solana", 
            "bgarlinghouse", "EleanorTerrett", "whale_alert", "lookonchain", "cz_binance", 
            "BNBCHAIN", "RichardTeng", "WuBlockchain", "BillyM2k", "dogecoin", 
            "elonmusk", "chameleon_jeff", "HyperliquidX", "mishaboar", "Hedge_Fund_7"
        ]
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        self.twitter_bearer_token = None
        try:
            bearer_token = API_CONFIG.get('twitter', {}).get('bearer_token')
            if bearer_token:
                self.twitter_bearer_token = bearer_token
                self.logger.info("✅ Twitter API Token (v2) terdeteksi di .env")
            else:
                self.logger.info("⚠️ Twitter API Token tidak ditemukan. Modul Twitter dalam status standby.")
        except Exception as e:
            self.logger.debug(f"Twitter API skip: {e}")

        self.logger.info("✅ Sub-Agent: Bot Scraper initialized")

    async def scan_rss_feeds(self):
        signals = []
        for feed_url in self.rss_feeds:
            try:
                response = await asyncio.to_thread(
                    requests.get, feed_url, headers=self.headers, timeout=10
                )
                if response.status_code != 200:
                    continue
                    
                feed = feedparser.parse(response.content)
                for entry in feed.entries[:5]: # Ambil 5 berita teratas agar lebih responsif
                    
                    # --- FILTRASI WAKTU (5 Menit Terakhir) ---
                    # Menghindari berita kadaluarsa (Stale News)
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        import calendar
                        from time import time
                        published_ts = calendar.timegm(entry.published_parsed)
                        current_ts = time()
                        if current_ts - published_ts > 300: # 300 detik = 5 Menit
                            continue
                            
                    title = entry.get('title', '').lower()
                    
                    # 1. Deteksi Berita Ini Tentang Koin Apa?
                    detected_asset = None
                    for asset, patterns in self.target_assets.items():
                        if any(re.search(p, title) for p in patterns):
                            detected_asset = asset
                            break
                            
                    # Jika berita tidak membahas koin target kita, buang!
                    if not detected_asset:
                        continue
                    
                    # 2. Deteksi Sentimen Menggunakan Regex
                    is_positive = any(re.search(p, title) for p in self.positive_patterns)
                    is_negative = any(re.search(p, title) for p in self.negative_patterns)
                    
                    # 3. Mapping Sinyal ke Format Mesin Utama (BUY_YES / BUY_NO)
                    if is_positive and not is_negative:
                        signals.append(SignalData(
                            signal_type="NEWS_BULLISH",
                            target_asset=detected_asset,
                            direction="BUY_YES", # Diubah agar dibaca sebagai 'UP' oleh Polymarket
                            edge_bonus=15.0,     # Equivalent to urgency logic
                            confidence=80.0,     # Tambahan skor confidence
                            source="RSS_Scraper"
                        ))
                        self.logger.info(f"📰 [BULLISH NEWS] {detected_asset}: {title[:50]}...")
                        
                    elif is_negative and not is_positive:
                        signals.append(SignalData(
                            signal_type="NEWS_BEARISH",
                            target_asset=detected_asset,
                            direction="BUY_NO",  # Diubah agar dibaca sebagai 'DOWN' oleh Polymarket
                            edge_bonus=15.0,     # Equivalent to urgency logic
                            confidence=80.0,     # Tambahan skor confidence
                            source="RSS_Scraper"
                        ))
                        self.logger.info(f"📰 [BEARISH NEWS] {detected_asset}: {title[:50]}...")
                        
            except Exception as e:
                self.logger.warning(f"⚠️ Skip RSS {feed_url}: {e}")
                
        return signals

    async def scan_twitter(self):
        """Scrape latest crypto news updates via Search Engine targeting Twitter."""
        signals = []
        try:
            # Pecah menjadi batch 5 username agar query tidak terlalu panjang
            chunk_size = 5
            for i in range(0, len(self.twitter_accounts), chunk_size):
                batch = self.twitter_accounts[i:i+chunk_size]
                
                # Format: site:twitter.com ("user1" OR "user2") news
                usernames_query = " OR ".join([f'"{user}"' for user in batch])
                query = f'site:twitter.com ({usernames_query}) crypto news'
                
                url = "https://www.bing.com/search"
                user_agents = [
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ]
                headers = {
                    "User-Agent": __import__('random').choice(user_agents)
                }
                params = {
                    "q": query
                }
                
                response = await asyncio.to_thread(
                    requests.get, url, headers=headers, params=params, timeout=10
                )
                
                # Hindari di-ban jika terlalu cepat
                await asyncio.sleep(5)

                if response.status_code == 200:
                    html_content = response.text
                    
                    # Bersihkan tag script, style, dan html tags dengan Regex standar
                    clean_text = re.sub(r'<script.*?>.*?</script>', ' ', html_content, flags=re.DOTALL)
                    clean_text = re.sub(r'<style.*?>.*?</style>', ' ', clean_text, flags=re.DOTALL)
                    clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
                    clean_text = ' '.join(clean_text.split()) # normalisasi whitespace
                    text_lower = clean_text.lower()
                    
                    # Analisa sentimen dari text yang terambil (Search Snippets / Titles)
                    detected_assets = []
                    for asset, patterns in self.target_assets.items():
                        if any(re.search(p, text_lower) for p in patterns):
                            detected_assets.append(asset)
                            
                    for asset in detected_assets:
                        is_positive = any(re.search(p, text_lower) for p in self.positive_patterns)
                        is_negative = any(re.search(p, text_lower) for p in self.negative_patterns)
                        
                        if is_positive and not is_negative:
                            signals.append(SignalData(
                                signal_type="TWITTER_SEARCH_BULLISH",
                                target_asset=asset,
                                direction="BUY_YES",
                                edge_bonus=15.0,
                                confidence=80.0,
                                source="Twitter_Search_Scraper"
                            ))
                            self.logger.info(f"🔍 [SEARCH-TWITTER BULLISH] Mendeteksi respon positif untuk {asset}")
                            
                        elif is_negative and not is_positive:
                            signals.append(SignalData(
                                signal_type="TWITTER_SEARCH_BEARISH",
                                target_asset=asset,
                                direction="BUY_NO",
                                edge_bonus=15.0,
                                confidence=80.0,
                                source="Twitter_Search_Scraper"
                            ))
                            self.logger.info(f"🔍 [SEARCH-TWITTER BEARISH] Mendeteksi respon negatif untuk {asset}")
                elif response.status_code == 429:
                    self.logger.warning("⚠️ Search Engine rate limit (429)! Delaying scraper.")
                    await asyncio.sleep(15)
                    break # Stop batching jika kena limit
                else:
                    self.logger.warning(f"⚠️ Error Search Engine: HTTP {response.status_code}")
                    
        except Exception as e:
            self.logger.warning(f"⚠️ Skip Search Twitter Scraper Error: {e}")
            
        return signals

    async def get_signals(self):
        signals = []
        signals.extend(await self.scan_rss_feeds())
        signals.extend(await self.scan_twitter())
        return signals

class AdvancedCollateralEngine:
    """Binance Fast-Fetch Data Engine for Technical Alpha"""
    def __init__(self):
        self.binance = ccxt.binance()
        
    async def get_ohlcv(self, asset: str, tf: str = '1m', limit: int = 60) -> List[dict]:
        try:
            # Menggunakan asyncio.to_thread murni agar non-blocking
            data = await asyncio.to_thread(self.binance.fetch_ohlcv, f"{asset}/USDT", tf, limit=limit)
            # Format: [timestamp, open, high, low, close, volume]
            return data
        except:
            return []

    async def calculate_alpha_signal(self, asset: str) -> Optional[SignalData]:
        """Menghitung momentum dan volatilitas (Latency Sniping & Crossover Logic)"""
        ohlcv = await self.get_ohlcv(asset, '3m', 100)
        if len(ohlcv) < 50: return None
        
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]
        
        current_price = closes[-1]
        prev_price = closes[-2]
        
        ema50_current = calc_ema(closes, 50)
        ema50_prev = calc_ema(closes[:-1], 50)
        
        rsi_current = calc_rsi(closes, 14)
        rsi_prev = calc_rsi(closes[:-1], 14)
        
        # Deteksi Lonjakan Volume (Celah 1: Perbandingan yang lebih akurat dengan SMA 20 Volume terakhir)
        avg_vol = sum(volumes[-21:-1]) / 20 if sum(volumes[-21:-1]) > 0 else 1
        # Mengukur surge dari volume candle terakhir (Celah 2: Real-time spike)
        vol_surge = volumes[-1] / avg_vol
        
        # Momentum Kalkulus (Celah 3: Crossover sejati, menangkap perpindahan trend secara eksak)
        cross_up = prev_price <= ema50_prev and current_price > ema50_current
        cross_down = prev_price >= ema50_prev and current_price < ema50_current
        
        # RSI Reversal (Celah 4: Menangkap momen "keluar" dari oversold/overbought)
        rsi_golden_cross = rsi_prev <= 30 and rsi_current > 30
        rsi_death_cross = rsi_prev >= 70 and rsi_current < 70
        
        # Skenario 1: Breakout Crossover UP + Volume Spike
        if cross_up and vol_surge > 3.5 and rsi_current < 70:
            return SignalData("BREAKOUT_CROSS_UP", asset, "BUY_YES", 25.0, 85.0, "Tech_Alpha")
            
        # Skenario 2: Breakdown Crossover DOWN + Volume Spike
        if cross_down and vol_surge > 3.5 and rsi_current > 30:
            return SignalData("BREAKDOWN_CROSS_DOWN", asset, "BUY_NO", 25.0, 85.0, "Tech_Alpha")
            
        # Skenario 3: Reversal / Recovery (Golden RSI Cross)
        if rsi_golden_cross and vol_surge > 2.5:
            return SignalData("OVERSOLD_REVERSAL", asset, "BUY_YES", 15.0, 75.0, "Tech_Alpha")
            
        # Skenario 4: Overbought Rejection (Death RSI Cross)
        if rsi_death_cross and vol_surge > 2.5:
            return SignalData("OVERBOUGHT_REJECT", asset, "BUY_NO", 15.0, 75.0, "Tech_Alpha")

        return None
        
    async def ohlcv(self, *args):
        return await self.get_ohlcv(*args)

class BotPolymarket:
    def __init__(self):
        self.logger = logging.getLogger("PolymarketMaster")
        self.tech_engine = AdvancedCollateralEngine()
        self.scraper = BotScraper()
        self.portfolio = PortfolioTracker()
        self.private_key = API_CONFIG['polymarket']['private_key']
        self.wallet = API_CONFIG['polymarket']['wallet_address']
        self.proxy_wallet = API_CONFIG['polymarket'].get('proxy_address')
        
        # Fallback ke address hardcode lama jika proxy tidak di-set di env
        if not self.proxy_wallet:
            self.proxy_wallet = self.wallet if self.wallet else '0xe6A9bE1740d29F59D6e00C9a08Ee8eacBC299D0b'
        
        # Tracker agar tidak trade double di coin yang sama dalam 40 menit
        self.last_trade_time = {}
        
        # Builder code (bytes32). Default = zero (no builder attribution).
        # Polymarket CLOB V2 menggantikan POLY_BUILDER_* HMAC headers dengan field
        # `builder` di order yang ditandatangani — sekarang cuma satu string.
        builder_code_env = API_CONFIG['polymarket'].get('builder_code')
        self.builder_code = builder_code_env if builder_code_env else None

        # Auto-detect signer type kalau user tidak set manual via env var.
        #   • EOA (0)         → tidak ada proxy wallet terpisah
        #   • POLY_PROXY (1)  → pakai proxy Polymarket default (email-login user)
        sig_type_env = API_CONFIG['polymarket'].get('signature_type')
        if sig_type_env is not None:
            try:
                self.signature_type = int(sig_type_env)
            except ValueError:
                self.signature_type = 1
        else:
            # Auto: kalau proxy_wallet sama dengan EOA atau tidak di-set → EOA.
            if self.wallet and self.proxy_wallet \
                    and self.wallet.lower() == self.proxy_wallet.lower():
                self.signature_type = 0
            elif API_CONFIG['polymarket'].get('proxy_address'):
                self.signature_type = 1
            else:
                self.signature_type = 0
        self.logger.info(
            f"🔐 Signer config: signature_type={self.signature_type} "
            f"(0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE) | funder={self.proxy_wallet}"
        )

        if self.private_key and PY_CLOB_CLIENT_AVAILABLE:
            # NOTE: V2 ClobClient masih menerima signature constructor yang sama:
            #   ClobClient(host, chain_id, key=..., signature_type=..., funder=...)
            self.clob_client = ClobClient(
                CLOB_HOST, 137, self.private_key,
                signature_type=self.signature_type, funder=self.proxy_wallet,
            )
            try:
                # V2 method: create_or_derive_api_key (sebelumnya: create_or_derive_api_creds)
                if PY_CLOB_CLIENT_VERSION >= 2 and hasattr(self.clob_client, "create_or_derive_api_key"):
                    self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_key())
                else:
                    # Backward compat untuk V1
                    self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_creds())
            except Exception as e:
                self.logger.error(f"Gagal set API Creds: {e}")
        else:
            raise Exception("🛑 STOP: MODE REAL TRADE AKTIF! Bot dihentikan karena 'Private Key' atau library 'py_clob_client_v2' tidak ditemukan.")

    async def check_and_approve_pusd(self):
        """Verifikasi & auto-approve pUSD allowance untuk CTF Exchange V2
        (baik exchange standard maupun neg-risk exchange).

        CATATAN PENTING — POLY_PROXY (signature_type=1):
        Kalau kamu pakai proxy wallet Polymarket (misalnya akun email-login),
        pUSD-mu duduk di alamat proxy, bukan di EOA. Approve manual via RPC
        dari EOA cuma meng-approve tokens milik EOA, bukan proxy. Untuk user
        proxy, approval WAJIB dilakukan via UI Polymarket sekali — bot
        hanya bisa memverifikasi status, bukan meng-approve otomatis.
        """
        self.logger.info("🔐 [Web3] Verifikasi pUSD allowance CTF Exchange V2 (standard + neg-risk)...")
        if not self.wallet or not self.private_key:
            return

        w3, rpc = connect_polygon_rpc()
        if not w3:
            self.logger.error("❌ Semua RPC Polygon gagal terhubung! Approval di-skip.")
            return
        self.logger.info(f"✅ RPC Connected: {rpc}")

        # Alamat dompet yang memegang pUSD:
        #   • signature_type == 0 (EOA)  → EOA kamu sendiri
        #   • signature_type >= 1       → proxy wallet Polymarket
        owner_addr = self.proxy_wallet if self.signature_type >= 1 else self.wallet
        try:
            owner = w3.to_checksum_address(owner_addr)
        except Exception as e:
            self.logger.error(f"❌ Alamat pemilik pUSD invalid ({owner_addr}): {e}")
            return

        pusd = w3.to_checksum_address(POLYMARKET_PUSD)
        spenders = [
            ("CTF Exchange V2 (standard)", w3.to_checksum_address(POLYMARKET_CTF_EXCHANGE_V2)),
            ("CTF Exchange V2 (neg-risk)", w3.to_checksum_address(POLYMARKET_NEG_RISK_EXCHANGE_V2)),
        ]

        erc20_abi = [
            {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
            {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
        ]
        contract = w3.eth.contract(address=pusd, abi=erc20_abi)

        eoa_is_owner = self.signature_type == 0
        for label, spender in spenders:
            try:
                allowance = contract.functions.allowance(owner, spender).call()
                if allowance >= (50 * 10**6):
                    self.logger.info(
                        f"✅ [pUSD] Allowance {label} aman: {allowance/10**6:,.2f} pUSD"
                    )
                    continue

                # Allowance kurang — cuma bisa auto-approve kalau EOA sendiri
                # yang pegang pUSD. Kalau pakai POLY_PROXY/SAFE, user harus
                # approve dari UI Polymarket.
                if not eoa_is_owner:
                    self.logger.warning(
                        f"⚠️ [pUSD] Allowance {label} rendah ({allowance/10**6:,.2f} pUSD) "
                        f"tapi owner adalah proxy — auto-approve di-skip. "
                        f"Silakan approve sekali via UI Polymarket."
                    )
                    continue

                self.logger.warning(
                    f"⚠️ [pUSD] Allowance {label} rendah/kosong — meminta MAX approve..."
                )
                eoa = w3.to_checksum_address(self.wallet)
                nonce = w3.eth.get_transaction_count(eoa)
                tx = contract.functions.approve(spender, 2**256 - 1).build_transaction({
                    'chainId': 137, 'gas': 100000,
                    'maxFeePerGas': w3.to_wei('50', 'gwei'),
                    'maxPriorityFeePerGas': w3.to_wei('50', 'gwei'),
                    'nonce': nonce,
                })
                signed = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
                tx_hash = w3.eth.send_raw_transaction(_signed_raw_tx(signed))
                self.logger.info(
                    f"⏳ [pUSD] Approve {label} terkirim: {tx_hash.hex()} — menunggu konfirmasi..."
                )
                w3.eth.wait_for_transaction_receipt(tx_hash)
                self.logger.info(f"✅ [pUSD] Approve {label} selesai.")
            except Exception as e:
                self.logger.error(f"❌ [pUSD] Cek/approve {label} error: {e}")

    def garbage_collection(self):
        """Pembersihan token kadaluarsa dari memori agar RAM tidak bocor."""
        now = datetime.utcnow().replace(tzinfo=None)
        expired = []
        for asset, data in list(POLYMARKET_TOKEN_IDS.items()):
            try:
                dt_str = data['closes_at']
                dt_str = dt_str[:-1] if dt_str.endswith('Z') else dt_str
                end_dt = datetime.fromisoformat(dt_str.split('+')[0])
                if (end_dt - now).total_seconds() < 60:
                    expired.append(asset)
            except: pass
        for e in expired:
            logging.info(f"🗑️ [Garbage Collection] Hapus {e} (Expired/Resolving)")
            del POLYMARKET_TOKEN_IDS[e]

    def send_telegram_report(self, message):
        """Mengirim laporan ke Telegram"""
        try:
            token = API_CONFIG['telegram']['token']
            chat_id = API_CONFIG['telegram']['chat_id']
            if token and chat_id:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {'chat_id': chat_id, 'text': message}
                requests.post(url, json=payload, timeout=5)
        except Exception as e:
            self.logger.error(f"❌ Telegram report failed: {e}")

    async def check_balance(self, broadcast: bool = False):
        """Cek saldo pUSD di Proxy Wallet"""
        try:
            w3, _rpc = connect_polygon_rpc()
            if not w3:
                self.logger.error("❌ Semua RPC Polygon gagal saat cek saldo.")
                return 0.0

            proxy_wallet = self.proxy_wallet if self.proxy_wallet else '0xe6A9bE1740d29F59D6e00C9a08Ee8eacBC299D0b'

            # Cek pUSD Token (CTF Exchange V2 Collateral)
            erc20_abi = '[{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'
            contract = w3.eth.contract(
                address=w3.to_checksum_address(POLYMARKET_PUSD),
                abi=json.loads(erc20_abi),
            )
            balance = contract.functions.balanceOf(w3.to_checksum_address(proxy_wallet)).call()
            balance_pusd = balance / 10**6
            
            # Jika 0, mari kita cek juga USDC bridged / native USDC just in case (optional tapi kita skip USDC lama)
            if balance_pusd == 0.0:
                usdc_e_addr = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
                contract_e = w3.eth.contract(address=w3.to_checksum_address(usdc_e_addr), abi=json.loads(erc20_abi))
                val_e = contract_e.functions.balanceOf(w3.to_checksum_address(proxy_wallet)).call()
                # If they have old USDC, just log it, but bot strictly checks pUSD balance for returning? 
                # Let's still use pUSD as main balance, returning pUSD
                if val_e > 0:
                    self.logger.warning(f"⚠️ Saldo pUSD kosong, tapi dompet punya {val_e / 10**6:,.2f} USDC. Mohon wrap USDC -> pUSD via dApp Polymarket.")

            if broadcast:
                status_msg = f"🤖 Status Bot Polymarket 📍 Proxy: {proxy_wallet[:10]}...{proxy_wallet[-4:]} 💰 Saldo: {balance_pusd:,.2f} pUSD 📊 Status: Aktif"
                if balance_pusd == 0:
                    status_msg = f"⚠️ Saldo di Proxy Wallet Kosong!\nMengecek: {proxy_wallet}\nPastikan minimal ada saldo pUSD di Polygon Network."
                self.send_telegram_report(status_msg)
                
            return balance_pusd
        except Exception as e:
            self.logger.error(f"❌ Balance check failed: {e}")
            return 0.0

    async def get_clob_midpoint(self, token_id: str) -> float:
        """Mengambil harga langsung dari Orderbook Polymarket."""
        try:
            req = await asyncio.to_thread(requests.get, f"{CLOB_HOST}/midpoint?token_id={token_id}", timeout=5)
            return float(req.json().get('mid', 0.0))
        except: return 0.0

    async def deploy_smart_order(self, asset: str, signal: SignalData, is_aggressive: bool = False):
        """
        Logika eksekusi yang menggunakan 'Pricing Edge'.
        Jika teknikal mengatakan harga akan naik (BUY_YES), tapi di Polymarket
        probabilitasnya sudah direpresentasikan 90% ($0.90), kita batalkan (Risk/Reward buruk).
        """
        from time import time
        current_time = time()
        last_traded = self.last_trade_time.get(asset, 0)
        
        # Cooldown 40 menit (2400 detik) untuk asset yang sama
        if current_time - last_traded < 40 * 60:
            minutes_left = 40 - ((current_time - last_traded) / 60)
            self.logger.info(f"⏭️ [SKIP] Asset {asset} sudah ditrade baru-baru ini. Cooldown tersisa: {minutes_left:.1f} menit.")
            return

        target_side = 'yes' if signal.direction == 'BUY_YES' else 'no'
        
        if asset not in POLYMARKET_TOKEN_IDS: return
        token_id = POLYMARKET_TOKEN_IDS[asset][target_side]
        
        odds_price = await self.get_clob_midpoint(token_id)
        if odds_price == 0.0: return
        
        # ==========================================
        # VALUE BETTING (R/R CHECKER)
        # ==========================================
        # Strategi: Jangan membeli opsi di atas 75 sen ($0.75) kecuali momentumnya ekstrim.
        # Jika probabilitas sangat memihak tapi harga murah (<$0.50), perbesar bet!
        
        if odds_price > 0.85:
            self.logger.warning(f"🚫 [Skip] {asset} {signal.direction} terlalu mahal ({odds_price}). Bad R:R.")
            return
            
        base_bet = 4.3 # pUSD
        
        # Beta multiplier untuk fase agresif
        if is_aggressive:
            base_bet *= 1.5
            self.logger.info(f"🔥 [FASE AGRESIF] Mengaktifkan Beta Multiplier untuk {asset}!")

        if odds_price < 0.40 and signal.confidence >= 80:
            # Peluang emas! Diskon probabilitas.
            bet_size = base_bet * 3.0
            self.logger.info(f"💎 [VALUE BET] Menemukan dislokasi harga pada {asset}!")
        elif odds_price <= 0.65:
            bet_size = base_bet * 1.5
        else:
            bet_size = base_bet

        self.logger.info(f"📈 [EXECUTION] Mengirim order: {asset} {target_side.upper()} | Harga: ${odds_price:.3f} | Jumlah: ${bet_size:.2f} pUSD | Sinyal: {signal.signal_type}")
        
        # Sanity check ukuran order — Polymarket minimum $1 notional.
        # Kalau bet_size < $1, skip daripada dapat error silent dari CLOB.
        if bet_size < 1.0:
            self.logger.warning(
                f"⚠️ [SKIP] {asset} bet_size ${bet_size:.2f} < $1 notional minimum Polymarket."
            )
            return

        if self.clob_client:
            try:
                # === Polymarket CLOB V2 order args ===
                # V2 menghilangkan field: nonce, fee_rate_bps, taker, expiration
                # (expiration tetap optional di luar struct yang di-sign).
                # V2 menambahkan field: builder_code, metadata (default zero bytes32).
                # Fee sekarang di-set operator on-chain saat match — bukan lagi
                # di order yang di-sign.
                if PY_CLOB_CLIENT_VERSION >= 2:
                    from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, OrderType
                else:
                    # Path fallback V1 — akan ditolak oleh production CLOB V2
                    # dengan error `order_version_mismatch`. Hanya untuk debug.
                    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType  # type: ignore

                # Convert size to share amount (pUSD / price). Biar builder V2
                # internal yang melakukan rounding ke tick_size — kita kirim 4 desimal.
                share_size = round(bet_size / odds_price, 4)

                # Build OrderArgs — V2 hanya butuh token_id/price/size/side.
                # Pass builder_code kalau di-set (untuk revenue sharing program).
                order_kwargs = dict(
                    token_id=token_id,
                    price=odds_price,
                    size=share_size,
                    side="BUY",
                )
                if PY_CLOB_CLIENT_VERSION >= 2 and self.builder_code:
                    order_kwargs["builder_code"] = self.builder_code

                args = OrderArgs(**order_kwargs)

                # Pakai `create_and_post_order` — di V2 ini dibungkus oleh
                # `_retry_on_version_update`, jadi kalau server pernah bump
                # kontrak lagi & kirim `order_version_mismatch`, client akan
                # otomatis re-resolve version dan re-sign ulang dengan domain
                # yang baru. Dengan pendekatan manual `create_order` + `post_order`
                # fitur ini hilang.
                if PY_CLOB_CLIENT_VERSION >= 2 and hasattr(self.clob_client, "create_and_post_order"):
                    post_res = self.clob_client.create_and_post_order(
                        args,
                        PartialCreateOrderOptions(neg_risk=None),
                        order_type=OrderType.GTC,
                    )
                else:
                    signed_order = self.clob_client.create_order(
                        args, PartialCreateOrderOptions(neg_risk=None)
                    )
                    post_res = self.clob_client.post_order(signed_order, OrderType.GTC)

                # Cek jika ada error di response (termasuk dict error yang sering dikembalikan Polymarket clob_client)
                if isinstance(post_res, dict) and (post_res.get("error") or post_res.get("success") is False):
                    error_msg = post_res.get("error_msg", post_res.get("error", "Unknown error"))
                    self.logger.error(f"❌ [ORDER REJECTED] API Polymarket merespon dengan error: {error_msg}")
                    return

                self.logger.info(f"✅ [POSTED] Order {asset} berhasil dikirim ke bursa! Respon: {post_res}")

                # Tandai coin ini sudah trading, mulai cooldown 40 menit
                self.last_trade_time[asset] = time()
                
                # Catat ke portfolio
                self.portfolio.record_new_bet(bet_size)
                
                # Dapatkan saldo terbaru (tanpa broadcast double status)
                available_balance = await self.check_balance(broadcast=False)
                
                action_msg = f"📍 Market: {asset} Hourly\n🎯 Posisi: {target_side.upper()}\n💰 Jumlah: ${bet_size:.2f} pUSD\n📡 Sinyal: {signal.signal_type}"
                telegram_msg = self.portfolio.generate_report(available_balance, action_msg)
                self.send_telegram_report(telegram_msg)
            except Exception as e:
                self.logger.error(f"❌ [ORDER FAILED] API Polymarket menolak: {e}")

    async def run_monitoring(self):
        self.logger.info("🤖 Memulai Advanced Brain (Teknik Gustafsson Pro) ...")
        
        # Eksekusi pengecekan Blockchain sebelum looping berjalan
        await self.check_and_approve_pusd()
        # Sinkronisasi portofolio dengan Polymarket
        self.portfolio.sync_with_polymarket(self.proxy_wallet)
        # Cek saldo awal dan kirim report (broadcast=True)
        await self.check_balance(broadcast=True)
        
        while True:
            try:
                # 1. Maintenance & Sinkronisasi Eksternal
                self.garbage_collection()
                fetch_token_ids_from_gamma()
                self.portfolio.sync_with_polymarket(self.proxy_wallet)
                
                # Cek jika bot sedang di-pause karena accumulated_loss_for_pause > $30
                import time
                current_timestamp = time.time()
                if current_timestamp < self.portfolio.stats.get("paused_until", 0.0):
                    time_left_min = (self.portfolio.stats["paused_until"] - current_timestamp) / 60
                    self.logger.warning(f"⏳ BOT DIPAUSE SEMENTARA (Drawdown harian menyentuh limit $30). Sisa waktu pause: {time_left_min:.1f} menit.")
                    self.logger.info("Bot hanya memonitor portofolio. Tidak akan mengeksekusi order.")
                    await asyncio.sleep(60)
                    continue
                
                minute = datetime.now().minute
                if minute <= 15: phase = "OBSERVASI (No Trade)"
                elif minute <= 40: phase = "GOLDEN WINDOW (Trade On)"
                else: phase = "AGRESIF (Trade On + Beta multiplier)"
                
                self.current_phase = phase  # Simpan untuk Telegram report
                
                self.logger.info(f"\n==========================================")
                self.logger.info(f"⏰ Fase Waktu: {phase} | Menit ke-{minute}")
                self.logger.info(f"==========================================")
                
                # 2. Scanning Pasar
                for asset in TARGET_ASSETS:
                    signal = await self.tech_engine.calculate_alpha_signal(asset)
                    if signal:
                        self.logger.info(f"🎯 [TECH SIGNAL DETECTED] {asset} -> {signal.signal_type} ({signal.direction}) | Conf: {signal.confidence}%")
                        if 15 < minute <= 40:
                            self.logger.info(f"✅ [GOLDEN WINDOW] Mengizinkan eksekusi TECH signal untuk {asset}")
                            await self.deploy_smart_order(asset, signal, is_aggressive=False)
                        elif minute > 40:
                            self.logger.info(f"🔒 [AGRESIF PHASE] Menahan TECH signal. Fase Agresif hanya untuk BotScraper!")
                        else:
                            self.logger.info(f"🔒 [LOCKED] Menahan eksekusi karna masih fase Observasi.")

                # 3. Scanning Sinyal Sentimen / Scraper
                scraper_signals = await self.scraper.get_signals()
                for signal in scraper_signals:
                    self.logger.info(f"📰 [SCRAPER SIGNAL DETECTED] {signal.target_asset} -> {signal.signal_type} ({signal.direction}) | Conf: {signal.confidence}%")
                    if minute > 15:
                        is_aggressive = minute > 40
                        fase_name = "AGRESIF" if is_aggressive else "GOLDEN WINDOW"
                        self.logger.info(f"✅ [{fase_name}] Mengizinkan eksekusi SCRAPER signal untuk {signal.target_asset}")
                        await self.deploy_smart_order(signal.target_asset, signal, is_aggressive)
                    else:
                        self.logger.info(f"🔒 [LOCKED] Menahan eksekusi SCRAPER karna masih fase Observasi.")
                            
                # Scanning cepat tiap 45 detik untuk menangkap Latency Breakout!
                # Breakout terjadi sangat cepat, delay 3 menit versi lama akan kalah oleh Bot lain.
                await asyncio.sleep(45)

            except Exception as e:
                self.logger.error(f"🔥 Core Error: {e}")
                await asyncio.sleep(30)

if __name__ == "__main__":
    bot = BotPolymarket()
    asyncio.run(bot.run_monitoring())
