"""Synthetic stream generator for tests and dry runs (NOT market data).

Produces events in the Advanced Data Stream format with a mix of rugs, duds,
mid pumps and 5x+ runners.  Early behaviour is only loosely correlated with the
outcome, so a strategy has something to find but nothing is trivially separable.
"""

from __future__ import annotations

import heapq
import random
import string

from . import SOL_MINT

PROTOCOLS = [("PUMPFUN", 0.6), ("BONK", 0.2), ("METEORA_DBC", 0.1), ("STONKFUN", 0.1)]
MIGRATE_TO = {"PUMPFUN": "PUMPSWAP", "BONK": "RAYDIUM", "STONKFUN": "RAYDIUM", "METEORA_DBC": "METEORA"}
KINDS = [("rug", 0.35), ("dud", 0.45), ("mid", 0.13), ("runner", 0.07)]


def _pick(rng, table):
    x, acc = rng.random(), 0.0
    for v, w in table:
        acc += w
        if x <= acc:
            return v
    return table[-1][0]


def _addr(rng, n=44):
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(n))


def _token_events(rng, t0, block0, creators):
    kind = _pick(rng, KINDS)
    proto = _pick(rng, PROTOCOLS)
    mint, pool = _addr(rng), _addr(rng)
    serial = rng.random() < 0.3
    creator = rng.choice(creators) if serial else _addr(rng)
    supply = 1_000_000_000
    Q, T = 30.0, 1_073_000_000.0  # virtual reserves
    k = Q * T
    ev = []
    sig_n = [0]

    def sig():
        sig_n[0] += 1
        return f"{mint[:8]}-{sig_n[0]}"

    def trade(ts, block, trader, side, quote):
        nonlocal Q, T
        if side == "buy":
            Q2 = Q + quote
            T2 = k / Q2
            tok = T - T2
        else:
            tok = quote  # here `quote` carries the token amount to sell
            T2 = T + tok
            Q2 = k / T2
            quote = Q - Q2
        Q, T = Q2, T2
        price = Q / T
        return {"signature": sig(), "block": block, "timestamp": ts, "action": side, "protocol": cur_proto[0],
                "txSigner": trader, "pool": cur_pool[0], "mint": mint, "quoteMint": SOL_MINT,
                "tokenAmount": round(tok, 2), "quoteAmount": round(quote, 9), "price": price,
                "marketCapQuote": price * supply, "tokensInPool": T, "quoteInPool": Q,
                "tradersInvolved": [trader],
                "breakdown": [{"trader": trader, "action": side, "tokenAmount": round(tok, 2),
                               "quoteAmount": round(quote, 9), "price": price}]}

    cur_proto, cur_pool = [proto], [pool]
    dev_buy = rng.choice([0.0, 0.2, 0.5, 1.0, 2.0, 3.0]) if kind != "rug" else rng.choice([1.0, 2.0, 4.0, 6.0])
    holdings: dict = {}
    if dev_buy:
        e = trade(t0, block0, creator, "buy", dev_buy)
        e["signature"] = f"{mint[:8]}-create"
        holdings[creator] = e["tokenAmount"]
        ev.append(e)
    ev.append({"signature": f"{mint[:8]}-create", "block": block0, "timestamp": t0, "action": "create",
               "protocol": proto, "txSigner": creator, "mint": mint, "pool": pool, "quoteMint": SOL_MINT,
               "creator": creator, "name": f"Token {mint[:4]}", "symbol": mint[:4].upper(), "uri": "",
               "decimals": 6, "supply": supply, "tokensInPool": T, "quoteInPool": Q,
               "initialBuy": {"quoteAmount": dev_buy, "tokenAmount": holdings.get(creator, 0)} if dev_buy else None,
               "mintAuthority": _addr(rng) if rng.random() < 0.02 else None, "freezeAuthority": None,
               "tokenExtensions": []})
    # bundled snipers in the launch block (more common on rugs)
    n_bundle = rng.choice([0, 0, 1, 2, 4, 8]) if kind == "rug" else rng.choice([0, 0, 0, 1, 2])
    for _ in range(n_bundle):
        w = _addr(rng)
        e = trade(t0, block0, w, "buy", rng.uniform(0.3, 2.0))
        holdings[w] = holdings.get(w, 0) + e["tokenAmount"]
        ev.append(e)

    # behaviour by kind: (p_buy early, p_buy late, rate early, decay, lifetime_s)
    params = {
        "rug": (0.62, 0.45, 0.6, 0.004, rng.uniform(60, 900)),
        "dud": (0.55, 0.40, 0.35, 0.01, rng.uniform(60, 1800)),
        "mid": (0.66, 0.50, 0.9, 0.002, rng.uniform(900, 5400)),
        "runner": (0.66, 0.565, 1.1, 0.0004, rng.uniform(3600, 5 * 3600)),
    }[kind]
    p_early, p_late, rate0, decay, life = params
    rug_at = t0 + rng.uniform(20, 600) if kind == "rug" else None
    rugged = False
    migrated = False
    t, block = float(t0), block0
    traders = [_addr(rng) for _ in range(rng.randint(30, 400))]
    while t - t0 < life:
        age = t - t0
        rate = max(0.02, rate0 * (1.0 / (1.0 + decay * age)))
        t += rng.expovariate(rate)
        block = block0 + int((t - t0) / 0.4)
        ts = int(t)
        if rug_at and not rugged and t >= rug_at:
            rugged = True
            tok = holdings.get(creator, 0) + sum(v for a, v in holdings.items() if a != creator) * 0.5
            if tok > 0:
                ev.append(trade(ts, block, creator, "sell", tok))
                holdings = {}
            p_early = p_late = 0.25
            continue
        p_buy = p_early if age < 300 else p_late
        if rng.random() < p_buy:
            w = rng.choice(traders)
            size = min(rng.lognormvariate(-1.2, 1.0), 20.0)
            e = trade(ts, block, w, "buy", size)
            holdings[w] = holdings.get(w, 0) + e["tokenAmount"]
            ev.append(e)
        else:
            sellers = [a for a, v in holdings.items() if v > 1 and (a != creator or kind in ("dud", "mid"))]
            if not sellers:
                continue
            w = rng.choice(sellers)
            amt = holdings[w] * rng.choice([0.25, 0.5, 1.0])
            holdings[w] -= amt
            ev.append(trade(ts, block, w, "sell", amt))
        if not migrated and Q >= 115:
            migrated = True
            new_pool = _addr(rng)
            ev.append({"signature": sig(), "block": block, "timestamp": ts, "action": "migrate",
                       "protocol": proto, "txSigner": _addr(rng), "mint": mint, "fromPool": pool,
                       "toPool": new_pool, "toProtocol": MIGRATE_TO[proto]})
            cur_proto[0], cur_pool[0] = MIGRATE_TO[proto], new_pool
    return ev


def generate(n_tokens: int = 1000, hours: float = 24.0, seed: int = 1, start_ts: int = 1_790_000_000):
    """Return a time-ordered list of synthetic stream events."""
    rng = random.Random(seed)
    creators = [_addr(rng) for _ in range(max(5, n_tokens // 40))]
    streams = []
    for i in range(n_tokens):
        t0 = start_ts + int(rng.uniform(0, hours * 3600))
        streams.append(_token_events(rng, t0, 400_000_000 + (t0 - start_ts) * 2, creators))
    # merge per-token streams by time; keeps per-transaction order stable
    return list(heapq.merge(*streams, key=lambda e: e["timestamp"]))
