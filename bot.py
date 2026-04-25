root@157.15.40.59's password: 
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

try:
    from py_clob_client import ClobClient
    PY_CLOB_CLIENT_AVAILABLE = True
except ImportError:
    PY_CLOB_CLIENT_AVAILABLE = False
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
        'wallet_address': os.getenv('POLYMARKET_WALLET_ADDRESS')
    },
    'telegram': {
        'token': os.getenv('TELEGRAM_BOT_TOKEN'),
        'chat_id': os.getenv('TELEGRAM_CHAT_ID')
    }
}

TARGET_ASSETS = ['BTC', 'ETH', 'SOL', 'XRP']
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
def _extract_token_ids(market: dict) -> Tuple[Optional[str], Optional[str]]:
    # Support both old API format (tokens array) and new API format (clobTokenIds + outcomes)
    tokens = market.get('tokens', [])
    yes_id, no_id = None, None
    
    if tokens:
        # Old API format
        for t in tokens:
            outcome = str(t.get('outcome', '')).strip().lower()
            tid = str(t.get('token_id', '')).strip()
            if outcome == 'yes': yes_id = tid
            elif outcome == 'no': no_id = tid
    else:
        # New API format: clobTokenIds and outcomes
        clob_ids = market.get('clobTokenIds', '')
        outcomes = market.get('outcomes', '')
        
        if clob_ids and outcomes:
            try:
                # Parse JSON strings
                import json
                ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                outs = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                
                if len(ids) >= 2 and len(outs) >= 2:
                    # Map first ID to first outcome, second ID to second outcome
                    for i, outcome in enumerate(outs):
                        if i < len(ids):
                            outcome_lower = str(outcome).strip().lower()
                            if outcome_lower == 'yes':
                                yes_id = str(ids[i]).strip()
                            elif outcome_lower == 'no':
                                no_id = str(ids[i]).strip()
                            # Fallback for Up/Down markets
                            elif outcome_lower == 'up':
                                yes_id = str(ids[i]).strip()
                            elif outcome_lower == 'down':
                                no_id = str(ids[i]).strip()
            except Exception as e:
                # Fallback: try simple parsing
                pass
    
    return yes_id, no_id

def fetch_token_ids_from_gamma(force: bool = False):
    global _GAMMA_LAST_FETCH
    now_utc = datetime.utcnow()

    if not force and _GAMMA_LAST_FETCH is not None:
        if (now_utc - _GAMMA_LAST_FETCH).total_seconds() / 60 < _GAMMA_CACHE_MINUTES:
            return bool(POLYMARKET_TOKEN_IDS)

    logging.info("[Discovery] Scanning Gamma API for Volatile Markets (1-72h)...")
    try:
        req = requests.get(f"{GAMMA_HOST}/markets?limit=500&active=true&closed=false&order=createdAt&ascending=false", timeout=15)
        req.raise_for_status()
        markets = req.json()
        
        best_markets = {}
        for m in markets:
            if not m.get('active', True) or m.get('closed', False):
                continue
            question = m.get('question', '').lower()
            
            # Identify crypto targets in question
            matched_asset = None
            asset_keywords = {
                'BTC': ['bitcoin', 'btc'],
                'ETH': ['ethereum', 'eth'],
                'SOL': ['solana', 'sol'],
                'XRP': ['ripple', 'xrp'],
                'BNB': ['binance coin', 'bnb'],
                'DOGE': ['dogecoin', 'doge']
            }
            for asset_key, keywords in asset_keywords.items():
                if asset_key in TARGET_ASSETS and any(kw in question for kw in keywords):
                    matched_asset = asset_key
                    break
            if not matched_asset: continue

            # Focus only on Price Prediction markets - ONLY 'Up or Down' allowed
            if "up or down" not in question or "hourly" not in question:
                continue

            # Time validation
            end_str = m.get('endDate', '')
            if not end_str: continue
            clean_str = end_str[:-1] if end_str.endswith('Z') else end_str
            clean_str = clean_str.split('+')[0]
            try:
                end_dt = datetime.fromisoformat(clean_str)
                hours_left = (end_dt - now_utc).total_seconds() / 3600
                if hours_left < 0.08 or hours_left > 72: continue
            except: continue

            # Validate IDs
            yes_id, no_id = _extract_token_ids(m)
            if not (yes_id and no_id): continue

            # Keep the shortest timeframe valid market for latency sniping
            if matched_asset not in best_markets or hours_left < best_markets[matched_asset]['hours']:
                best_markets[matched_asset] = {
                    'yes': yes_id, 'no': no_id, 'hours': hours_left, 'closes_at': end_str,
                    'title': m.get('question')
                }

        POLYMARKET_TOKEN_IDS.clear()
        for k, v in best_markets.items():
            POLYMARKET_TOKEN_IDS[k] = v
            logging.info(f"🟢 [Locked Market] {k}: {v['title'][:40]}... ({v['hours']:.1f}h left)")
        
        _GAMMA_LAST_FETCH = now_utc
        return True
    except Exception as e:
        logging.error(f"[Discovery] Fetch error: {e}")
        return False

# ==========================================
# 5. CORE ENGINES: TECHNICAL, CORRELATION, CLOB
# ==========================================
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
        """Menghitung momentum dan volatilitas (Latency Sniping Logic)"""
        ohlcv = await self.ohlcv(asset, '1m', 100)
        if len(ohlcv) < 50: return None
        
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]
        
        current_price = closes[-1]
        ema50 = calc_ema(closes, 50)
        rsi = calc_rsi(closes, 14)
        
        # Deteksi Lonjakan Volume 3 Menit Terakhir (Smart Money masuk)
        recent_vol = sum(volumes[-3:]) / 3
        avg_vol = sum(volumes[-20:-3]) / 17 if sum(volumes[-20:-3]) else 1
        vol_surge = recent_vol / avg_vol
        
        # Momentum Kalkulus (The Edge)
        trend_up = current_price > ema50
        trend_down = current_price < ema50
        
        # Skenario 1: Latency Sniping UP
        if trend_up and vol_surge > 2.5 and rsi < 70:
            return SignalData("MOMENTUM_SNIPE_UP", asset, "BUY_YES", 25.0, 85.0, "Tech_Alpha")
            
        # Skenario 2: Latency Sniping DOWN (Panic Sell)
        if trend_down and vol_surge > 2.5 and rsi > 30:
            return SignalData("MOMENTUM_SNIPE_DOWN", asset, "BUY_NO", 25.0, 85.0, "Tech_Alpha")
            
        # Skenario 3: Reversal / Recovery
        if rsi < 20 and vol_surge > 1.5:
            return SignalData("OVERSOLD_REVERSAL", asset, "BUY_YES", 15.0, 70.0, "Tech_Alpha")
            
        if rsi > 80 and vol_surge > 1.5:
            return SignalData("OVERBOUGHT_REJECT", asset, "BUY_NO", 15.0, 70.0, "Tech_Alpha")

        return None
        
    async def ohlcv(self, *args):
        return await self.get_ohlcv(*args)

class BotPolymarket:
    def __init__(self):
        self.logger = logging.getLogger("PolymarketMaster")
        self.tech_engine = AdvancedCollateralEngine()
        self.private_key = API_CONFIG['polymarket']['private_key']
        self.wallet = API_CONFIG['polymarket']['wallet_address']
        
        if self.private_key and PY_CLOB_CLIENT_AVAILABLE:
            self.clob_client = ClobClient(CLOB_HOST, 137, self.private_key, signature_type=1, funder="0xe6A9bE1740d29F59D6e00C9a08Ee8eacBC299D0b")
            try:
                 self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_creds())
            except Exception as e:
                 self.logger.error(f"Gagal set API Creds: {e}")
        else:
            raise Exception("🛑 STOP: MODE REAL TRADE AKTIF! Bot dihentikan karena 'Private Key' atau library 'py_clob_client' tidak ditemukan.")

    async def check_and_approve_usdc(self):
        """Pengecekan Allowance Polymarket Wajib Untuk Real Trade"""
        self.logger.info("🔐 [Web3] Melakukan verifikasi USDC Allowance CTF Exchange...")
        if not self.wallet or not self.private_key: return
        # Rotating RPC list
        rpc_list = [
            'https://polygon-rpc.com',
            'https://1rpc.io/matic',
            'https://polygon.llamarpc.com',
            'https://rpc-mainnet.maticvigil.com',
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
        ctf_exchange = w3.to_checksum_address("0x4bFB41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
        usdc_address = w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174") 
        
        erc20_abi = [
            {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
            {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"}
        ]
        
        try:
            contract = w3.eth.contract(address=usdc_address, abi=erc20_abi)
            allowance = contract.functions.allowance(target_wallet, ctf_exchange).call()
            
            if allowance < (50 * 10**6): # Jika izin di bawah $50
                self.logger.warning("⚠️ [USDC] Allowance rendah/kosong. Meminta Auto-Approve Tak Terhingga (MAX)...")
                nonce = w3.eth.get_transaction_count(target_wallet)
                tx = contract.functions.approve(ctf_exchange, 2**256 - 1).build_transaction({
                    'chainId': 137, 'gas': 100000,
                    'maxFeePerGas': w3.to_wei('50', 'gwei'),
                    'maxPriorityFeePerGas': w3.to_wei('50', 'gwei'),
                    'nonce': nonce,
                })
                raw_tx = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
                tx_hash = w3.eth.send_raw_transaction(raw_tx.rawTransaction)
                self.logger.info(f"⏳ [USDC] Approve Tx Has Terkirim: {tx_hash.hex()}. Menunggu blok konfirmasi...")
                w3.eth.wait_for_transaction_receipt(tx_hash)
                self.logger.info("✅ [USDC] Approve Selesai. Dompet siap untuk REAL TRADE tanpa rejeksi kontrak!")
            else:
                self.logger.info(f"✅ [USDC] Allowance aman tervalidasi: {allowance/10**6:,.2f} USDC.")
        except Exception as e:
            self.logger.error(f"❌ [USDC] Cek allowance error: {e}")

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

    async def check_balance(self):
        """Cek saldo USDC di Proxy Wallet"""
        try:
            from web3 import Web3
            rpc = 'https://polygon-rpc.com'
            w3 = Web3(Web3.HTTPProvider(rpc))
            if not w3.is_connected():
                return 0.0
            
            proxy_wallet = '0xe6A9bE1740d29F59D6e00C9a08Ee8eacBC299D0b'
            usdc_addr = '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359'
            
            erc20_abi = '[{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'
            contract = w3.eth.contract(address=usdc_addr, abi=json.loads(erc20_abi))
            balance = contract.functions.balanceOf(proxy_wallet).call()
            balance_usdc = balance / 10**6
            
            status_msg = f"🤖 Status Bot Polymarket 📍 Proxy: {proxy_wallet[:10]}...{proxy_wallet[-4:]} 💰 Saldo: {balance_usdc:,.2f} USDC 📊 Status: {getattr(self, 'current_phase', 'Starting')}"
            
            if balance_usdc == 0:
                status_msg = "⚠️ Saldo di Proxy Wallet Kosong!"
            
            self.send_telegram_report(status_msg)
            return balance_usdc
        except Exception as e:
            self.logger.error(f"❌ Balance check failed: {e}")
            return 0.0

    async def get_clob_midpoint(self, token_id: str) -> float:
        """Mengambil harga langsung dari Orderbook Polymarket."""
        try:
            req = await asyncio.to_thread(requests.get, f"{CLOB_HOST}/midpoint?token_id={token_id}", timeout=5)
            return float(req.json().get('mid', 0.0))
        except: return 0.0

    async def deploy_smart_order(self, asset: str, signal: SignalData):
        """
        Logika eksekusi yang menggunakan 'Pricing Edge'.
        Jika teknikal mengatakan harga akan naik (BUY_YES), tapi di Polymarket
        probabilitasnya sudah direpresentasikan 90% ($0.90), kita batalkan (Risk/Reward buruk).
        """
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
            
        base_bet = 1.5 # USDC
        if odds_price < 0.40 and signal.confidence >= 80:
            # Peluang emas! Diskon probabilitas.
            bet_size = base_bet * 3.0
            self.logger.info(f"💎 [VALUE BET] Menemukan dislokasi harga pada {asset}!")
        elif odds_price <= 0.65:
            bet_size = base_bet * 1.5
        else:
            bet_size = base_bet

        self.logger.info(f"📈 [EXECUTION] Mengirim order: {asset} {target_side.upper()} | Harga: ${odds_price:.3f} | Jumlah: ${bet_size} USDC | Sinyal: {signal.signal_type}")
        
        if self.clob_client:
            try:
                from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
                from py_clob_client.constants import ZERO_ADDRESS
                # Convert size to share amount (USDC / price)
                share_size = round(bet_size / odds_price, 2)
                args = OrderArgs(token_id=token_id, price=odds_price, size=share_size, side="BUY", fee_rate_bps=0, nonce=0, expiration=0, taker=ZERO_ADDRESS)
                signed_order = self.clob_client.create_order(args, PartialCreateOrderOptions(neg_risk=None))
                # Baris krusial untuk mengirim order ke server Polymarket:
                post_res = self.clob_client.post_order(signed_order)
                self.logger.info(f"✅ [POSTED] Order {asset} berhasil dikirim ke bursa! Respon: {post_res}")
                
                # Kirim report ke Telegram setelah order berhasil
                status_msg = f"🤖 Status Bot Polymarket 📍 Proxy: 0xe6A9bE17...D0b 💰 Saldo: Aktif 📊 Status: Order {asset} Berhasil!"
                self.send_telegram_report(status_msg)
            except Exception as e:
                self.logger.error(f"❌ [ORDER FAILED] API Polymarket menolak: {e}")

    async def run_monitoring(self):
        self.logger.info("🤖 Memulai Advanced Brain (Teknik Gustafsson Pro) ...")
        
        # Eksekusi pengecekan Blockchain sebelum looping berjalan
        await self.check_and_approve_usdc()
        # Cek saldo awal dan kirim report
        await self.check_balance()
        
        while True:
            try:
                # 1. Maintenance
                self.garbage_collection()
                fetch_token_ids_from_gamma()
                
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
                        self.logger.info(f"🎯 [SIGNAL DETECTED] {asset} -> {signal.signal_type} ({signal.direction}) | Conf: {signal.confidence}%")
                        if minute > 15: # Eksekusi diizinkan
                            await self.deploy_smart_order(asset, signal)
                        else:
                            self.logger.info(f"🔒 [LOCKED] Menahan eksekusi karna masih fase Observasi.")
                            
                # Scanning cepat tiap 45 detik untuk menangkap Latency Breakout!
                # Breakout terjadi sangat cepat, delay 3 menit versi lama akan kalah oleh Bot lain.
                await asyncio.sleep(45)

            except Exception as e:
                self.logger.error(f"🔥 Core Error: {e}")
                await asyncio.sleep(30)

if __name__ == "__main__":
    bot = BotPolymarket()
    asyncio.run(bot.run_monitoring())
