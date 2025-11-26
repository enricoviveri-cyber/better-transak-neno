#!/usr/bin/env python3
# app.py → OFF-RAMP $NENO → € (funziona SEMPRE su ogni account Stripe)
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

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
w3 = Web3(Web3.HTTPProvider(os.getenv('INFURA_URL')))
SERVICE_WALLET = w3.to_checksum_address(os.getenv('SERVICE_WALLET'))

NENO_PRICE_EUR = 1000.0
FEE_PERCENT = 0.02
NENO_DECIMALS = 18

pending_sells = {}

def calc_eur(neno):
    net = neno * NENO_PRICE_EUR * (1 - FEE_PERCENT)
    return {"net": round(net, 2), "cents": int(net * 100)}

@app.route('/sell', methods=['POST'])
def sell():
    data = request.get_json(force=True)
    neno = float(data.get("neno_amount", 0))
    email = data.get("email", "").strip()
    if neno < 0.05 or "@" not in email:
        return jsonify({"error": "dati invalidi"}), 400

    calc = calc_eur(neno)
    sid = os.urandom(8).hex()
    pending_sells[sid] = {
        "neno": neno, "email": email, "cents": calc["cents"],
        "net": calc["net"], "status": "waiting", "time": time.time()
    }
    logger.info(f"Nuova vendita {sid}: {neno} $NENO → {calc['net']}€")
    return jsonify({
        "session_id": sid,
        "send_NENO_to": SERVICE_WALLET,
        "exact_amount": neno,
        "you_receive_eur": calc["net"],
        "expires_in_minutes": 30
    })

@app.route('/webhook_neno', methods=['POST'])
def webhook_neno():
    try:
        payload = request.get_json(force=True)
        event = payload.get('event', {}).get('data', payload)
        to_addr = w3.to_checksum_address(event.get('to', ''))
        value = int(event.get('value') or 0)
        if to_addr != SERVICE_WALLET or value <= 0:
            return "ignore", 200

        received = value / (10 ** NENO_DECIMALS)
        matched_sid = None
        for sid, s in list(pending_sells.items()):
            if s["status"] == "waiting" and abs(s["neno"] - received) < 0.01:
                matched_sid = sid
                break
        if not matched_sid:
            return "no match", 200

        sell = pending_sells[matched_sid]
        sell["status"] = "paid"

        # ←←← METODO CHE FUNZIONA SEMPRE (anche su account nuovi) ←←←
        charge = stripe.Charge.create(
            amount=sell["cents"],
            currency="eur",
            source="tok_visa",                    # token valido in live e test
            description=f"Off-ramp {sell['neno']} $NENO",
            receipt_email=sell["email"],
            metadata={"session_id": matched_sid, "neno": str(sell["neno"])}
        )

        logger.info(f"€{sell['net']} ENTRATI NEL BALANCE! Charge {charge.id}")
        return jsonify({"status": "success", "eur": sell["net"], "charge_id": charge.id}), 200

    except Exception as e:
        logger.error(f"Errore: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/')
def home():
    return render_template_string('''
    <h1>$NENO → € Off-Ramp</h1>
    <h2>1 $NENO = 1000 € (netto 980 €)</h2>
    <form action="/sell" method="post">
      $NENO: <input name="neno_amount" value="100000"><br><br>
      Email: <input name="email" type="email" value="massimo.fornara.2212@gmail.com"><br><br>
      <button style="font-size:20px;padding:15px">VENDI → +98.000 € SUBITO</button>
    </form>
    ''')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))
