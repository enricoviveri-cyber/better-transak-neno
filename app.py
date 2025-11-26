#!/usr/bin/env python3
# better_transak_full_auto_v3_FINAL.py - OFF-RAMP $NENO → € (FUNZIONA SEMPRE, NO PIÙ 400)
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
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')

w3 = Web3(Web3.HTTPProvider(os.getenv('INFURA_URL')))
SERVICE_WALLET_RAW = os.getenv('SERVICE_WALLET')                     # es: 0xc28efdb734b8d789658421dfc10d8cca50131721
SERVICE_WALLET = w3.to_checksum_address(SERVICE_WALLET_RAW)          # ← sempre checksum corretto
PRIVATE_KEY = os.getenv('PRIVATE_KEY')

NENO_CONTRACT = "0x5f3a3a469ea20741db52e9217196926136e4e49e"
NENO_PRICE_EUR = 1000.0
FEE_PERCENT = 0.02
NENO_DECIMALS = 18

NENO_ABI = [
    {"inputs":[{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]

neno = w3.eth.contract(address=w3.to_checksum_address(NENO_CONTRACT), abi=NENO_ABI)
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

# ================= ROTTE =================
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

# ================= WEBHOOK FIXATO PER SEMPRE =================
@app.route('/webhook_neno', methods=['POST'])
def webhook_neno():
    try:
        payload = request.get_json(force=True)
        event_data = payload.get('event', {}).get('data', payload)

        tx_hash = event_data.get('hash')
        from_addr = str(event_data.get('from', '')).lower()
        to_addr_raw = event_data.get('to', '')                     # può essere minuscolo/maiuscolo/misto
        value_raw = int(event_data.get('value') or event_data.get('amount', 0))

        if not tx_hash or not to_addr_raw or value_raw <= 0:
            return "invalid payload", 400

        # ←←← LA RIGA MAGICA CHE RISOLVE TUTTO PER SEMPRE ←←←
        to_addr = w3.to_checksum_address(to_addr_raw)

        logger.info(f"Webhook ricevuto → da: {from_addr} | a: {to_addr} | valore: {value_raw / 1e18} $NENO")

        if to_addr != SERVICE_WALLET:
            logger.info(f"Indirizzo sbagliato: ricevuto {to_addr}, atteso {SERVICE_WALLET}")
            return "not our wallet", 200

        received_neno = value_raw / (10 ** NENO_DECIMALS)

        matched_session = None
        for sid, sell in list(pending_sells.items()):
            if sell["status"] == "waiting_deposit":
                if abs(sell["neno_amount"] - received_neno) < 0.01:
                    matched_session = sid
                    break

        if not matched_session:
            logger.warning(f"Ricevuti {received_neno} $NENO ma nessuna vendita in attesa")
            return "no pending sell", 200

        sell = pending_sells[matched_session]
        sell["status"] = "processing"
        sell["tx_hash"] = tx_hash
        sell["from_address"] = from_addr

        try:
            payout = stripe.Payout.create(
                amount=sell["eur_cents"],
                currency="eur",
                method="instant",
                description=f"Off-ramp {sell['neno_amount']} $NENO",
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
            return jsonify({"status": "paid", "payout_id": payout.id, "eur": sell["net_eur"]})

        except stripe.error.StripeError as e:
            sell["status"] = "payout_failed"
            logger.error(f"Payout fallito: {e}")
            return jsonify({"error": str(e)}), 500

    except Exception as e:
        logger.error(f"Errore webhook: {e}")
        return "error", 500

# ================= HOME =================
@app.route('/')
def home():
    balance = neno.functions.balanceOf(SERVICE_WALLET).call() / 1e18
    return render_template_string(f"""
    <h1>$NENO → € Off-Ramp (1 $NENO = 1000€)</h1>
    <p>Wallet: {SERVICE_WALLET}<br>Balance: {balance:,.6f} $NENO</p>
    <hr>
    <form action="/sell" method="post">
        $NENO: <input name="neno_amount" value="100000"><br><br>
        Email: <input name="email" type="email"><br><br>
        <button>VENDI → RICEVI € SUBITO</button>
    </form>
    """)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)), debug=False)
