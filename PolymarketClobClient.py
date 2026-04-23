class PolymarketClobClient:
    """Client untuk Polymarket CLOB API - REVISI V2"""

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
        
        if PY_CLOB_CLIENT_AVAILABLE and self.private_key:
            if check_token_ids_valid():
                try:
                    self.client = ClobClient(
                        host=self.clob_host,
                        chain_id=137,  # Polygon Mainnet
                        key=self.private_key
                    )
                    
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
                raise Exception("Private key provided but token IDs are invalid.")
        else:
            raise Exception("py-clob-client not available or missing private key.")
        
        try:
            fetch_token_ids_from_gamma()
        except Exception as e:
            self.logger.warning(f"Failed to fetch token IDs from Gamma API: {e}")

    # ... (fungsi get_balance biarkan sama seperti sebelumnya) ...

    def place_order(self, token_id: str, side: str, price: float, size: float) -> dict:
        """
        Place a real order on Polymarket CLOB menggunakan token_id spesifik.
        """
        try:
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
            from py_clob_client.constants import ZERO_ADDRESS
            
            # 1. Validasi Input
            if price <= 0:
                raise ValueError(f"Invalid price: {price}")
            if size <= 0:
                raise ValueError(f"Invalid size: {size}")
            if side.lower() not in ["buy", "sell"]:
                raise ValueError(f"Invalid side: {side}")
            
            order_side = "BUY" if side.lower() == "buy" else "SELL"
            
            # 2. Pembulatan Aman untuk Tick Size Polymarket (Mencegah Error Invalid Tick)
            safe_price = round(float(price), 2)
            safe_size = round(float(size), 2)
            
            # 3. Parameter Order
            order_args = OrderArgs(
                token_id=str(token_id),  # Wajib string berupa angka unik dari Polymarket
                price=safe_price,
                size=safe_size,
                side=order_side,
                fee_rate_bps=0, 
                nonce=0, 
                expiration=0, 
                taker=ZERO_ADDRESS, 
            )
            
            self.logger.info(f"Preparing order: {order_args}")
            try:
                # 4. Auto-detect risk & eksekusi
                options = PartialCreateOrderOptions(neg_risk=None)
                order_result = self.client.create_order(order_args, options)
                
                # Ekstrak ID
                order_id = None
                if isinstance(order_result, dict):
                    order_id = order_result.get('order_id') or order_result.get('id')
                elif hasattr(order_result, 'order_id'):
                    order_id = order_result.order_id
                elif hasattr(order_result, 'id'):
                    order_id = order_result.id
                
                if not order_id:
                    order_id = str(order_result)
                
                self.logger.info(f"✅ Real order placed: Token {token_id} {side} ${safe_price} size {safe_size}")
                
                return {
                    "status": "ok",
                    "order_id": order_id,
                    "result": order_result,
                    "token_id": token_id,
                    "side": order_side,
                    "price": safe_price,
                    "size": safe_size,
                }
            except Exception as e:
                error_msg = str(e)
                self.logger.error(f"❌ API ERROR placing order: {e}")
                if "market not found" in error_msg.lower() or "404" in error_msg:
                    self.logger.error(f"Market tidak ditemukan untuk token_id: {token_id}.")
                return {
                    "status": "error",
                    "error": str(e),
                    "token_id": token_id,
                    "side": side,
                    "price": safe_price,
                    "size": safe_size,
                }
        except Exception as e:
            self.logger.error(f"Failed to process order parameters: {e}")
            return {
                "status": "error",
                "error": str(e)
            }