#!/usr/bin/env python3

import sys
import os
import json
import time
import base64
import hashlib
import secrets
import random
import asyncio
import argparse
import logging
import socket
import heapq
from collections import deque

import websockets
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Miner")

RESET = "\033[0m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
MAGENTA = "\033[95m"

BANNER = """
  ██████╗  ██████╗     ███╗   ███╗██╗███╗   ██╗███████╗██████╗ 
  ██╔══██╗██╔════╝     ████╗ ████║██║████╗  ██║██╔════╝██╔══██╗
  ██████╔╝██║  ███╗    ██╔████╔██║██║██╔██╗ ██║█████╗  ██████╔╝
  ██╔═══╝ ██║   ██║    ██║╚██╔╝██║██║██║╚██╗██║██╔══╝  ██╔══██╗
  ██║     ╚██████╔╝    ██║ ╚═╝ ██║██║██║ ╚████║███████╗██║  ██║
  ╚═╝      ╚═════╝     ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝
"""

WALLET_FILE = "miner_wallet.json"
DNS_SEEDS = ["munexseed.duckdns.org"]
FALLBACK_NODE = "ws://munexseed.duckdns.org:8080/ws"
WS_PATH = "/ws"
WS_PORT = 8080

DIR_NAMES = ["east", "west", "north", "south", "up", "down"]
DIR_OFFSETS = [(1,0,0), (-1,0,0), (0,1,0), (0,-1,0), (0,0,1), (0,0,-1)]

MOVE_TIMEOUT = 10.0
HEARTBEAT_INTERVAL = 10
MAX_RECONNECT = 5
KNOWLEDGE_INTERVAL = 3.0

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

def resolve_dns(domain, port=WS_PORT):
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

class PCMiner:
    def __init__(self, wallet, privkey, miner_id=None, node_url=None):
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

        self.maze = None
        self.size = 0
        self.start = None
        self.current = None
        self.goal = None
        self.deadline = 0.0
        self.max_path_length = 0
        self.block_id = -1
        self.steps = 0
        self.accepted = 0
        self.attempts = 0
        self.round_active = False

        self.known_walls = set()
        self.reported_walls = set()
        self.last_report = 0.0
        self.nav_task = None
        self.pending_ack = None
        self.heartbeat_task = None
        self.reconn_attempts = 0
        self.start_time = 0.0
        self.last_move_duration = 0.0

    async def connect(self, url):
        try:
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
            logger.info(f"Connected to {url}")
            return True
        except Exception as e:
            logger.error(f"Connect failed: {e}")
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
            logger.error(f"Registration error: {e}")
            return False

        if data.get("type") == "registered":
            self.registered = True
            self.balance = data.get("confirmed_balance", 0)
            bal = self.balance // 1_000_000
            logger.info(f"Registered! Balance: {bal} MCX")
            asyncio.create_task(self.get_balance())
            return True

        if data.get("type") == "memory_challenge_request":
            seed = base64.b64decode(data["seed"])
            mem_size = data.get("memory_size", 8 * 1024 * 1024)
            logger.info(f"Memory challenge ({mem_size//1024//1024} MB)...")
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
                logger.info("Registered (already in round)")
                await self.handle_message(final_data)
                asyncio.create_task(self.get_balance())
                return True
            elif final_data.get("type") == "registered":
                self.registered = True
                self.balance = final_data.get("confirmed_balance", 0)
                bal = self.balance // 1_000_000
                logger.info(f"Registered! Balance: {bal} MCX")
                asyncio.create_task(self.get_balance())
                return True
            else:
                logger.error(f"Registration failed after challenge: {final_data}")
                return False

        logger.error(f"Unexpected registration response: {data}")
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
        if not self.known_walls:
            return
        to_report = self.known_walls - self.reported_walls
        if not to_report:
            return
        batch = list(to_report)[:30]
        walls = [list(w) for w in batch]
        payload = {
            "type": "knowledge_report",
            "miner_id": self.miner_id,
            "walls": walls,
            "preferences": {}
        }
        try:
            await self.ws.send(json.dumps(payload))
            self.reported_walls.update(batch)
            self.last_report = now
        except:
            pass

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

        logger.info(f"Block {self.block_id}: start {self.start}, goal {self.goal}, max steps {self.max_path_length}")

        while self.round_active and self.current != self.goal and time.time() < self.deadline:
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
            if step > self.max_path_length:
                logger.warning(f"Step {step} exceeds max {self.max_path_length}, stopping.")
                self.round_active = False
                break

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

            move_start = time.time()
            try:
                await self.ws.send(json.dumps(payload))
                ack = await asyncio.wait_for(self.pending_ack, timeout=MOVE_TIMEOUT)
                self.last_move_duration = time.time() - move_start
                if ack.get("success"):
                    self.steps = step
                    self.accepted += 1
                    self.current = next_pos
                    if ack.get("state", {}).get("goal"):
                        self.goal = tuple(ack["state"]["goal"])
                    time_left = max(0.0, self.deadline - time.time())
                    bal = self.balance // 1_000_000
                    sr = (self.accepted / self.attempts) if self.attempts > 0 else 1.0
                    print(f"\r{BOLD}[{self.steps:3d}/{self.max_path_length:3d}]{RESET} "
                          f"pos {self.current} -> goal {self.goal} | SR {sr*100:3.0f}% | "
                          f"time left {time_left:4.1f}s | {MAGENTA}bal {bal} MCX{RESET}",
                          end="", flush=True)
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
            print(f"\n{GREEN}Goal reached in {self.steps} steps!{RESET}")
        else:
            print(f"\n{YELLOW}Round finished.{RESET}")
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
                return

            self.block_id = data["block_id"]
            self.size = data["size"]
            self.maze = data["maze"]
            self.start = tuple(data["start"])
            self.current = self.start
            self.goal = tuple(data["goal"])
            self.deadline = data["deadline"]
            self.max_path_length = data.get("max_path_length", 10**9)
            self.round_active = True
            self.steps = 0
            self.accepted = 0
            self.attempts = 0
            self.known_walls.clear()
            self.reported_walls.clear()
            self.last_report = 0.0

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
                if data.get("state", {}).get("finished"):
                    self.round_active = False
                    print(f"\n{GREEN}Goal confirmed by node!{RESET}")
            else:
                pass

        elif msg_type == "balance":
            self.balance = data.get("balance", 0)
            bal = self.balance // 1_000_000
            logger.info(f"Balance updated: {bal} MCX")

        elif msg_type == "block_accepted":
            reward = data.get("reward", 0)
            self.balance += reward
            bal = self.balance // 1_000_000
            logger.info(f"Block reward: +{reward//1_000_000} MCX | New: {bal} MCX")

        elif msg_type == "knowledge_ack":
            if not data.get("accepted"):
                logger.warning("Knowledge report rejected: " + data.get("message", ""))

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
        logger.info(f"Wallet: {self.wallet}")
        logger.info(f"Miner ID: {self.miner_id}")
        logger.info(f"Private key: {self.privkey[:8]}...")

        self.heartbeat_task = asyncio.create_task(self.heartbeat())

        while self.running:
            if not self.node_url:
                nodes = await discover_nodes()
                if not nodes:
                    logger.warning("No nodes discovered, retrying...")
                    await asyncio.sleep(10)
                    continue
                self.node_url = nodes[0]
                logger.info(f"Using node: {self.node_url}")

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
    args = parser.parse_args()

    if not args.wallet or not args.privkey:
        w, pk = load_wallet()
        if w and pk:
            args.wallet, args.privkey = w, pk
            logger.info(f"Loaded wallet: {args.wallet}")
        else:
            w, pk, _ = generate_keypair()
            args.wallet, args.privkey = w, pk
            save_wallet(args.wallet, args.privkey)
            logger.info(f"Generated new wallet: {args.wallet}")
            logger.info(f"Private key: {args.privkey}")
            logger.warning("SAVE YOUR PRIVATE KEY!")

    miner = PCMiner(args.wallet, args.privkey, args.miner_id, args.node)
    try:
        await miner.run()
    except KeyboardInterrupt:
        logger.info("Shutdown.")
    finally:
        await miner.stop()

if __name__ == "__main__":
    asyncio.run(main())
