#!/usr/bin/env python3
# better_transak_full_auto.py - $NENO a 1000€ fisso - BUY & SELL 100% automatico
from flask import Flask, request, jsonify, render_template_string
from web3 import Web3
import stripe
import os
import logging
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ================= CONFIG =================
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')

w3 = Web3(Web3.HTTPProvider(os.getenv('INFURA_URL')))
SERVICE_WALLET = os.getenv('SERVICE_WALLET').lower()
PRIVATE_KEY = os.getenv('PRIVATE_KEY')

NENO_CONTRACT = "0x5f3a3a469ea20741db52e9217196926136e4e49e"
NENO_PRICE_EUR = 1000.0
FEE_PERCENT = 0.02
NENO_DECIMALS = 18

neno = w3.eth.contract(address=w3.to_checksum_address(NENO_CONTRACT), abi=[
    {"constant":False,"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"}],"name":"transfer","outputs":[],"type":"function"},
    {"constant":True,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}
])

pending_sells = {}

# ================= CALCOLI =================
def calc_neno(eur): 
    gross = eur / NENO_PRICE_EUR
    net = gross * (1 - FEE_PERCENT)
    return {"net": round(net, 6), "raw": int(net * 10**NENO_DECIMALS)}

def calc_eur(neno_amount):
    gross = neno_amount * NENO_PRICE_EUR
    net = gross * (1 - FEE_PERCENT)
    return {"net": round

(net, 2), "cents": int(net * 100)}

def send_neno(to, raw_amount):
    nonce = w3.eth.get_transaction_count(SERVICE_WALLET)
    tx = neno.functions.transfer(to, raw_amount).build_transaction({
        'chainId': 1, 'gas': 80000, 'gasPrice': w3.to_wei('20', 'gwei'), 'nonce': nonce
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()
    return tx_hash

# ================= ROTTE =================
@app.route('/buy', methods=['POST'])
def buy():
    data = request.get_json()
    eur = float(data.get("eur_amount", 0))
    wallet = data.get("wallet", "").strip()
    if eur < 50 or not w3.is_address(wallet):
        return jsonify({"error": "Min 50€ o wallet invalido"}), 400
    calc = calc_neno(eur)
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price_data': {
            'currency': 'eur',
            'product_data': {'name': f'{calc["net"]} $NENO'},
            'unit_amount': int(eur * 100),
        }, 'quantity': 1}],
        mode='payment',
        success_url='https://google.com',
        cancel_url='https://google.com',
        metadata={'type': 'buy', 'wallet': wallet, 'raw_neno': str(calc["raw"])}
    )
    return jsonify({"payment_url": session.url, "neno": calc["net"]})

@app.route('/sell', methods=['POST'])
def sell():
    data = request.get_json()
    neno_amount = float(data.get("neno_amount", 0))
    email = data.get("email")
    if neno_amount < 0.05: return jsonify({"error": "Min 0.05 $NENO"}), 400
    calc = calc_eur(neno_amount)
    session_id = os.urandom(8).hex()
    pending_sells[session_id] = {
        "email": email, "neno_amount": neno_amount, "eur_cents": calc["cents"], "status": "waiting"
    }
    return jsonify({
        "send_to": SERVICE_WALLET,
        "amount": neno_amount,
        "you_receive_eur": calc["net"],
        "session_id": session_id
    })

@app.route('/webhook_neno', methods=['POST'])
def webhook_neno():
    payload = request.json
    try:
        tx = payload.get('event', {}).get('data', payload)
        to_addr = tx['to'].lower()
        value = int(tx.get('value') or tx.get('amount', 0))
        tx_hash = tx['hash']
    except: return "invalid", 400
    if to_addr != SERVICE_WALLET or value <= 0: return "ignored", 200
    received = value / 10**NENO_DECIMALS
    match = None
    for sid, data in pending_sells.items():
        if data["status"] == "waiting" and abs(data["neno_amount"] - received) < 0.001:
            match = sid; break
    if not match: return "no match", 200
    sell = pending_sells[match]; sell["status"] = "paid"
    stripe.Payout.create(
        amount=sell["eur_cents"],
        currency="eur",
        description=f"Sell {sell['neno_amount']} $NENO",
        metadata={"tx": tx_hash}
    )
    logger.info(f"PAGATO {sell['eur_cents']/100}€ a {sell['email']} | TX {tx_hash}")
    return jsonify({"status": "paid"})

@app.route('/webhook_stripe', methods=['POST'])
def webhook_stripe():
    payload = request.data
    sig = request.headers.get('Stripe-Signature')
    event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    if event['type'] == 'checkout.session.completed':
        s = event['data']['object']
        if s.metadata.get('type') == 'buy':
            tx = send_neno(s.metadata['wallet'], int(s.metadata['raw_neno']))
            logger.info(f"BUY: {s.metadata['raw_neno']} raw a {s.metadata['wallet']} | TX {tx}")
    return jsonify(success=True)

@app.route('/')
def home():
    bal = neno.functions.balanceOf(SERVICE_WALLET).call() / 1e18
    return render_template_string(f"""
    <h1>$NENO Ramp – 1 $NENO = 1000€ (fisso)</h1>
    <p>Balance servizio: {bal:,.4f} $NENO</p>
    <h2>Compra</h2>
    <form action="/buy" method="post">
        EUR: <input name="eur_amount" value="1000"><br>
        Wallet: <input name="wallet" size=50><br>
        <button>Paga → Ricevi $NENO</button>
    </form>
    <h2>Vendi</h2>
    <form action="/sell" method="post">
        $NENO: <input name="neno_amount" value="1"><br>
        Email: <input name="email" type="email"><br>
        <button>Invia $NENO → Ricevi €</button>
    </form>
    """)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))
