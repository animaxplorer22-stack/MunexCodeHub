#!/usr/bin/env python3

import sys
import os
import time
import json
import base64
import hashlib
import secrets
import random
import asyncio
import argparse
import logging
import socket
import heapq
import math
from typing import Optional, Tuple, List, Set
from datetime import datetime

import websockets
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

# ─── Colours & styling ────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = \
    "\033[30m", "\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[35m", "\033[36m", "\033[37m"
BG_BLACK, BG_RED, BG_GREEN, BG_YELLOW, BG_BLUE, BG_MAGENTA, BG_CYAN, BG_WHITE = \
    "\033[40m", "\033[41m", "\033[42m", "\033[43m", "\033[44m", "\033[45m", "\033[46m", "\033[47m"

def c(colour, text):
    return f"{colour}{text}{RESET}"

# ─── ASCII Banner ─────────────────────────────────────────────
BANNER = f"""
{c(MAGENTA,'  ███╗   ███╗██╗   ██╗███╗   ██╗███████╗██╗  ██╗')}{c(CYAN,'     ███╗   ███╗██╗███╗   ██╗███████╗██████╗ ')}
{c(MAGENTA,'  ████╗ ████║██║   ██║████╗  ██║██╔════╝╚██╗██╔╝')}{c(CYAN,'     ████╗ ████║██║████╗  ██║██╔════╝██╔══██╗')}
{c(MAGENTA,'  ██╔████╔██║██║   ██║██╔██╗ ██║█████╗   ╚███╔╝ ')}{c(CYAN,'     ██╔████╔██║██║██╔██╗ ██║█████╗  ██████╔╝')}
{c(MAGENTA,'  ██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝   ██╔██╗ ')}{c(CYAN,'     ██║╚██╔╝██║██║██║╚██╗██║██╔══╝  ██╔══██╗')}
{c(MAGENTA,'  ██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████╗██╔╝ ██╗')}{c(CYAN,'     ██║ ╚═╝ ██║██║██║ ╚████║███████╗██║  ██║')}
{c(MAGENTA,'  ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝')}{c(CYAN,'     ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝')}
{c(GREEN,'  ───  PROOF OF MAZE RACE MINER  ───')}
{c(DIM,'  v63.0 – Mainnet')}
"""

# ─── Logging setup ────────────────────────────────────────────
class ColouredFormatter(logging.Formatter):
    LEVEL_COLOURS = {
        logging.DEBUG: DIM,
        logging.INFO: CYAN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: RED+BOLD,
    }
    def format(self, record):
        levelname = record.levelname
        if levelname in self.LEVEL_COLOURS:
            record.levelname = f"{self.LEVEL_COLOURS[levelname]}{levelname}{RESET}"
        if record.name == "Miner":
            record.name = f"{c(MAGENTA, 'Miner')}"
        return super().format(record)

logger = logging.getLogger("Miner")
logger.setLevel(logging.INFO)
console = logging.StreamHandler()
console.setFormatter(ColouredFormatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(console)

# ─── Constants ──────────────────────────────────────────────
WALLET_FILE = "miner_wallet.json"
DNS_SEEDS = ["munexseed.duckdns.org"]
FALLBACK_NODE = "ws://munexseed.duckdns.org:8080/ws"
WS_PATH = "/ws"
WS_DEFAULT_PORT = 8080

DIR_NAMES = ["east", "west", "north", "south", "up", "down"]
DIR_OFFSETS = [(1,0,0), (-1,0,0), (0,1,0), (0,-1,0), (0,0,1), (0,0,-1)]

MOVE_TIMEOUT = 10.0
HEARTBEAT_INTERVAL = 10
MAX_RECONNECT = 5
KNOWLEDGE_INTERVAL = 3.0
POW_DIFFICULTY = 16

# ─── Crypto helpers ──────────────────────────────────────────
def generate_keypair():
    priv = ec.generate_private_key(ec.SECP256K1())
    priv_int = priv.private_numbers().private_value
    priv_hex = priv_int.to_bytes(32, "big").hex()
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(serialization.Encoding.X962,
                                 serialization.PublicFormat.UncompressedPoint)
    keccak = hashlib.sha3_256(pub_bytes[1:]).digest()
    addr = "0x" + keccak[-20:].hex()
    return addr, priv_hex, pub_bytes.hex()

def sign_message(priv_hex, msg):
    priv_key = ec.derive_private_key(int(priv_hex, 16), ec.SECP256K1())
    sig = priv_key.sign(msg.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(sig)
    return r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()

def pubkey_from_priv(priv_hex):
    priv_key = ec.derive_private_key(int(priv_hex, 16), ec.SECP256K1())
    pub = priv_key.public_key()
    x = pub.public_numbers().x.to_bytes(32, "big")
    y = pub.public_numbers().y.to_bytes(32, "big")
    return (b"\x04" + x + y).hex()

def load_wallet():
    if os.path.exists(WALLET_FILE):
        with open(WALLET_FILE, "r") as f:
            data = json.load(f)
            return data.get("wallet"), data.get("privkey")
    return None, None

def save_wallet(addr, priv):
    with open(WALLET_FILE, "w") as f:
        json.dump({"wallet": addr, "privkey": priv}, f, indent=2)
    try:
        os.chmod(WALLET_FILE, 0o600)
    except:
        pass

def resolve_dns(domain, port=WS_DEFAULT_PORT):
    urls = []
    try:
        ips = socket.gethostbyname_ex(domain)[2]
        for ip in ips:
            urls.append(f"ws://{ip}:{port}{WS_PATH}")
    except:
        pass
    return urls

async def discover_nodes():
    discovered = []
    for seed in DNS_SEEDS:
        urls = resolve_dns(seed)
        if urls:
            discovered.extend(urls)
    if not discovered:
        discovered = [FALLBACK_NODE]
    random.shuffle(discovered)
    return discovered

def a_star(maze, start, goal, known_walls):
    size = len(maze)
    sx, sy, sz = start
    gx, gy, gz = goal
    if not (0 <= sx < size and 0 <= sy < size and 0 <= sz < size) or \
       not (0 <= gx < size and 0 <= gy < size and 0 <= gz < size):
        return None
    if maze[sz][sy][sx] == 1 or maze[gz][gy][gx] == 1:
        return None

    open_set = [(0, start)]
    came_from = {}
    g_score = {start: 0}
    f_score = {start: abs(sx-gx)+abs(sy-gy)+abs(sz-gz)}

    while open_set:
        _, cur = heapq.heappop(open_set)
        if cur == goal:
            path = []
            while cur in came_from:
                path.append(cur)
                cur = came_from[cur]
            path.reverse()
            return path
        cx, cy, cz = cur
        for dx, dy, dz in DIR_OFFSETS:
            nx, ny, nz = cx+dx, cy+dy, cz+dz
            if not (0 <= nx < size and 0 <= ny < size and 0 <= nz < size):
                continue
            if maze[nz][ny][nx] == 1:
                continue
            if (nx, ny, nz) in known_walls:
                continue
            nxt = (nx, ny, nz)
            tentative = g_score[cur] + 1
            if tentative < g_score.get(nxt, float('inf')):
                came_from[nxt] = cur
                g_score[nxt] = tentative
                f_score[nxt] = tentative + abs(nx-gx)+abs(ny-gy)+abs(nz-gz)
                heapq.heappush(open_set, (f_score[nxt], nxt))
    return None

def solve_pow(seed: str, difficulty: int) -> int:
    nonce = 0
    start = time.time()
    while True:
        data = f"{seed}{nonce}".encode()
        digest = hashlib.sha256(data).digest()
        if int.from_bytes(digest, 'big') >> (256 - difficulty) == 0:
            elapsed = (time.time() - start) * 1000
            logger.debug(f"PoW solved in {elapsed:.0f}ms, nonce={nonce}")
            return nonce
        nonce += 1

class PCMiner:
    def __init__(self, wallet, privkey, miner_id=None, node_url=None, verbose=False):
        self.wallet = wallet
        self.privkey = privkey
        self.pubkey = pubkey_from_priv(privkey)
        if miner_id is None:
            rand = secrets.token_hex(8)
            seed = f"{self.wallet}_{time.time()}_{rand}"
            digest = hashlib.sha256(seed.encode()).hexdigest()[:24].upper()
            self.miner_id = f"PC_{digest}"
        else:
            self.miner_id = miner_id
        self.node_url = node_url
        self.ws = None
        self.registered = False
        self.balance = 0
        self.running = True
        self.verbose = verbose

        self.maze = None
        self.size = 0
        self.start = None
        self.current = None
        self.goal = None
        self.deadline = 0.0
        self.max_steps = 10**9
        self.block_id = -1
        self.steps = 0
        self.accepted = 0
        self.attempts = 0
        self.round_active = False
        self.difficulty = 1.0

        self.known_walls = set()
        self.reported_walls = set()
        self.last_report = 0.0

        self.nav_task = None
        self.pending_ack = None
        self.heartbeat_task = None
        self.reconn_attempts = 0
        self.start_time = 0.0
        self.last_move_duration = 0.0
        self.pow_pending = False
        self.ping_start = 0.0
        self.last_ping = 0.0
        self._ping_future = None

    async def connect(self, url):
        try:
            start = time.time()
            self.ws = await websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=60,
                open_timeout=30,
                close_timeout=20,
                max_size=2**24
            )
            self.node_url = url
            self.reconn_attempts = 0
            logger.info(f"{c(GREEN,'Connected')} to {c(CYAN,url)}")
            self.last_ping = (time.time() - start) * 1000
            return True
        except Exception as e:
            logger.error(f"{c(RED,'Connect failed')}: {e}")
            return False

    async def disconnect(self):
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None

    async def register(self):
        ts = int(time.time())
        sig = sign_message(self.privkey, f"{self.miner_id}{self.wallet}{ts}")
        payload = {
            "type": "register",
            "miner_id": self.miner_id,
            "wallet": self.wallet,
            "public_key": self.pubkey,
            "signature": sig,
            "timestamp": ts,
            "miner_type": "pc"
        }
        try:
            await self.ws.send(json.dumps(payload))
            resp = await asyncio.wait_for(self.ws.recv(), timeout=30)
            data = json.loads(resp)
        except Exception as e:
            logger.error(f"{c(RED,'Registration error')}: {e}")
            return False

        if data.get("type") == "registered":
            self.registered = True
            self.balance = data.get("confirmed_balance", 0)
            self.difficulty = data.get("difficulty", 1.0)
            bal = self.balance // 1_000_000
            logger.info(f"{c(GREEN,'Registered')}  Balance: {c(YELLOW,str(bal)+' MCX')}  Diff: {c(CYAN,str(self.difficulty))}")
            asyncio.create_task(self.get_balance())
            return True

        if data.get("type") == "memory_challenge_request":
            seed = base64.b64decode(data["seed"])
            mem_size = data.get("memory_size", 8 * 1024 * 1024)
            logger.info(f"{c(YELLOW,'Memory challenge')} ({mem_size//1024//1024} MB)...")
            h = hashlib.sha256()
            counter = 0
            while h.digest_size * counter < mem_size:
                h.update(hashlib.sha256(seed + counter.to_bytes(8, "big")).digest())
                counter += 1
            commitment = h.digest()
            commit_b64 = base64.b64encode(commitment).decode()
            await self.ws.send(json.dumps({
                "type": "memory_challenge_response",
                "miner_id": self.miner_id,
                "seed": data["seed"],
                "commitment": commit_b64
            }))
            final = await asyncio.wait_for(self.ws.recv(), timeout=60)
            final_data = json.loads(final)
            if final_data.get("type") == "maze_init":
                self.registered = True
                logger.info(f"{c(GREEN,'Registered')} (already in round)")
                await self.handle_message(final_data)
                asyncio.create_task(self.get_balance())
                return True
            elif final_data.get("type") == "registered":
                self.registered = True
                self.balance = final_data.get("confirmed_balance", 0)
                self.difficulty = final_data.get("difficulty", 1.0)
                bal = self.balance // 1_000_000
                logger.info(f"{c(GREEN,'Registered')}  Balance: {c(YELLOW,str(bal)+' MCX')}  Diff: {c(CYAN,str(self.difficulty))}")
                asyncio.create_task(self.get_balance())
                return True
            else:
                logger.error(f"{c(RED,'Registration failed')} after challenge")
                return False

        logger.error(f"{c(RED,'Unexpected registration response')}")
        return False

    async def get_balance(self):
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"type": "get_balance", "wallet": self.wallet}))
        except:
            pass

    async def report_walls(self, force=False):
        if not self.registered or self.ws is None:
            return
        now = time.time()
        if not force and now - self.last_report < KNOWLEDGE_INTERVAL:
            return
        to_report = self.known_walls - self.reported_walls
        if not to_report and not force:
            return
        walls = [list(w) for w in to_report]
        payload = {
            "type": "knowledge_report",
            "miner_id": self.miner_id,
            "walls": walls,
            "preferences": {}
        }
        try:
            await self.ws.send(json.dumps(payload))
            self.reported_walls.update(to_report)
            self.last_report = now
        except Exception as e:
            logger.warning(f"Failed to send knowledge report: {e}")

    async def solve_pow_async(self, seed, difficulty):
        logger.info(f"{c(YELLOW,'Wall hit')} – solving PoW (difficulty {difficulty})...")
        loop = asyncio.get_event_loop()
        nonce = await loop.run_in_executor(None, solve_pow, seed, difficulty)
        return nonce

    async def navigate(self):
        if self.maze is None or self.current is None or self.goal is None:
            return

        self.steps = 0
        self.accepted = 0
        self.attempts = 0
        self.known_walls.clear()
        self.reported_walls.clear()
        self.last_report = 0.0
        self.start_time = time.time()
        self.pow_pending = False

        remaining = self.deadline - time.time()
        logger.info(f"{c(CYAN,'Block')} #{self.block_id}  "
                    f"Start: {c(YELLOW,str(self.start))}  "
                    f"Goal: {c(YELLOW,str(self.goal))}  "
                    f"Size: {c(MAGENTA,str(self.size)+'³')}  "
                    f"Diff: {c(CYAN,str(self.difficulty))}  "
                    f"Time: {c(YELLOW,f'{remaining:.1f}s')}")
        await self.report_walls(force=True)

        print(f"\n{c(WHITE+BOLD,'Time')}  {c(WHITE+BOLD,'Block')}  {c(WHITE+BOLD,'Step')}  {c(WHITE+BOLD,'Pos')}           {c(WHITE+BOLD,'Goal')}          {c(WHITE+BOLD,'SR')}  {c(WHITE+BOLD,'Size')}  {c(WHITE+BOLD,'Diff')}  {c(WHITE+BOLD,'Ping')}  {c(WHITE+BOLD,'Balance')}")
        print(c(DIM,'─'*95))

        while self.round_active and self.current != self.goal and time.time() < self.deadline:
            if self.pow_pending:
                await asyncio.sleep(0.1)
                continue

            await self.report_walls()

            path = a_star(self.maze, self.current, self.goal, self.known_walls)
            if path is None or len(path) == 0:
                await asyncio.sleep(0.2)
                continue

            next_pos = path[0]
            dx = next_pos[0] - self.current[0]
            dy = next_pos[1] - self.current[1]
            dz = next_pos[2] - self.current[2]
            dir_idx = -1
            for i, (ox, oy, oz) in enumerate(DIR_OFFSETS):
                if ox == dx and oy == dy and oz == dz:
                    dir_idx = i
                    break
            if dir_idx == -1:
                await asyncio.sleep(0.1)
                continue

            direction = DIR_NAMES[dir_idx]
            step = self.steps + 1

            self.attempts += 1
            self.pending_ack = asyncio.get_event_loop().create_future()
            sig = sign_message(self.privkey, f"{self.block_id}{self.miner_id}{direction}{step}")

            payload = {
                "type": "maze_move",
                "miner_id": self.miner_id,
                "direction": direction,
                "step": step,
                "signature": sig,
            }

            ping_start = time.time()
            try:
                await self.ws.send(json.dumps(payload))
                ack = await asyncio.wait_for(self.pending_ack, timeout=MOVE_TIMEOUT)
                self.last_ping = (time.time() - ping_start) * 1000

                if ack.get("type") == "wall_hit":
                    seed = ack["seed"]
                    difficulty = ack.get("difficulty", POW_DIFFICULTY)
                    self.pow_pending = True
                    nonce = await self.solve_pow_async(seed, difficulty)
                    await self.ws.send(json.dumps({
                        "type": "wall_hit_solve",
                        "miner_id": self.miner_id,
                        "nonce": nonce
                    }))
                    sol_ack = await asyncio.wait_for(self.pending_ack, timeout=MOVE_TIMEOUT)
                    if sol_ack.get("accepted"):
                        self.pow_pending = False
                        wall_pos = (self.current[0] + dx, self.current[1] + dy, self.current[2] + dz)
                        self.known_walls.add(wall_pos)
                        await self.report_walls(force=True)
                    else:
                        logger.warning(f"{c(RED,'PoW rejected')}, retrying...")
                        self.pow_pending = False
                    continue

                if ack.get("success"):
                    self.steps = step
                    self.accepted += 1
                    self.current = next_pos
                    if ack.get("state", {}).get("goal"):
                        self.goal = tuple(ack["state"]["goal"])
                    if ack.get("state", {}).get("difficulty"):
                        self.difficulty = ack["state"]["difficulty"]

                    bal = self.balance // 1_000_000
                    sr = (self.accepted / self.attempts) * 100 if self.attempts > 0 else 0
                    time_fmt = datetime.now().strftime('%H:%M:%S')

                    line = (f"{c(DIM,time_fmt)}  "
                            f"{c(CYAN,f'{self.block_id:5d}')}  "
                            f"{c(MAGENTA,f'{self.steps:4d}')}  "
                            f"{c(YELLOW,f'({self.current[0]},{self.current[1]},{self.current[2]})')}  "
                            f"{c(GREEN,f'({self.goal[0]},{self.goal[1]},{self.goal[2]})')}  "
                            f"{c(WHITE,f'{sr:5.1f}%')}  "
                            f"{c(BLUE,f'{self.size:3d}³')}  "
                            f"{c(CYAN,f'{self.difficulty:4.0f}')}  "
                            f"{c(YELLOW,f'{self.last_ping:5.0f}ms')}  "
                            f"{c(GREEN,f'{bal:5d} MCX')}")
                    print(line, flush=True)

                else:
                    if "wall" in str(ack.get("message", "")).lower():
                        wall_pos = (self.current[0] + dx, self.current[1] + dy, self.current[2] + dz)
                        self.known_walls.add(wall_pos)
                        await self.report_walls(force=True)
            except asyncio.TimeoutError:
                logger.warning(f"Move timeout for step {step}")
            except Exception as e:
                logger.error(f"Move error: {e}")
            await asyncio.sleep(0.05)

        if self.current == self.goal:
            logger.info(f"{c(GREEN,'🏁 Goal reached')} in {self.steps} steps!")
        else:
            logger.info(f"{c(YELLOW,'⏳ Round finished')}")
        self.round_active = False

    async def handle_message(self, data):
        msg_type = data.get("type")

        if msg_type == "maze_init":
            if self.round_active and data.get("block_id") == self.block_id:
                if "goal" in data:
                    self.goal = tuple(data["goal"])
                if "deadline" in data:
                    self.deadline = data["deadline"]
                if "maze" in data:
                    self.maze = data["maze"]
                if "difficulty" in data:
                    self.difficulty = data["difficulty"]
                return

            self.block_id = data["block_id"]
            self.size = data["size"]
            self.maze = data["maze"]
            self.start = tuple(data["start"])
            self.current = self.start
            self.goal = tuple(data["goal"])
            self.deadline = data["deadline"]
            self.max_steps = data.get("max_path_length", 10**9)
            self.difficulty = data.get("difficulty", 1.0)
            self.round_active = True
            self.steps = 0
            self.accepted = 0
            self.attempts = 0
            self.known_walls.clear()
            self.reported_walls.clear()
            self.last_report = 0.0
            self.pow_pending = False

            if self.deadline <= time.time() + 2:
                logger.warning(f"Block {self.block_id} already expired.")
                self.round_active = False
                return

            if self.nav_task and not self.nav_task.done():
                self.nav_task.cancel()
            self.nav_task = asyncio.create_task(self.navigate())

        elif msg_type == "maze_state":
            state = data.get("state", {})
            if state.get("current"):
                self.current = tuple(state["current"])
            if state.get("goal"):
                self.goal = tuple(state["goal"])
            if state.get("deadline"):
                self.deadline = state["deadline"]
            if state.get("block_id"):
                self.block_id = state["block_id"]
            if state.get("difficulty"):
                self.difficulty = state["difficulty"]
            if not self.round_active and self.maze is not None:
                self.round_active = True
                if self.nav_task and not self.nav_task.done():
                    self.nav_task.cancel()
                self.nav_task = asyncio.create_task(self.navigate())

        elif msg_type == "maze_move_ack":
            if self.pending_ack and not self.pending_ack.done():
                self.pending_ack.set_result(data)
            if data.get("success"):
                if data.get("state", {}).get("current"):
                    self.current = tuple(data["state"]["current"])
                if data.get("state", {}).get("goal"):
                    self.goal = tuple(data["state"]["goal"])
                if data.get("state", {}).get("difficulty"):
                    self.difficulty = data["state"]["difficulty"]
                if data.get("state", {}).get("finished"):
                    self.round_active = False
                    logger.info(f"{c(GREEN,'🏁 Goal confirmed by node!')}")
            else:
                pass

        elif msg_type == "wall_hit":
            if self.pending_ack and not self.pending_ack.done():
                self.pending_ack.set_result(data)

        elif msg_type == "wall_hit_solved":
            if self.pending_ack and not self.pending_ack.done():
                self.pending_ack.set_result(data)

        elif msg_type == "balance":
            old = self.balance
            self.balance = data.get("balance", 0)
            bal = self.balance // 1_000_000
            if self.balance != old:
                logger.info(f"Balance updated: {c(YELLOW,str(bal)+' MCX')}")

        elif msg_type == "block_accepted":
            reward = data.get("reward", 0)
            self.balance += reward
            bal = self.balance // 1_000_000
            logger.info(f"{c(GREEN,'🎉 Block reward')} +{c(YELLOW,str(reward//1_000_000)+' MCX')}  "
                        f"New balance: {c(YELLOW,str(bal)+' MCX')}")

        elif msg_type == "knowledge_ack":
            if not data.get("accepted"):
                logger.warning(f"Knowledge report rejected: {data.get('message', '')}")

        elif msg_type == "error":
            logger.error(f"Node error: {data.get('message')}")

    async def heartbeat(self):
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self.ws and self.ws.open and self.registered:
                try:
                    await self.ws.send(json.dumps({
                        "type": "uptime_ping",
                        "miner_id": self.miner_id,
                        "uptime_seconds": int(time.time())
                    }))
                except:
                    pass

    async def run(self):
        print(BANNER)
        logger.info(f"Wallet: {c(CYAN,self.wallet)}")
        logger.info(f"Miner ID: {c(CYAN,self.miner_id)}")

        self.heartbeat_task = asyncio.create_task(self.heartbeat())

        while self.running:
            if not self.node_url:
                nodes = await discover_nodes()
                if not nodes:
                    logger.warning("No nodes discovered, retrying...")
                    await asyncio.sleep(10)
                    continue
                self.node_url = nodes[0]
                logger.info(f"Using node: {c(CYAN,self.node_url)}")

            if not await self.connect(self.node_url):
                self.reconn_attempts += 1
                if self.reconn_attempts > MAX_RECONNECT:
                    logger.warning(f"Giving up on {self.node_url}, trying next...")
                    self.node_url = None
                    self.reconn_attempts = 0
                await asyncio.sleep(5)
                continue

            if await self.register():
                self.registered = True
                try:
                    async for msg in self.ws:
                        if not msg:
                            continue
                        try:
                            data = json.loads(msg)
                            await self.handle_message(data)
                        except json.JSONDecodeError:
                            logger.warning("Invalid JSON")
                        except Exception as e:
                            logger.error(f"Message error: {e}")
                except websockets.ConnectionClosed:
                    logger.warning("Connection closed, reconnecting...")
                except Exception as e:
                    logger.error(f"Loop error: {e}")
                finally:
                    await self.disconnect()
                    self.registered = False
            else:
                logger.warning("Registration failed, retrying...")
                await asyncio.sleep(10)
            await asyncio.sleep(1)

        if self.heartbeat_task:
            self.heartbeat_task.cancel()

    async def stop(self):
        self.running = False
        if self.nav_task:
            self.nav_task.cancel()
        await self.disconnect()

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", help="WebSocket URL")
    parser.add_argument("--wallet", help="Wallet address")
    parser.add_argument("--privkey", help="Private key hex")
    parser.add_argument("--miner-id", help="Custom miner ID")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not args.wallet or not args.privkey:
        w, pk = load_wallet()
        if w and pk:
            args.wallet, args.privkey = w, pk
            logger.info(f"Loaded wallet: {c(CYAN,args.wallet)}")
        else:
            w, pk, _ = generate_keypair()
            args.wallet, args.privkey = w, pk
            save_wallet(args.wallet, args.privkey)
            logger.info(f"Generated new wallet: {c(CYAN,args.wallet)}")
            logger.info(f"Private key: {args.privkey}")
            logger.warning("SAVE YOUR PRIVATE KEY!")

    miner = PCMiner(args.wallet, args.privkey, args.miner_id, args.node, verbose=args.verbose)
    try:
        await miner.run()
    except KeyboardInterrupt:
        logger.info("Shutdown by user.")
    finally:
        await miner.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Interrupted")
        sys.exit(0)
