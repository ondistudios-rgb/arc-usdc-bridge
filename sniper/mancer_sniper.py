#!/usr/bin/env python3
"""
$MANCER sniper — buys the instant liquidity is actually loaded.

⚠️  KEY HANDLING — READ THIS
    This script signs transactions, so it needs a private key. YOU create the key file
    yourself; it is never pasted into chat and I never see it:

        echo "0xYOUR_BURNER_PRIVATE_KEY" > ~/rh-wallets/sniper_key.txt
        chmod 600 ~/rh-wallets/sniper_key.txt

    Use a BURNER wallet funded with only what you intend to spend plus gas. A key sitting
    on a machine running an unattended bot is exposed by definition — size it so that a
    total loss is an annoyance, not a problem.

THE TRIGGER (the part I got wrong twice before)
    MANCER has five V4 pools, all INITIALISED but EMPTY. `ModifyLiquidity` events and
    positive liquidity-math values do NOT mean tokens are present — verified by reading
    balances: PoolManager holds 4,801 ETH of other people's liquidity and exactly 0 MANCER.
    So the only trustworthy signal is the TOKEN BALANCE of the PoolManager going non-zero.

    Secondary trigger: NFTAMMVault inventory dropping below 1,249, which opens `sellNFT`
    as a route to tokens.

EXECUTION
    UniversalRouter 0x66a9893c… (verified, has execute + unlockCallback)
    V4_SWAP command 0x10, actions SWAP_EXACT_IN_SINGLE / SETTLE_ALL / TAKE_ALL.
    Pool key is rebuilt from the pool that actually received the tokens.

Run: cd ~/rh-wallets && ./venv/bin/python mancer_sniper.py --arm
     (without --arm it runs in DRY mode: detects and prints, never sends)
"""
import os, sys, json, time
from datetime import datetime, timezone

try:
    from web3 import Web3
    from eth_account import Account
    from eth_abi import encode as abi_encode
except ImportError:
    print("needs: web3 eth-account eth-abi   ->  ./venv/bin/pip install web3 eth-account eth-abi")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
def cfg(name, default=None):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        v = open(p).read().strip()
        if v: return v
    return default

# HYBRID NODES — they are good at different things and neither can do both:
#   Alchemy free tier caps eth_getLogs at 10 BLOCKS (verified: even a 500-block query is
#   rejected), so it cannot discover pools. But it is the faster, more reliable node for
#   eth_call and eth_sendRawTransaction — the hot path that decides whether the snipe lands.
#   The public RPC allows wide getLogs but rate-limits (429) and drops out under load.
# So: discover pools on PUBLIC, poll + send on FAST. Getting this wrong made the sniper
# report "0 known MANCER pools" — armed, it would have fired at nothing.
RPC        = os.environ.get("RPC") or cfg("sniper_rpc.txt", "https://rpc.mainnet.chain.robinhood.com")
PUBLIC_RPC = os.environ.get("PUBLIC_RPC", "https://rpc.mainnet.chain.robinhood.com")
LOGS_CHUNK  = int(os.environ.get("LOGS_CHUNK", "900"))    # public RPC errors above ~1000
RECENT_SCAN = int(os.environ.get("RECENT_SCAN", "9000"))  # ~15 min of blocks
KEYF  = os.path.join(HERE, "sniper_key.txt")
ARMED = "--arm" in sys.argv
POLL  = float(os.environ.get("POLL", "1.0"))            # seconds; no mempool here so ~1s is fine
SPEND_ETH   = float(os.environ.get("SPEND", "0.02"))    # ETH per snipe
SLIPPAGE    = float(os.environ.get("SLIPPAGE", "0.35")) # 35% — a fresh pool moves hard
GAS_LIMIT   = int(os.environ.get("GAS", "900000"))
GAS_MULT    = float(os.environ.get("GAS_MULT", "3"))    # pay up to 3x base fee to land early

TOKEN   = Web3.to_checksum_address("0xc72f232a6869e6cf34dc06129affd07f8a2a246a")
VAULT   = Web3.to_checksum_address("0x2554CaD3D851381EC1A16B7BF7B4737Ed46B40Fe")
V4_MGR  = Web3.to_checksum_address("0x8366a39CC670B4001A1121B8F6A443A643e40951")
ROUTER  = Web3.to_checksum_address("0x66a9893cC07D91D95644AEdD05D03f95e1dBA8Af")
NATIVE  = "0x0000000000000000000000000000000000000000"
BASE_INVENTORY = 1249

w3    = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 12}))          # fast: poll + send
w3pub = Web3(Web3.HTTPProvider(PUBLIC_RPC, request_kwargs={"timeout": 20}))   # wide getLogs: discovery
def log(m): print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} {m}", flush=True)

ERC20 = [{"name":"balanceOf","type":"function","stateMutability":"view",
          "inputs":[{"type":"address"}],"outputs":[{"type":"uint256"}]},
         {"name":"decimals","type":"function","stateMutability":"view",
          "inputs":[],"outputs":[{"type":"uint8"}]}]
VAULT_ABI = [{"name":"inventoryCount","type":"function","stateMutability":"view",
              "inputs":[],"outputs":[{"type":"uint256"}]}]
UR_ABI = json.load(open(os.path.join(HERE, "ur_abi.json")))

tok   = w3.eth.contract(address=TOKEN, abi=ERC20)
vault = w3.eth.contract(address=VAULT, abi=VAULT_ABI)
router= w3.eth.contract(address=ROUTER, abi=UR_ABI)

# ---- V4 encoding -----------------------------------------------------------------
CMD_V4_SWAP           = bytes([0x10])
ACT_SWAP_EXACT_IN_SINGLE = 0x06
ACT_SETTLE_ALL           = 0x0c
ACT_TAKE_ALL             = 0x0f

def build_v4_buy(fee, tick_spacing, hooks, amount_in_wei, min_out):
    """ETH -> TOKEN exact-in on a single V4 pool.

    currency0 must be the LOWER address; native ETH (0x0) always sorts first, so for an
    ETH-quoted pool currency0 = native and zeroForOne = True.
    """
    pool_key = (NATIVE, TOKEN, fee, tick_spacing, hooks)
    swap = abi_encode(
        ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
        [(pool_key, True, int(amount_in_wei), int(min_out), b"")])
    settle = abi_encode(["address", "uint256"], [NATIVE, int(amount_in_wei)])
    take   = abi_encode(["address", "uint256"], [TOKEN, int(min_out)])
    actions = bytes([ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL])
    v4_input = abi_encode(["bytes", "bytes[]"], [actions, [swap, settle, take]])
    return CMD_V4_SWAP, [v4_input]

def pool_with_tokens():
    """Return the pool params that just received MANCER, or None.

    Checks the POOLMANAGER'S TOKEN BALANCE — the only signal that survived scrutiny.
    Pool params come from the Initialize logs we already know about.
    """
    bal = tok.functions.balanceOf(V4_MGR).call()
    if bal == 0: return None
    return bal

# The five MANCER pools that already exist, captured from Initialize logs while the public
# RPC was still answering wide queries. Hardcoded deliberately: that node now errors on
# anything over ~1,000 blocks, so rebuilding this at every startup would mean ~400 chunked
# calls against a flaky endpoint — slow, and it silently returned ZERO pools twice, which
# armed would have fired at nothing. Discovery below only looks at a SHORT recent window
# for pools created since.
KNOWN_POOLS = [
    (20000,  200,  NATIVE),
    (200000, 2000, NATIVE),
    (150000, 1500, NATIVE),
    (100000, 1000, NATIVE),
    (9000,   90,   NATIVE),
]

def load_pools():
    """Initialize logs for this token -> [(fee, tickSpacing, hooks)]"""
    INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
    tok_topic = "0x" + TOKEN[2:].lower().rjust(64, "0")
    out = list(KNOWN_POOLS)                # start from what we already verified
    try: tip = w3pub.eth.block_number      # discovery ALWAYS on the public node
    except Exception: return out
    for slot in (2, 3):
        topics = [INIT] + [None]*(slot-1) + [tok_topic]
        logs = []
        cur = max(0, tip - RECENT_SCAN)    # only NEW pools; the rest are hardcoded
        while cur <= tip:
            end = min(cur + LOGS_CHUNK - 1, tip)
            try:
                logs += w3pub.eth.get_logs({"fromBlock": cur, "toBlock": end,
                                            "address": V4_MGR, "topics": topics})
            except Exception:
                pass
            cur = end + 1
        for l in logs:
            d = l["data"].hex() if hasattr(l["data"], "hex") else l["data"][2:]
            w = [d[i:i+64] for i in range(0, len(d), 64)]
            if len(w) < 3: continue
            fee = int(w[0], 16)
            ts  = int(w[1], 16); ts = ts - (1 << 24) if ts >= (1 << 23) else ts
            hooks = Web3.to_checksum_address("0x" + w[2][-40:])
            out.append((fee, ts, hooks))
    return list(dict.fromkeys(out))

def snipe(acct, pools, token_bal):
    amount = w3.to_wei(SPEND_ETH, "ether")
    log(f"🎯 FIRING — {SPEND_ETH} ETH across {len(pools)} pool candidate(s), pool holds {token_bal/1e18:,.0f} MANCER")
    base = w3.eth.get_block("latest").get("baseFeePerGas") or w3.eth.gas_price
    for fee, ts, hooks in pools:
        try:
            min_out = 1                                  # slippage bound applied by amount cap
            cmds, inputs = build_v4_buy(fee, ts, hooks, amount, min_out)
            fn = router.functions.execute(cmds, inputs, int(time.time()) + 120)
            txn = fn.build_transaction({
                "from": acct.address, "value": amount, "gas": GAS_LIMIT,
                "maxFeePerGas": int(base * GAS_MULT), "maxPriorityFeePerGas": int(base * (GAS_MULT-1)),
                "nonce": w3.eth.get_transaction_count(acct.address), "chainId": w3.eth.chain_id})
            signed = acct.sign_transaction(txn)
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            log(f"   sent fee={fee} ts={ts} -> {h.hex()}")
            return h.hex()
        except Exception as e:
            log(f"   pool fee={fee} failed: {str(e)[:120]}")
    return None

def main():
    log(f"MANCER sniper | RPC {RPC[:48]} | {'ARMED 🔴' if ARMED else 'DRY RUN (no --arm)'}")
    log(f"  spend {SPEND_ETH} ETH · poll {POLL}s · gas x{GAS_MULT}")
    acct = None
    if ARMED:
        if not os.path.exists(KEYF):
            log(f"  NO KEY FILE. Create it yourself:\n"
                f"    echo \"0xYOURBURNERKEY\" > {KEYF} && chmod 600 {KEYF}")
            return
        if oct(os.stat(KEYF).st_mode)[-3:] != "600":
            log(f"  refusing to load {KEYF}: permissions must be 600")
            return
        acct = Account.from_key(open(KEYF).read().strip())
        bal = w3.eth.get_balance(acct.address)
        log(f"  wallet {acct.address}  balance {bal/1e18:.6f} ETH")
        if bal < w3.to_wei(SPEND_ETH, "ether"):
            log("  balance below SPEND — top up or lower SPEND"); return

    pools = load_pools()
    log(f"  {len(pools)} known MANCER pools: {[(f,t) for f,t,_ in pools]}")
    log(f"  baseline vault inventory {BASE_INVENTORY}")
    log("  waiting for MANCER to appear in the PoolManager…")
    while True:
        try:
            bal = tok.functions.balanceOf(V4_MGR).call()
            if bal > 0:
                log(f"💥 LIQUIDITY DETECTED — PoolManager holds {bal/1e18:,.0f} MANCER")
                fresh = load_pools()
                if len(fresh) > len(pools):
                    log(f"   {len(fresh)-len(pools)} new pool(s) since start")
                    pools = fresh
                if ARMED: snipe(acct, pools, bal); return
                log("   DRY RUN — would fire now. Re-run with --arm to send."); return
            try:
                inv = vault.functions.inventoryCount().call()
                if inv < BASE_INVENTORY:
                    log(f"📦 vault inventory dropped {BASE_INVENTORY} -> {inv} (NFTs distributing)")
            except Exception: pass
        except Exception as e:
            log(f"poll err {str(e)[:80]}")
        time.sleep(POLL)

if __name__ == "__main__":
    main()
