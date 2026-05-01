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
logging.getLogger("py_clob_client").setLevel(logging.CRITICAL)
logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)

try:
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client_v2.constants import ZERO_ADDRESS
        PY_CLOB_CLIENT_AVAILABLE = True
        PY_CLOB_CLIENT_VERSION = 2
    except ImportError:
        from py_clob_client import ClobClient
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.constants import ZERO_ADDRESS
        PY_CLOB_CLIENT_AVAILABLE = True
        PY_CLOB_CLIENT_VERSION = 1
except ImportError:
    PY_CLOB_CLIENT_AVAILABLE = False
    PY_CLOB_CLIENT_VERSION = 0
    logging.warning("py-clob-client is missing. Trading real mode will strictly simulate or crash.")

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

TARGET_ASSETS = ['BTC', 'ETH'] # Diubah sementara hanya BTC dan ETH sesuai request
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

POLYMARKET_TOKEN_IDS = {}
_GAMMA_LAST_FETCH: Optional[datetime] = None
_GAMMA_CACHE_MINUTES = 15

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
        
        # Target assets disamakan dengan Bot Utama (sementara hanya BTC dan ETH)
        self.target_assets = {
            'BTC': [r'\bbitcoin\b', r'\bbtc\b'],
            'ETH': [r'\bethereum\b', r'\beth\b']
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
        """Menghitung momentum dan volatilitas menggunakan Multi-EMA pada 5m untuk proyeksi 1 jam"""
        ohlcv = await self.get_ohlcv(asset, '5m', 100)
        if len(ohlcv) < 50: return None
        
        closes = [c[4] for c in ohlcv]
        
        current_price = closes[-1]
        
        ema9 = calc_ema(closes, 9)
        ema21 = calc_ema(closes, 21)
        ema50 = calc_ema(closes, 50)
        
        ema9_prev = calc_ema(closes[:-1], 9)
        ema21_prev = calc_ema(closes[:-1], 21)
        
        rsi_current = calc_rsi(closes, 14)
        rsi_prev = calc_rsi(closes[:-1], 14)
        
        # Definisi Trend (Semua EMA sejajar dan harga inline)
        trend_up = current_price > ema9 and ema9 > ema21 and ema21 > ema50
        trend_down = current_price < ema9 and ema9 < ema21 and ema21 < ema50
        
        # Definisi Ekspansi Momentum (Jarak EMA9 dan EMA21 semakin melebar)
        gap_current = ema9 - ema21
        gap_prev = ema9_prev - ema21_prev
        
        momentum_up = gap_current > gap_prev and gap_current > 0
        momentum_down = gap_current < gap_prev and gap_current < 0
        
        # RSI Reversals Tepat Waktu (Memantul dari Overbought/Oversold dan memotong EMA9)
        rsi_bottoming = rsi_prev <= 35 and rsi_current > 35
        rsi_topping = rsi_prev >= 65 and rsi_current < 65
        
        # Skenario 1: Strong Uptrend + Momentum Bertambah
        if trend_up and momentum_up and rsi_current < 70:
            return SignalData("STRONG_UPTREND", asset, "BUY_YES", 25.0, 65.0, "Tech_Alpha")
            
        # Skenario 2: Strong Downtrend + Momentum Bertambah
        if trend_down and momentum_down and rsi_current > 30:
            return SignalData("STRONG_DOWNTREND", asset, "BUY_NO", 25.0, 65.0, "Tech_Alpha")
            
        # Skenario 3: Oversold Reversal & Price Breaks EMA9 (Mulai Naik!)
        if rsi_bottoming and current_price > ema9:
            return SignalData("OVERSOLD_REVERSAL_CONFIRMED", asset, "BUY_YES", 15.0, 60.0, "Tech_Alpha")
            
        # Skenario 4: Overbought Rejection & Price Breaks EMA9 (Mulai Turun!)
        if rsi_topping and current_price < ema9:
            return SignalData("OVERBOUGHT_REJECT_CONFIRMED", asset, "BUY_NO", 15.0, 60.0, "Tech_Alpha")

        return None

    async def get_order_book_imbalance(self, symbol="BTC/USDT"):
        """Menghitung rasio dominasi antara pembeli dan penjual (L2 Data) - Short Term"""
        try:
            # Menggunakan asyncio.to_thread agar tidak blocking
            ob = await asyncio.to_thread(self.binance.fetch_order_book, symbol, limit=20)
            bid_vol = sum([b[1] for b in ob['bids']])
            ask_vol = sum([a[1] for a in ob['asks']])
            
            if bid_vol + ask_vol == 0:
                return 0.0
            imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
            return imbalance
        except Exception as e:
            logging.error(f"Error OBI: {e}")
            return 0.0

    async def get_deep_orderbook_imbalance(self, symbol="BTC/USDT", depth_percentage: float = 0.02) -> float:
        """
        Menganalisa Order Book mendalam untuk time frame 1 Jam.
        Mencari dinding buy/sell (walls) besar sampai kedalaman 2% dari harga rata-rata bersangkutan.
        """
        try:
            ob = await asyncio.to_thread(self.binance.fetch_order_book, symbol, limit=1000)
            if not ob['bids'] or not ob['asks']:
                return 0.0
                
            current_price = (ob['bids'][0][0] + ob['asks'][0][0]) / 2
            
            # Kita hanya peduli volume raksasa di sekitar +- 2%
            min_price = current_price * (1 - depth_percentage)
            max_price = current_price * (1 + depth_percentage)
            
            bid_vol = sum([b[1] for b in ob['bids'] if b[0] >= min_price])
            ask_vol = sum([a[1] for a in ob['asks'] if a[0] <= max_price])
            
            if bid_vol + ask_vol == 0:
                return 0.0
            imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
            return imbalance
        except Exception as e:
            logging.error(f"Error Deep OBI: {e}")
            return 0.0

    async def get_vwap_score(self, symbol="BTC/USDT"):
        """Mendeteksi Mean Reversion berdasarkan Volume Weighted Average Price"""
        try:
            # Mengambil data 1 jam terakhir (60 menit)
            ohlcv = await self.get_ohlcv(symbol.replace("/USDT", ""), tf='1m', limit=60)
            if not ohlcv:
                return 0, 0, 0
            
            # VWAP = sum(Price * Volume) / sum(Volume)
            total_pv = sum([(o[4] * o[5]) for o in ohlcv]) # Price * Volume
            total_v = sum([o[5] for o in ohlcv])          # Total Volume
            
            if total_v == 0:
                return 0, 0, 0
                
            vwap = total_pv / total_v
            current_price = ohlcv[-1][4] # Harga closing terakhir
            
            # Deviasi harga terhadap VWAP (dalam persentase)
            deviation = (current_price - vwap) / vwap
            return current_price, vwap, deviation
        except Exception as e:
            logging.error(f"Error VWAP: {e}")
            return 0, 0, 0

    async def get_btc_momentum(self) -> float:
        """
        Menganalisa momentum BTC dalam 5 menit terakhir sebagai Leading Indicator.
        Fakta: Pergerakan altcoin sering memiliki 'delay' dibandingkan pergerakan BTC.
        Kita dapat memanfaatkannya untuk memprediksi arah altcoin sesaat sebelum bereaksi.
        """
        try:
            ohlcv = await self.get_ohlcv("BTC", tf='1m', limit=5)
            if not ohlcv or len(ohlcv) < 5:
                return 0.0
            
            # Harga close 5 menit lalu vs harga close sekarang
            start_price = ohlcv[0][4]
            current_price = ohlcv[-1][4]
            
            pct_change = (current_price - start_price) / start_price * 100
            return pct_change
        except Exception as e:
            logging.error(f"Error BTC Momentum: {e}")
            return 0.0

    async def get_1h_open_distance(self, asset: str) -> float:
        """Menghitung jarak persentase harga saat ini dari harga Open candle 1H berjalan."""
        try:
            # Ambil data OHLCV 1 jam, limit 1 untuk candle jam ini yg sedang berjalan
            ohlcv = await self.get_ohlcv(asset, tf='1h', limit=1)
            if not ohlcv or len(ohlcv) < 1:
                return 0.0
            
            open_price = ohlcv[0][1] # Index 1 adalah Open
            current_price = ohlcv[0][4] # Index 4 adalah Close (Current)
            
            if open_price == 0:
                return 0.0
                
            pct_change = (current_price - open_price) / open_price * 100
            return pct_change
        except Exception as e:
            logging.error(f"Error 1H Open Distance: {e}")
            return 0.0

    async def get_volume_surge(self, asset: str) -> float:
        """Mengukur lonjakan volume pada timeframe 5m (agregasi 10 menit) untuk prediksi 1h"""
        try:
            ohlcv = await self.get_ohlcv(asset, tf='5m', limit=30)
            if not ohlcv or len(ohlcv) < 20:
                return 0.0
            
            volumes = [c[5] for c in ohlcv]
            # Volume 10 menit terakhir (2 candle x 5 menit)
            recent_10m_vol = sum(volumes[-2:])
            # Rata-rata volume 10 menitan dari 18 candle sebelumnya (9 blok x 10 menit)
            if sum(volumes[-20:-2]) > 0:
                previous_vols = sum(volumes[-20:-2]) / 9
            else:
                previous_vols = 1
                
            vol_surge = recent_10m_vol / previous_vols
            return vol_surge
        except Exception as e:
            logging.error(f"Error Vol Surge: {e}")
            return 0.0

    async def analyze_market_with_triggers(self, asset: str) -> Optional[SignalData]:
        # 1. Ambil Sinyal Dasar (Teknikal/Momentum yang sudah ada)
        base_signal = await self.calculate_alpha_signal(asset)
        
        # 1.b TARGETING DELAY: BTC LEAD CORRELATION EDGE
        btc_mom = 0.0
        if asset != "BTC":
            btc_mom = await self.get_btc_momentum()
            
            # Jika tidak ada sinyal teknis, tapi BTC bergerak SUPER liar (>0.4% dalam 5 menit)
            # Buat sinyal spontan untuk altcoin sebelum bot lain mendeteksi!
            if not base_signal:
                if btc_mom > 0.4:
                    logging.info(f"🚀 [BTC LEADER] BTC Surge Tajam (+{btc_mom:.2f}%). Menginisiasi sinyal UP preemptif untuk {asset}!")
                    base_signal = SignalData("BTC_CORRELATION_UP", asset, "BUY_YES", 20.0, 65.0, "Correlation_Edge")
                elif btc_mom < -0.4:
                    logging.info(f"🩸 [BTC LEADER] BTC Dump Tajam ({btc_mom:.2f}%). Menginisiasi sinyal DOWN preemptif untuk {asset}!")
                    base_signal = SignalData("BTC_CORRELATION_DOWN", asset, "BUY_NO", 20.0, 65.0, "Correlation_Edge")

        if not base_signal:
            return None
            
        # 1.c TAHAP FILTERING: 1H OPEN DISTANCE
        dist_1h = await self.get_1h_open_distance(asset)
        if base_signal.direction == "BUY_YES" and dist_1h < -0.4:
            logging.warning(f"⚠️ [1H OPEN REJECT] {asset} sudah turun {dist_1h:.2f}% dari open 1h. Membutuhkan reverse ekstrem untuk Win UP. Signal dibatalkan.")
            return None
        elif base_signal.direction == "BUY_NO" and dist_1h > 0.4:
            logging.warning(f"⚠️ [1H OPEN REJECT] {asset} sudah naik {dist_1h:.2f}% dari open 1h. Membutuhkan reverse ekstrem untuk Win DOWN. Signal dibatalkan.")
            return None
            
        symbol = f"{asset}/USDT"
        
        # 2. TRIGGER: Order Book Imbalance (Composite Short & Deep untuk Filter 1 Jam)
        short_obi = await self.get_order_book_imbalance(symbol)
        deep_obi = await self.get_deep_orderbook_imbalance(symbol, depth_percentage=0.015)
        # Deep OBI (1.5% distance) memiliki prioritas bobot lebih besar untuk keberlangsungan tren 1 jam
        obi_ratio = (short_obi * 0.3) + (deep_obi * 0.7)
        
        obi_boost = 0
        if obi_ratio > 0.15:
            if base_signal.direction == "BUY_YES":
                obi_boost = 15
                logging.info(f"🔥 [TRIGGER OBI] Deep Bids Dominan ({obi_ratio:.2f}) pada {asset}. Boosting UP signal.")
            else:
                obi_boost = -15
                logging.info(f"⚠️ [TRIGGER OBI] Deep Bids Dominan ({obi_ratio:.2f}) pada {asset} bertentangan. Melemahkan DOWN signal.")
        elif obi_ratio < -0.15:
            if base_signal.direction == "BUY_NO":
                obi_boost = 15
                logging.info(f"📉 [TRIGGER OBI] Deep Asks Dominan ({obi_ratio:.2f}) pada {asset}. Boosting DOWN signal.")
            else:
                obi_boost = -15
                logging.info(f"⚠️ [TRIGGER OBI] Deep Asks Dominan ({obi_ratio:.2f}) pada {asset} bertentangan. Melemahkan UP signal.")

        # 3. TRIGGER: VWAP Mean Reversion
        price, vwap, dev = await self.get_vwap_score(symbol)
        vwap_filter = True
        
        # Logika Mean Reversion: 
        # Jika harga sudah 2.0% di atas VWAP, hati-hati "Down" balik ke rata-rata
        if dev > 0.02 and base_signal.direction == "BUY_YES":
            vwap_filter = False
            logging.warning(f"⚠️ [VWAP ALERT] Harga {asset} terlalu jauh di atas VWAP ({dev:.2%}). Membatalkan UP (Potensi Reversion).")
        elif dev < -0.02 and base_signal.direction == "BUY_NO":
            vwap_filter = False
            logging.warning(f"⚠️ [VWAP ALERT] Harga {asset} terlalu jauh di bawah VWAP ({dev:.2%}). Membatalkan DOWN (Potensi Reversion).")
        
        # 4. TRIGGER: KONFIRMASI BTC SEBAGAI FILTER
        btc_boost = 0
        if asset != "BTC":
            # Jika BTC naik dengan meyakinkan
            if btc_mom > 0.2:
                if base_signal.direction == "BUY_YES":
                    btc_boost = 15
                    logging.info(f"📈 [BTC EDGE] BTC terkonfirmasi Uptrend (+{btc_mom:.2f}%). Memperkuat momentum UP {asset}.")
                elif base_signal.direction == "BUY_NO":
                    vwap_filter = False
                    logging.warning(f"⚠️ [BTC EDGE] BTC sedang naik (+{btc_mom:.2f}%). Membatalkan sentimen DOWN {asset} untuk menghindari loss.")
            # Jika BTC turun dengan meyakinkan
            elif btc_mom < -0.2:
                if base_signal.direction == "BUY_NO":
                    btc_boost = 15
                    logging.info(f"📉 [BTC EDGE] BTC terkonfirmasi Downtrend ({btc_mom:.2f}%). Memperkuat momentum DOWN {asset}.")
                elif base_signal.direction == "BUY_YES":
                    vwap_filter = False
                    logging.warning(f"⚠️ [BTC EDGE] BTC sedang turun ({btc_mom:.2f}%). Membatalkan sentimen UP {asset} untuk menghindari loss.")

        # 5. TRIGGER: Volume Surge 10M (Konfirmasi Momentum Cepat)
        vol_surge = await self.get_volume_surge(asset)
        vol_boost = 0
        if vol_surge > 1.25:
            vol_boost = 15
            logging.info(f"🌊 [VOLUME SURGE] Lonjakan volume 10m terdeteksi ({vol_surge:.1f}x) pada {asset}. Filter probabilitas 1h aktif.")
            
        # 6. Final Decision
        final_confidence = base_signal.confidence + obi_boost + btc_boost + vol_boost
        
        # Sinyal dasar bernilai minimum 60 (reversal) atau 65 (strong trend).
        # Harus minimal 85% untuk dieksekusi (membutuhkan setidaknya 2 dari 3 konfirmasi trigger positif tanpa bantahan VWAP)
        if vwap_filter and final_confidence >= 85:
            base_signal.confidence = final_confidence
            return base_signal
        else:
            if vwap_filter and final_confidence > 60:
                logging.info(f"⏭️ [SKIP] {asset} Sinyal terlalu lemah (Confidence: {final_confidence}%). Di bawah syarat 85%.")
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
        
        if self.private_key and PY_CLOB_CLIENT_AVAILABLE:
            sig_type = 2 if (self.proxy_wallet and self.wallet and self.proxy_wallet.lower() != self.wallet.lower()) else 1
            self.clob_client = ClobClient(CLOB_HOST, 137, self.private_key, signature_type=sig_type, funder=self.proxy_wallet)
            
            # [CRITICAL UPDATE]: Add headers to Bypass Cloudflare 403 Forbidden
            if hasattr(self.clob_client, 'session'):
                self.clob_client.session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': 'https://polymarket.com',
                    'Referer': 'https://polymarket.com/',
                    'Accept-Language': 'en-US,en;q=0.9'
                })
            
            api_key = (API_CONFIG['polymarket'].get('api_key') or os.getenv('POLYMARKET_API_KEY') or "").strip("'\"")
            api_secret = (API_CONFIG['polymarket'].get('api_secret') or os.getenv('POLYMARKET_API_SECRET') or "").strip("'\"")
            api_passphrase = (API_CONFIG['polymarket'].get('passphrase') or os.getenv('POLYMARKET_API_PASSPHRASE') or os.getenv('POLYMARKET_PASSPHRASE') or "").strip("'\"")
            
            # Jika user maksa input API key namun salah (401), berikan fallback untuk regenerate
            if api_key and api_secret and api_passphrase:
                try:
                    from py_clob_client_v2.clob_types import ApiCreds
                    self.clob_client.set_api_creds(ApiCreds(api_key, api_secret, api_passphrase))
                    self.logger.info("🔑 [AUTH] Menggunakan API Key spesifik dari .env (v2)")
                except ImportError:
                    from py_clob_client.clob_types import ApiCreds
                    self.clob_client.set_api_creds(ApiCreds(api_key, api_secret, api_passphrase))
                    self.logger.info("🔑 [AUTH] Menggunakan API Key spesifik dari .env (v1)")
            else:
                self.logger.info("🔑 [AUTH] Kredensial tidak lengkap di .env, generate API otomatis...")
                
                # Coba derive / create API key agar TIDAK 401
                try:
                    creds = None
                    
                    # Coba metode pembuatan creds (dgn urutan TERBARU dahulu)
                    for method_name in ["derive_api_key", "create_or_derive_api_creds", "create_or_derive_api_key", "create_api_key"]:
                        if hasattr(self.clob_client, method_name):
                            try:
                                creds = getattr(self.clob_client, method_name)()
                                if creds:
                                    break
                            except Exception as e:
                                self.logger.warning(f"ℹ️ Generate via {method_name} diblokir/gagal: {e}")
                    
                    if creds:
                        self.clob_client.set_api_creds(creds)
                        self.logger.info("✅ [AUTH] Berhasil mendapatkan API credentials secara mandiri (Self-Generated).")
                    else:
                        self.logger.warning("🛑 [AUTH] Gagal mendapatkan API key secara mandiri & ENV kosong. Bot akan berhenti.")
                        self.clob_client = None
                except Exception as e:
                     self.logger.error(f"Gagal set API Creds secara otomatis: {e}")
        else:
            raise Exception("🛑 STOP: MODE REAL TRADE AKTIF! Bot dihentikan karena 'Private Key' atau library 'py_clob_client' tidak ditemukan.")

    async def check_and_approve_pusd(self):
        """Pengecekan Allowance Polymarket Wajib Untuk Real Trade"""
        self.logger.info("🔐 [Web3] Melakukan verifikasi pUSD Allowance CTF Exchange V2...")
        if not self.wallet or not self.private_key: return
        # Rotating RPC list
        rpc_list = [
            'https://polygon-rpc.com',
            'https://1rpc.io/matic',
            'https://polygon.llamarpc.com',
            'https://rpc-mainnet.maticvigil.com',
            'https://rpc.ankr.com/polygon'
        ]
        
        w3 = None
        for rpc in rpc_list:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
                if w3.is_connected():
                    self.logger.info(f"✅ RPC Connected: {rpc}")
                    break
            except Exception as e:
                self.logger.warning(f"⚠️ RPC gagal: {rpc} - {e}")
                continue
        
        if not w3 or not w3.is_connected():
            self.logger.error("❌ Semua RPC gagal terhubung! Approval di-skip sementara.")
            return
        
        target_wallet = w3.to_checksum_address(self.wallet)
        ctf_exchange = w3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B") # CTF Exchange V2
        pusd_address = w3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB") # pUSD
        
        erc20_abi = [
            {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
            {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"}
        ]
        
        try:
            contract = w3.eth.contract(address=pusd_address, abi=erc20_abi)
            allowance = contract.functions.allowance(target_wallet, ctf_exchange).call()
            
            if allowance < (50 * 10**6): # Jika izin di bawah $50
                self.logger.warning("⚠️ [pUSD] Allowance rendah/kosong. Meminta Auto-Approve Tak Terhingga (MAX)...")
                nonce = w3.eth.get_transaction_count(target_wallet)
                tx = contract.functions.approve(ctf_exchange, 2**256 - 1).build_transaction({
                    'chainId': 137, 'gas': 100000,
                    'maxFeePerGas': w3.to_wei('50', 'gwei'),
                    'maxPriorityFeePerGas': w3.to_wei('50', 'gwei'),
                    'nonce': nonce,
                })
                raw_tx = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
                tx_hash = w3.eth.send_raw_transaction(raw_tx.rawTransaction)
                self.logger.info(f"⏳ [pUSD] Approve Tx Has Terkirim: {tx_hash.hex()}. Menunggu blok konfirmasi...")
                w3.eth.wait_for_transaction_receipt(tx_hash)
                self.logger.info("✅ [pUSD] Approve Selesai. Dompet siap untuk REAL TRADE tanpa rejeksi kontrak!")
            else:
                self.logger.info(f"✅ [pUSD] Allowance aman tervalidasi: {allowance/10**6:,.2f} pUSD.")
        except Exception as e:
            self.logger.error(f"❌ [pUSD] Cek allowance error: {e}")

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
            from web3 import Web3
            
            rpc_list = [
                'https://polygon-rpc.com',
                'https://1rpc.io/matic',
                'https://polygon.llamarpc.com',
                'https://rpc-mainnet.maticvigil.com',
                'https://rpc.ankr.com/polygon'
            ]
            
            w3 = None
            for rpc in rpc_list:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
                    if w3.is_connected():
                        break
                except Exception:
                    pass
            
            if not w3 or not w3.is_connected():
                self.logger.error("❌ Semua RPC Polygon gagal saat cek saldo.")
                return 0.0
            
            proxy_wallet = self.proxy_wallet if self.proxy_wallet else '0xe6A9bE1740d29F59D6e00C9a08Ee8eacBC299D0b'
            
            # Cek pUSD Token (CTF Exchange V2 Collateral)
            pusd_addr = '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'
            erc20_abi = '[{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'
            contract = w3.eth.contract(address=w3.to_checksum_address(pusd_addr), abi=json.loads(erc20_abi))
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
            
        if signal.confidence < 85.0:
            self.logger.warning(f"🚫 [Skip] {asset} Confidence ({signal.confidence:.1f}%) di bawah threshold 85%.")
            return
            
        # ==========================================
        # HALf-KELLY CRITERION POSITION SIZING
        # ==========================================
        # p = probabilitas menang (dari confidence)
        p = signal.confidence / 100.0
        # b = net odds murni (keuntungan per 1 unit taruhan; polymarket bernilai 1.0 pada win. Jadi profit = (1 - odds_price) / odds_price
        b = (1.0 - odds_price) / odds_price if odds_price > 0 else 0
        
        kelly_fraction = 0
        if b > 0:
            kelly_fraction = p - ((1.0 - p) / b)
            
        if kelly_fraction <= 0:
            self.logger.warning(f"🚫 [Skip] {asset} Expected Value negatif (Kelly {kelly_fraction:.2f}). Skip.")
            return
            
        # Gunakan Half-Kelly untuk mengurangi volatilitas drawdown (DITANGGUHKAN SEMENTARA)
        # half_kelly = kelly_fraction * 0.5
        
        # Batasi ukuran taruhan maksimum 8% dari portofolio (sesuai spesifikasi gambar) (DITANGGUHKAN SEMENTARA)
        # position_size_pct = min(half_kelly, 0.08)
        
        # MENGGUNAKAN FIXED 5 SHARES (SEMENTARA)
        # Membutuhkan modal sebesar harga * jumlah share
        bet_size = 5.0 * odds_price
        
        # Cek saldo untuk menghitung bet size asli
        balance_pusd = await self.check_balance(broadcast=False)
        if balance_pusd > 0.0 and bet_size > balance_pusd:
            self.logger.warning(f"⚠️ [INSUFFICIENT BALANCE] Saldo {balance_pusd:.2f} kurang dari kebutuhan {bet_size:.2f} untuk 5 shares. Menggunakan sisa saldo.")
            bet_size = balance_pusd
            # Jika saldo diturunkan tetapi minimal saham tidak terpenuhi, order mungkin akan ditolak Polymarket
                
        self.logger.info(f"💎 [FIXED SIZING] P:{p:.2f} B:{b:.2f} | Override Kelly -> Menggunakan 5 Shares | Size: ${bet_size:.2f} pUSD")

        self.logger.info(f"📈 [EXECUTION] Mengirim order: {asset} {target_side.upper()} | Harga: ${odds_price:.3f} | Jumlah: ${bet_size:.2f} pUSD | Sinyal: {signal.signal_type}")
        
        if self.clob_client:
            try:
                # Convert size to share amount (pUSD / price)
                share_size = round(bet_size / odds_price, 4)
                
                order_kwargs = dict(
                    token_id=token_id,
                    price=odds_price,
                    size=share_size,
                    side="BUY",
                )
                if PY_CLOB_CLIENT_VERSION < 2:
                    order_kwargs.update(dict(fee_rate_bps=0, nonce=0, expiration=0, taker=ZERO_ADDRESS))
                elif hasattr(self, 'builder_code') and self.builder_code:
                    order_kwargs["builder_code"] = self.builder_code

                args = OrderArgs(**order_kwargs)
                
                if PY_CLOB_CLIENT_VERSION >= 2 and hasattr(self.clob_client, "create_and_post_order"):
                    try:
                        from py_clob_client_v2.clob_types import OrderType
                        post_res = self.clob_client.create_and_post_order(
                            args,
                            PartialCreateOrderOptions(neg_risk=None),
                            order_type=OrderType.GTC,
                        )
                    except ImportError:
                        signed_order = self.clob_client.create_order(args, PartialCreateOrderOptions(neg_risk=None))
                        post_res = self.clob_client.post_order(signed_order)
                else:
                    signed_order = self.clob_client.create_order(args, PartialCreateOrderOptions(neg_risk=None))
                    # Baris krusial untuk mengirim order ke server Polymarket:
                    try:
                        if PY_CLOB_CLIENT_VERSION >= 2:
                            from py_clob_client_v2.clob_types import OrderType
                            post_res = self.clob_client.post_order(signed_order, OrderType.GTC)
                        else:
                            post_res = self.clob_client.post_order(signed_order)
                    except Exception:
                        post_res = self.clob_client.post_order(signed_order)
                
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
                err_msg = str(e)
                self.logger.error(f"❌ [ORDER FAILED] API Polymarket menolak: {err_msg}")
                if "401" in err_msg or "Unauthorized" in err_msg:
                    self.logger.error("🛑 PERHATIAN: 401 Unauthorized! API Key di .env DITOLAK oleh Polymarket.")
                    self.logger.error("👉 SOLUSI: Kemungkinan API Key ini adalah L1 key dan bukan L2 CLOB key, atau terkait wallet yang salah.")
                    self.logger.error("✨ SILAKAN KOSONGKAN nilai POLYMARKET_API_KEY, SECRET, dan PASSPHRASE di .env, lalu restart bot agar bot bisa men-generate (derive) credential L2 baru secara otomatis dengan aman.")
                    self.logger.error("Bot akan berhenti mengirim order sampai mendapat API Key yang valid.")
                    self.clob_client = None # Matikan Clob Client agar fallback ke Paper Trade di iterasi berikutnya

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
                    signal = await self.tech_engine.analyze_market_with_triggers(asset)
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
