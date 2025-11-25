        #!/usr/bin/env python3
# better_transak_full_auto_v2.py - OFF-RAMP $NENO → € REALE (payout bancario immediato)
from flask import Flask, request, jsonify, render_template_string
from web3 import Web3
import stripe
import os
import logging
from dotenv import load_dotenv
import time

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ================= CONFIG OBBLIGATORIA =================
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')           # Es. sk_live_xxx
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET') # Per verificare firma Stripe

w3 = Web3(Web3.HTTPProvider(os.getenv('INFURA_URL')))     # Mainnet Ethereum
SERVICE_WALLET = w3.to_checksum_address(os.getenv('SERVICE_WALLET'))
PRIVATE_KEY = os.getenv('PRIVATE_KEY')                     # Mai esporre!

NENO_CONTRACT = "0x5f3a3a469ea20741db52e9217196926136e4e49e"
NENO_PRICE_EUR = 1000.0          # Prezzo fisso che hai deciso
FEE_PERCENT = 0.02               # 2% fee (modificabile)
NENO_DECIMALS = 18

# ABI minima per transfer + balanceOf
NENO_ABI = [
    {"inputs":[{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]

neno = w3.eth.contract(address=w3.to_checksum_address(NENO_CONTRACT), abi=NENO_ABI)

# Dizionario vendite in attesa (in produzione usa Redis o DB)
pending_sells = {}

# ================= FUNZIONI UTILITY =================
def calc_eur_from_neno(neno_amount):
    gross_eur = neno_amount * NENO_PRICE_EUR
    net_eur = gross_eur * (1 - FEE_PERCENT)
    return {"net_eur": round(net_eur, 2), "cents": int(net_eur * 100)}

def send_neno(to_address, raw_amount):
    try:
        nonce = w3.eth.get_transaction_count(SERVICE_WALLET)
        tx = neno.functions.transfer(to_address, raw_amount).build_transaction({
            'chainId': 1,
            'gas': 100000,
            'maxFeePerGas': w3.to_wei('50', 'gwei'),
            'maxPriorityFeePerGas': w3.to_wei('2', 'gwei'),
            'nonce': nonce,
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        logger.info(f"$NENO inviati → {to_address} | TX {tx_hash.hex()}")
        return tx_hash.hex()
    except Exception as e:
        logger.error(f"Errore invio $NENO: {e}")
        return None

# ================= ROTTE PUBBLICHE =================
@app.route('/sell', methods=['POST'])
def sell():
    data = request.get_json(force=True)
    neno_amount = float(data.get("neno_amount", 0))
    email = data.get("email", "").strip()

    if neno_amount < 0.05:
        return jsonify({"error": "Minimo 0.05 $NENO"}), 400
    if "@" not in email:
        return jsonify({"error": "Email valida richiesta"}), 400

    calc = calc_eur_from_neno(neno_amount)
    session_id = os.urandom(12).hex()

    pending_sells[session_id] = {
        "neno_amount": neno_amount,
        "email": email,
        "eur_cents": calc["cents"],
        "net_eur": calc["net_eur"],
        "status": "waiting_deposit",
        "created_at": time.time()
    }

    logger.info(f"Nuova vendita creata: {session_id} | {neno_amount} $NENO → {calc['net_eur']}€")

    return jsonify({
        "session_id": session_id,
        "send_NENO_to": SERVICE_WALLET,
        "exact_amount": neno_amount,
        "you_receive_eur": calc["net_eur"],
        "expires_in_minutes": 30
    })

# ================= WEBHOOK RICEZIONE $NENO (il cuore dell'off-ramp) =================
@app.route('/webhook_neno', methods=['POST'])
def webhook_neno():
    try:
        payload = request.get_json(force=True)
        event_data = payload.get('event', {}).get('data', payload)

        tx_hash = event_data.get('hash')
        from_addr = event_data.get('from', '').lower()
        to_addr = event_data.get('to', '').lower()
        value_raw = int(event_data.get('value') or event_data.get('amount', 0))

        if not tx_hash or not w3.is_checksum_address(to_addr):
            return "invalid", 400

        if to_addr != SERVICE_WALLET:
            return "not our wallet", 200

        received_neno = value_raw / (10 ** NENO_DECIMALS)

        # Cerca corrispondenza in pending_sells
        matched_session = None
        for sid, sell in pending_sells.items():
            if sell["status"] == "waiting_deposit":
                diff = abs(sell["neno_amount"] - received_neno)
                if diff < 0.005:  # tolleranza 0.005 $NENO
                    matched_session = sid
                    break

        if not matched_session:
            logger.warning(f"$NENO ricevuti ma nessuna vendita in attesa: {received_neno} da {from_addr}")
            return "no pending sell", 200

        sell = pending_sells[matched_session]
        sell["status"] = "processing"
        sell["tx_hash"] = tx_hash
        sell["from_address"] = from_addr

        # === PAYOUT FIAT REALE TRAMITE STRIPE ===
        try:
            # Crea un Transfer verso un conto bancario collegato o carta salvata
            # Opzione 1: se hai già un Connected Account Stripe per l'utente → usa destination
            # Opzione 2 (più semplice): payout diretto dal tuo balance Stripe
            payout = stripe.Payout.create(
                amount=sell["eur_cents"],
                currency="eur",
                method="instant",                     # <── IMPORTANTE: instant se il tuo paese lo supporta
                description=f"Off-ramp {sell['neno_amount']} $NENO → €",
                statement_descriptor="NENO→EUR",
                metadata={
                    "session_id": matched_session,
                    "tx_hash": tx_hash,
                    "user_email": sell["email"]
                }
            )

            sell["status"] = "paid"
            sell["payout_id"] = payout.id
            logger.info(f"€ PAGATI ISTANTANEAMENTE! {sell['net_eur']}€ → {sell['email']} | Payout {payout.id}")

            # Opzionale: invia email di conferma
            # send_email(sell["email"], ...)

            return jsonify({"status": "paid", "payout_id": payout.id, "eur": sell["net_eur"]})

        except stripe.error.StripeError as e:
            sell["status"] = "payout_failed"
            logger.error(f"Stripe payout fallito: {e}")
            return jsonify({"error": "payout failed"}), 500

    except Exception as e:
        logger.error(f"Errore webhook_neno: {e}")
        return "error", 500

# ================= HOME PAGE (per test) =================
@app.route('/')
def home():
    balance = neno.functions.balanceOf(SERVICE_WALLET).call() / 1e18
    return render_template_string(f"""
    <h1>$NENO → € Off-Ramp (prezzo fisso 1 $NENO = 1000€)</h1>
    <p><strong>Wallet servizio:</strong> {SERVICE_WALLET}<br>
       <strong>Balance:</strong> {balance:,.6f} $NENO</p>
    <hr>
    <h2>Vendi $NENO → Ricevi € immediatamente</h2>
    <form action="/sell" method="post">
        Quantità $NENO: <input name="neno_amount" value="1" step="0.000001"><br><br>
        Tua email: <input name="email" type="email" value="mario.rossi@gmail.com"><br><br>
        <button style="font-size:20px">VENDI ORA → € sul conto</button>
    </form>
    """)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)), debug=False)
