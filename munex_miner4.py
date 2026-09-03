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
from collections import deque

import websockets
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = \
    "\033[30m", "\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[35m", "\033[36m", "\033[37m"

def c(colour, text):
    return f"{colour}{text}{RESET}"

BANNER = f"""
{c(MAGENTA,'  ███╗   ███╗██╗   ██╗███╗   ██╗███████╗██╗  ██╗')}{c(CYAN,'     ███╗   ███╗██╗███╗   ██╗███████╗██████╗ ')}
{c(MAGENTA,'  ████╗ ████║██║   ██║████╗  ██║██╔════╝╚██╗██╔╝')}{c(CYAN,'     ████╗ ████║██║████╗  ██║██╔════╝██╔══██╗')}
{c(MAGENTA,'  ██╔████╔██║██║   ██║██╔██╗ ██║█████╗   ╚███╔╝ ')}{c(CYAN,'     ██╔████╔██║██║██╔██╗ ██║█████╗  ██████╔╝')}
{c(MAGENTA,'  ██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝   ██╔██╗ ')}{c(CYAN,'     ██║╚██╔╝██║██║██║╚██╗██║██╔══╝  ██╔══██╗')}
{c(MAGENTA,'  ██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████╗██╔╝ ██╗')}{c(CYAN,'     ██║ ╚═╝ ██║██║██║ ╚████║███████╗██║  ██║')}
{c(MAGENTA,'  ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝')}{c(CYAN,'     ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝')}
{c(GREEN,'  ───  SMART MINER with KNOWLEDGE KEEP‑ALIVE  ───')}
{c(DIM,'  v64.3 – Periodic empty reports, no more stale knowledge')}
"""
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
KNOWLEDGE_INTERVAL = 3.0           # min time between reports when new walls exist
POW_DIFFICULTY = 16
BALANCE_FETCH_INTERVAL = 30
IDLE_TIMEOUT = 600

# New constant: send an empty knowledge report every 25s to keep node happy
KNOWLEDGE_KEEPALIVE_INTERVAL = 25   # seconds

_shared_known_walls = set()
_shared_lock = asyncio.Lock()

async def add_shared_walls(walls):
    async with _shared_lock:
        _shared_known_walls.update(walls)

def get_shared_walls():
    return _shared_known_walls
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
def a_star(maze, start, goal, known_walls, exploration_factor=1.0):
    size = len(maze)
    sx, sy, sz = start
    gx, gy, gz = goal
    if not (0 <= sx < size and 0 <= sy < size and 0 <= sz < size) or \
       not (0 <= gx < size and 0 <= gy < size and 0 <= gz < size):
        return None
    if maze[sz][sy][sx] == 1 or maze[gz][gy][gx] == 1:
        return None

    unknown_ratio = 1.0 - (len(known_walls) / (size*size*size)) if size > 0 else 0
    weight = 1.0 + exploration_factor * unknown_ratio

    open_set = [(0, start)]
    came_from = {}
    g_score = {start: 0}
    f_score = {start: weight * (abs(sx-gx)+abs(sy-gy)+abs(sz-gz))}

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
            if maze[nz][ny][nx] == 1 or (nx, ny, nz) in known_walls:
                continue
            nxt = (nx, ny, nz)
            tentative = g_score[cur] + 1
            if tentative < g_score.get(nxt, float('inf')):
                came_from[nxt] = cur
                g_score[nxt] = tentative
                f_score[nxt] = tentative + weight * (abs(nx-gx)+abs(ny-gy)+abs(nz-gz))
                heapq.heappush(open_set, (f_score[nxt], nxt))
    return None

def bfs_explore(maze, start, goal, known_walls, max_steps=1000):
    size = len(maze)
    sx, sy, sz = start
    gx, gy, gz = goal
    if (sx, sy, sz) == (gx, gy, gz):
        return []
    visited = {start}
    queue = deque([(start, [])])
    while queue:
        (cx, cy, cz), path = queue.popleft()
        if len(path) >= max_steps:
            continue
        for dx, dy, dz in DIR_OFFSETS:
            nx, ny, nz = cx+dx, cy+dy, cz+dz
            if not (0 <= nx < size and 0 <= ny < size and 0 <= nz < size):
                continue
            if maze[nz][ny][nx] == 1 or (nx, ny, nz) in known_walls:
                continue
            if (nx, ny, nz) == (gx, gy, gz):
                return path + [(nx, ny, nz)]
            if (nx, ny, nz) not in visited:
                visited.add((nx, ny, nz))
                queue.append(((nx, ny, nz), path + [(nx, ny, nz)]))
    return None

def random_valid_move(maze, pos, known_walls):
    size = len(maze)
    x, y, z = pos
    candidates = []
    for idx, (dx, dy, dz) in enumerate(DIR_OFFSETS):
        nx, ny, nz = x+dx, y+dy, z+dz
        if 0 <= nx < size and 0 <= ny < size and 0 <= nz < size:
            if maze[nz][ny][nx] == 0 and (nx, ny, nz) not in known_walls:
                candidates.append(idx)
    if candidates:
        return random.choice(candidates)
    return -1

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

# ─── Main Miner Class ──────────────────────────────────────────
class SmartMiner:
    def __init__(self, wallet, privkey, miner_id=None, node_url=None, verbose=False,
                 core_suffix="", balance_callback=None):
        self.wallet = wallet
        self.privkey = privkey
        self.pubkey = pubkey_from_priv(privkey)
        if miner_id is None:
            rand = secrets.token_hex(8)
            seed = f"{self.wallet}_{time.time()}_{rand}"
            digest = hashlib.sha256(seed.encode()).hexdigest()[:24].upper()
            base_id = f"PC_{digest}"
        else:
            base_id = miner_id
        if core_suffix:
            self.miner_id = f"{base_id}_{core_suffix}"
        else:
            self.miner_id = base_id
        self.node_url = node_url
        self.ws = None
        self.registered = False
        self.balance = 0
        self.running = True
        self.verbose = verbose
        self.balance_callback = balance_callback

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

        self.local_walls = set()
        self.reported_walls = set()
        self.last_report = 0.0
        self.exploration_factor = 1.0

        self.nav_task = None
        self.pending_ack = None
        self.heartbeat_task = None
        self.balance_task = None
        self.reconn_attempts = 0
        self.start_time = 0.0
        self.last_move_duration = 0.0
        self.pow_pending = False
        self.last_ping = 0.0
        self.last_message_time = time.time()
        self.stuck_counter = 0
        self.path_fail_count = 0

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
            logger.info(f"{c(GREEN,'Connected')} to {c(CYAN,url)} (miner {self.miner_id[-8:]})")
            self.last_ping = (time.time() - start) * 1000
            return True
        except Exception as e:
            logger.error(f"{c(RED,'Connect failed')} for {self.miner_id[-8:]}: {e}")
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
            logger.error(f"{c(RED,'Registration error')} for {self.miner_id[-8:]}: {e}")
            return False

        if data.get("type") == "registered":
            self.registered = True
            self.balance = data.get("confirmed_balance", 0)
            self.difficulty = data.get("difficulty", 1.0)
            bal = self.balance // 1_000_000
            logger.info(f"{c(GREEN,'Registered')} {self.miner_id[-8:]}  Balance: {c(YELLOW,str(bal)+' MCX')}  Diff: {c(CYAN,str(self.difficulty))}")
            asyncio.create_task(self.fetch_balance())
            return True

        if data.get("type") == "memory_challenge_request":
            seed = base64.b64decode(data["seed"])
            mem_size = data.get("memory_size", 8 * 1024 * 1024)
            logger.info(f"{c(YELLOW,'Memory challenge')} for {self.miner_id[-8:]} ({mem_size//1024//1024} MB)...")
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
                logger.info(f"{c(GREEN,'Registered')} {self.miner_id[-8:]} (already in round)")
                await self.handle_message(final_data)
                asyncio.create_task(self.fetch_balance())
                return True
            elif final_data.get("type") == "registered":
                self.registered = True
                self.balance = final_data.get("confirmed_balance", 0)
                self.difficulty = final_data.get("difficulty", 1.0)
                bal = self.balance // 1_000_000
                logger.info(f"{c(GREEN,'Registered')} {self.miner_id[-8:]}  Balance: {c(YELLOW,str(bal)+' MCX')}  Diff: {c(CYAN,str(self.difficulty))}")
                asyncio.create_task(self.fetch_balance())
                return True
            else:
                logger.error(f"{c(RED,'Registration failed')} for {self.miner_id[-8:]} after challenge")
                return False

        logger.error(f"{c(RED,'Unexpected registration response')} for {self.miner_id[-8:]}")
        return False

    async def fetch_balance(self):
        if self.ws is None or not self.registered:
            return
        try:
            await self.ws.send(json.dumps({"type": "get_balance", "wallet": self.wallet}))
        except:
            pass

    async def balance_fetch_loop(self):
        while self.running:
            await asyncio.sleep(BALANCE_FETCH_INTERVAL)
            if self.registered and self.ws:
                await self.fetch_balance()

    # ─── MODIFIED: Always send a report if we haven't sent for KNOWLEDGE_KEEPALIVE_INTERVAL ───
    async def report_walls(self, force=False):
        if not self.registered or self.ws is None:
            return
        now = time.time()
        # If forced, or new walls to report, or it's time for a keep-alive
        to_report = self.local_walls - self.reported_walls
        if force or to_report or (now - self.last_report >= KNOWLEDGE_KEEPALIVE_INTERVAL):
            walls = [list(w) for w in to_report] if to_report else []
            payload = {
                "type": "knowledge_report",
                "miner_id": self.miner_id,
                "walls": walls,
                "preferences": {}
            }
            try:
                await self.ws.send(json.dumps(payload))
                self.reported_walls.update(to_report)  # mark as reported
                self.last_report = now
                if self.verbose and walls:
                    logger.debug(f"Knowledge report sent: {len(walls)} walls")
                # Share new walls globally
                if to_report:
                    await add_shared_walls(to_report)
            except Exception as e:
                logger.warning(f"Failed to send knowledge report: {e}")

    async def solve_pow_async(self, seed, difficulty):
        logger.info(f"{c(YELLOW,'Wall hit')} for {self.miner_id[-8:]} – solving PoW (diff {difficulty})...")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, solve_pow, seed, difficulty)

    async def navigate(self):
        if self.maze is None or self.current is None or self.goal is None:
            return

        self.steps = 0
        self.accepted = 0
        self.attempts = 0
        self.local_walls.clear()
        self.reported_walls.clear()
        self.last_report = 0.0
        self.start_time = time.time()
        self.pow_pending = False
        self.stuck_counter = 0
        self.path_fail_count = 0

        remaining = self.deadline - time.time()
        logger.info(f"{c(CYAN,'Block')} #{self.block_id}  "
                    f"Start: {c(YELLOW,str(self.start))}  "
                    f"Goal: {c(YELLOW,str(self.goal))}  "
                    f"Size: {c(MAGENTA,str(self.size)+'³')}  "
                    f"Diff: {c(CYAN,str(self.difficulty))}  "
                    f"Time: {c(YELLOW,f'{remaining:.1f}s')}  "
                    f"Miner: {c(CYAN,self.miner_id[-8:])}")

        # Force initial report
        await self.report_walls(force=True)

        print(f"\n{c(WHITE+BOLD,'Time')}  {c(WHITE+BOLD,'Block')}  {c(WHITE+BOLD,'Step')}  {c(WHITE+BOLD,'Pos')}           {c(WHITE+BOLD,'Goal')}          {c(WHITE+BOLD,'SR')}  {c(WHITE+BOLD,'Size')}  {c(WHITE+BOLD,'Diff')}  {c(WHITE+BOLD,'Ping')}  {c(WHITE+BOLD,'Balance')}  {c(WHITE+BOLD,'Miner')}")
        print(c(DIM,'─'*110))

        while self.round_active and self.current != self.goal and time.time() < self.deadline:
            if self.pow_pending:
                await asyncio.sleep(0.1)
                continue

            # Call report_walls – it will send a keep‑alive if needed
            await self.report_walls()

            known = self.local_walls | get_shared_walls()

            # Adjust exploration factor
            if self.stuck_counter > 3:
                self.exploration_factor = min(2.0, self.exploration_factor * 1.1)
            else:
                self.exploration_factor = max(1.0, self.exploration_factor * 0.99)

            # Try A*
            path = a_star(self.maze, self.current, self.goal, known, self.exploration_factor)
            if path is None or len(path) == 0:
                # Try BFS
                if self.verbose:
                    logger.debug(f"A* failed for {self.miner_id[-8:]}, falling back to BFS")
                path = bfs_explore(self.maze, self.current, self.goal, known, max_steps=200)
                if path is None:
                    self.path_fail_count += 1
                    if self.path_fail_count % 5 == 0:
                        logger.warning(f"Pathfinding failed {self.path_fail_count} times for {self.miner_id[-8:]}, using random move")
                    # Random fallback
                    dir_idx = random_valid_move(self.maze, self.current, known)
                    if dir_idx == -1:
                        await asyncio.sleep(0.5)
                        continue
                    dx, dy, dz = DIR_OFFSETS[dir_idx]
                    next_pos = (self.current[0] + dx, self.current[1] + dy, self.current[2] + dz)
                    path = [next_pos]
                else:
                    self.path_fail_count = 0
            else:
                self.path_fail_count = 0

            if len(path) == 0:
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
                        self.local_walls.add(wall_pos)
                        self.stuck_counter = 0
                        await self.report_walls(force=True)
                    else:
                        logger.warning(f"{c(RED,'PoW rejected')} for {self.miner_id[-8:]}, retrying...")
                        self.pow_pending = False
                        self.stuck_counter += 1
                    continue

                if ack.get("success"):
                    self.steps = step
                    self.accepted += 1
                    self.current = next_pos
                    self.stuck_counter = 0
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
                            f"{c(GREEN,f'{bal:5d} MCX')}  "
                            f"{c(DIM,self.miner_id[-8:])}")
                    print(line, flush=True)

                else:
                    if "wall" in str(ack.get("message", "")).lower():
                        wall_pos = (self.current[0] + dx, self.current[1] + dy, self.current[2] + dz)
                        self.local_walls.add(wall_pos)
                        self.stuck_counter += 1
                        await self.report_walls(force=True)
                    else:
                        self.stuck_counter += 1
            except asyncio.TimeoutError:
                logger.warning(f"Move timeout for step {step} on {self.miner_id[-8:]}")
                self.stuck_counter += 1
            except Exception as e:
                logger.error(f"Move error on {self.miner_id[-8:]}: {e}")
                self.stuck_counter += 1

            if self.stuck_counter > 5 and random.random() < 0.3:
                dir_idx = random_valid_move(self.maze, self.current, known)
                if dir_idx != -1:
                    pass  # will try in next iteration

            await asyncio.sleep(0.05)

        if self.current == self.goal:
            logger.info(f"{c(GREEN,'🏁 Goal reached')} in {self.steps} steps for {self.miner_id[-8:]}")
        else:
            logger.info(f"{c(YELLOW,'⏳ Round finished')} for {self.miner_id[-8:]}")
        self.round_active = False

    async def handle_message(self, data):
        self.last_message_time = time.time()
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
            self.local_walls.clear()
            self.reported_walls.clear()
            self.last_report = 0.0
            self.pow_pending = False
            self.stuck_counter = 0
            self.exploration_factor = 1.0

            if self.deadline <= time.time() + 2:
                logger.warning(f"Block {self.block_id} already expired for {self.miner_id[-8:]}")
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
                    logger.info(f"{c(GREEN,'🏁 Goal confirmed by node!')} for {self.miner_id[-8:]}")
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
            if self.balance != old:
                bal = self.balance // 1_000_000
                logger.info(f"Balance updated for {self.miner_id[-8:]}: {c(YELLOW,str(bal)+' MCX')} (was {old//1_000_000} MCX)")
                if self.balance_callback:
                    self.balance_callback(self.miner_id, self.balance)

        elif msg_type == "block_accepted":
            reward = data.get("reward", 0)
            self.balance += reward
            bal = self.balance // 1_000_000
            logger.info(f"{c(GREEN,'🎉 Block reward')} +{c(YELLOW,str(reward//1_000_000)+' MCX')}  "
                        f"New balance: {c(YELLOW,str(bal)+' MCX')} for {self.miner_id[-8:]}")

        elif msg_type == "knowledge_ack":
            if not data.get("accepted"):
                logger.warning(f"Knowledge report rejected for {self.miner_id[-8:]}: {data.get('message', '')}")

        elif msg_type == "error":
            logger.error(f"Node error for {self.miner_id[-8:]}: {data.get('message')}")

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

    async def keepalive_monitor(self):
        while self.running:
            await asyncio.sleep(10)
            if self.ws and self.ws.open and self.registered:
                if time.time() - self.last_message_time > IDLE_TIMEOUT:
                    logger.warning(f"No message for {IDLE_TIMEOUT}s for {self.miner_id[-8:]}, forcing reconnect")
                    await self.disconnect()
                    await asyncio.sleep(1)

    async def run(self):
        logger.info(f"Starting miner {c(CYAN,self.miner_id)}")
        self.heartbeat_task = asyncio.create_task(self.heartbeat())
        self.balance_task = asyncio.create_task(self.balance_fetch_loop())
        self.keepalive_task = asyncio.create_task(self.keepalive_monitor())

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
                except websockets.ConnectionClosed as e:
                    logger.warning(f"Connection closed: {e}, reconnecting...")
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
        if self.balance_task:
            self.balance_task.cancel()
        if self.keepalive_task:
            self.keepalive_task.cancel()

    async def stop(self):
        self.running = False
        if self.nav_task:
            self.nav_task.cancel()
        await self.disconnect()

# ─── Multi‑miner runner ────────────────────────────────────
async def run_miners(wallet, privkey, node_url, num_cores, mode, verbose):
    miners = []
    if mode == "multi":
        for i in range(num_cores):
            suffix = f"core{i+1}"
            miner = SmartMiner(wallet, privkey, miner_id=None, node_url=node_url,
                               verbose=verbose, core_suffix=suffix)
            miners.append(miner)
        logger.info(f"Starting {num_cores} miners in multi-core mode.")
        tasks = [asyncio.create_task(miner.run()) for miner in miners]
        await asyncio.gather(*tasks)
    else:
        miner = SmartMiner(wallet, privkey, miner_id=None, node_url=node_url,
                           verbose=verbose, core_suffix="genius")
        await miner.run()

# ─── Main ──────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="Smart PoMR Miner with Knowledge Keep-alive")
    parser.add_argument("--node", help="WebSocket URL (overrides discovery)")
    parser.add_argument("--wallet", help="Wallet address")
    parser.add_argument("--privkey", help="Private key hex")
    parser.add_argument("--miner-id", help="Custom base miner ID")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--cores", type=int, help="Number of CPU cores to use (default: all)")
    parser.add_argument("--mode", choices=["multi", "single"], default=None,
                        help="'multi' for one miner per core, 'single' for a single genius miner")
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

    available_cores = os.cpu_count() or 1
    if args.cores is None:
        print(f"\n{c(CYAN, 'How many CPU cores do you want to use? (default: ' + str(available_cores) + ')')}{RESET}")
        try:
            cores_input = input("> ").strip()
            if cores_input:
                args.cores = int(cores_input)
            else:
                args.cores = available_cores
        except:
            args.cores = available_cores
    args.cores = max(1, min(args.cores, available_cores))

    if args.mode is None:
        print(f"\n{c(CYAN, 'Do you want to run multiple miners (one per core) or a single genius miner?')}{RESET}")
        print("  1) Multi-miner (one instance per core, each with its own miner ID)")
        print("  2) Single-genius miner (uses all cores for pathfinding and mining)")
        try:
            mode_choice = input("Enter 1 or 2 (default: 2): ").strip()
            if mode_choice == "1":
                args.mode = "multi"
            else:
                args.mode = "single"
        except:
            args.mode = "single"

    logger.info(f"Using {args.cores} cores in {args.mode} mode.")
    await run_miners(args.wallet, args.privkey, args.node, args.cores, args.mode, args.verbose)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Interrupted")
        sys.exit(0)