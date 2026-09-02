#!/usr/bin/env python3

import os
import sys
import time
import json
import base64
import random
import asyncio
import hashlib
import secrets
import argparse
import socket
import logging
from collections import deque

import websockets
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW = "\033[93m"
CYAN  = "\033[96m"
RESET = "\033[0m"
BOLD  = "\033[1m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("apex")

BANNER = f"""
{CYAN}{BOLD}
  █████╗ ██████╗ ███████╗██╗  ██╗    ███╗   ███╗██╗███╗   ██╗███████╗██████╗ 
 ██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝    ████╗ ████║██║████╗  ██║██╔════╝██╔══██╗
 ███████║██████╔╝█████╗   ╚███╔╝     ██╔████╔██║██║██╔██╗ ██║█████╗  ██████╔╝
 ██╔══██║██╔═══╝ ██╔══╝   ██╔██╗     ██║╚██╔╝██║██║██║╚██╗██║██╔══╝  ██╔══██╗
 ██║  ██║██║     ███████╗██╔╝ ██╗    ██║ ╚═╝ ██║██║██║ ╚████║███████╗██║  ██║
 ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝    ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝
{RESET}{CYAN}{BOLD}  ─── APEX MOBILE MINER – PoMR READY ───{RESET}
"""

WALLET_FILE = "miner_wallet.json"
DNS_SEEDS = ["munexseed.duckdns.org"]
FALLBACK_NODE = "ws://munexseed.duckdns.org:8080/ws"
WS_DEFAULT_PORT = 8080
WS_PATH = "/ws"

DIR_NAMES = ["east", "west", "north", "south", "up", "down"]
DIR_OFFSETS = [(1,0,0), (-1,0,0), (0,1,0), (0,-1,0), (0,0,1), (0,0,-1)]

MOVE_ACK_TIMEOUT = 15.0
HEARTBEAT_INTERVAL = 10
MAX_RECONNECT_ATTEMPTS = 5
MEMORY_CHALLENGE_SIZE = 8 * 1024 * 1024
KNOWLEDGE_ACK_TIMEOUT = 5.0

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

def pubkey_hex_from_priv(priv_hex):
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

def save_wallet(address, privkey):
    with open(WALLET_FILE, "w") as f:
        json.dump({"wallet": address, "privkey": privkey}, f, indent=2)
    try:
        os.chmod(WALLET_FILE, 0o600)
    except:
        pass

def memory_challenge_commitment(seed: bytes, memory_size: int) -> bytes:
    h = hashlib.sha256()
    counter = 0
    produced = 0
    chunk_size = 32
    while produced < memory_size:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        h.update(block)
        produced += chunk_size
        counter += 1
    return h.digest()

def resolve_dns(domain: str, port: int = WS_DEFAULT_PORT) -> list:
    urls = []
    try:
        ips = socket.gethostbyname_ex(domain)[2]
        for ip in ips:
            urls.append(f"ws://{ip}:{port}{WS_PATH}")
    except:
        pass
    return urls

async def discover_nodes() -> list:
    discovered = []
    for seed in DNS_SEEDS:
        urls = resolve_dns(seed)
        if urls:
            discovered.extend(urls)
    if not discovered:
        discovered = [FALLBACK_NODE]
    random.shuffle(discovered)
    return discovered

class MobileMiner:
    def __init__(self, wallet=None, privkey=None, miner_id=None, node_url=None):
        self.wallet = wallet
        self.privkey = privkey
        self.miner_id = miner_id
        self.pubkey = None
        self.node_url = node_url

        if not self.wallet or not self.privkey:
            w, pk = load_wallet()
            if w and pk:
                self.wallet, self.privkey = w, pk
                logger.info(f"Loaded wallet: {self.wallet}")
            else:
                self.wallet, self.privkey, pub = generate_keypair()
                save_wallet(self.wallet, self.privkey)
                logger.info(f"Generated new wallet: {self.wallet}")
        self.pubkey = pubkey_hex_from_priv(self.privkey)

        if not self.miner_id:
            rand_token = secrets.token_hex(8)
            seed = f"{self.wallet}_{time.time()}_{rand_token}"
            digest = hashlib.sha256(seed.encode()).hexdigest()[:24].upper()
            self.miner_id = f"APEX_{digest}"

        self.ws = None
        self.running = True
        self.registered = False
        self.round_active = False
        self.block_id = -1
        self.deadline = 0
        self.max_path_length = 100
        self.step_interval = 1.0

        self.maze = None
        self.size = 0
        self.start = None
        self.current = None
        self.goal = None
        self.goal_velocity = (0,0,0)
        self.steps_taken = 0
        self.accepted = 0
        self.attempts = 0
        self.balance = 0
        self.local_walls = set()
        self.visited = {}
        self.visited_cells = set()
        self._pending_ack = None
        self._reconnect_attempts = 0
        self._heartbeat_task = None
        self._nav_task = None
        self.block_start_time = 0
        self.last_move_duration = 0.0
        self.knowledge_submit_interval = 5
        self.last_knowledge_submit = 0
        self._knowledge_confirmed = False
        self._knowledge_pending = False
        self._last_move_time = 0
        self._state_request_future = None
        self._knowledge_future = None

    def _print_block_start(self):
        if self.maze is None:
            return
        size = self.size
        start = self.start
        goal = self.goal
        max_path = self.max_path_length
        print(f"\n{GREEN}{BOLD}>>> BLOCK #{self.block_id} MINING (start: {start}){RESET}")
        print(f"{CYAN}Size: {size}x{size}x{size} | Target: {goal} | "
              f"Max Path: {max_path}{RESET}\n")

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
            self._reconnect_attempts = 0
            print(f"{GREEN}✓ Connected to {url}{RESET}")
            return True
        except Exception as e:
            print(f"{RED}✗ Connect failed: {e}{RESET}")
            return False

    async def disconnect(self):
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None

    async def request_balance(self):
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"type": "get_balance", "wallet": self.wallet}))
        except Exception as e:
            logger.debug(f"Balance request failed: {e}")

    async def request_state(self):
        if self.ws is None or not self.registered:
            return None
        try:
            self._state_request_future = asyncio.get_event_loop().create_future()
            await self.ws.send(json.dumps({
                "type": "get_maze_state",
                "miner_id": self.miner_id
            }))
            state = await asyncio.wait_for(self._state_request_future, timeout=5.0)
            return state
        except Exception as e:
            logger.debug(f"State request failed: {e}")
            return None

    async def submit_knowledge(self):
        if not self.ws or not self.registered:
            return False
        if self._knowledge_pending:
            return False
        now = time.time()
        if now - self.last_knowledge_submit < self.knowledge_submit_interval:
            return False

        walls = list(self.local_walls)
        prefs = {}
        for d in DIR_NAMES:
            prefs[d] = random.random()
        try:
            self._knowledge_pending = True
            await self.ws.send(json.dumps({
                "type": "knowledge_report",
                "miner_id": self.miner_id,
                "walls": walls,
                "preferences": prefs
            }))
            self._knowledge_future = asyncio.get_event_loop().create_future()
            ack = await asyncio.wait_for(self._knowledge_future, timeout=KNOWLEDGE_ACK_TIMEOUT)
            self._knowledge_pending = False
            if ack and ack.get("accepted", False):
                self._knowledge_confirmed = True
                self.last_knowledge_submit = now
                return True
            else:
                self._knowledge_confirmed = False
                return False
        except asyncio.TimeoutError:
            self._knowledge_pending = False
            self._knowledge_confirmed = False
            return False
        except Exception as e:
            self._knowledge_pending = False
            self._knowledge_confirmed = False
            logger.debug(f"Knowledge submit failed: {e}")
            return False

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
            "miner_type": "apex"
        }
        try:
            await self.ws.send(json.dumps(payload))
            resp = await asyncio.wait_for(self.ws.recv(), timeout=30)
            data = json.loads(resp)
        except Exception as e:
            logger.error(f"Registration send/recv error: {e}")
            print(f"{RED}Registration failed: {e}{RESET}")
            return False

        if data.get("type") == "registered":
            self.registered = True
            self.balance = data.get("confirmed_balance", 0)
            bal_mcx = self.balance // 1_000_000
            print(f"{GREEN}✅ REGISTERED! Balance: {bal_mcx:,} MCX{RESET}")
            asyncio.create_task(self.request_balance())
            return True

        if data.get("type") == "memory_challenge_request":
            seed = base64.b64decode(data["seed"])
            memory_size = data.get("memory_size", MEMORY_CHALLENGE_SIZE)
            print(f"{CYAN}Completing memory challenge ({memory_size//1024//1024}MB)...{RESET}")
            commitment = memory_challenge_commitment(seed, memory_size)
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
                print(f"{GREEN}✅ REGISTERED (already in round)!{RESET}")
                await self.handle_message(final_data)
                asyncio.create_task(self.request_balance())
                return True
            elif final_data.get("type") == "registered":
                self.registered = True
                self.balance = final_data.get("confirmed_balance", 0)
                bal_mcx = self.balance // 1_000_000
                print(f"{GREEN}✅ REGISTERED! Balance: {bal_mcx:,} MCX{RESET}")
                asyncio.create_task(self.request_balance())
                return True
            else:
                print(f"{RED}Registration failed after challenge.{RESET}")
                return False

        print(f"{RED}Unexpected registration response.{RESET}")
        return False

    def _find_path_step(self, start, goal, remaining):
        """
        Returns direction index of first step on a path that reaches goal within
        `remaining` steps, or None if no such path exists.
        """
        if start == goal:
            return None
        sx, sy, sz = start
        gx, gy, gz = goal
        size = self.size
        maze = self.maze

        q = deque([(sx, sy, sz, [])])
        visited = {start}
        while q:
            x, y, z, path = q.popleft()
            if len(path) >= remaining:
                continue
            for idx, (dx, dy, dz) in enumerate(DIR_OFFSETS):
                nx, ny, nz = x + dx, y + dy, z + dz
                if not (0 <= nx < size and 0 <= ny < size and 0 <= nz < size):
                    continue
                if maze[nz][ny][nx]:
                    continue
                if (nx, ny, nz) in visited:
                    continue
                if (nx, ny, nz) in self.local_walls:
                    continue
                new_path = path + [(nx, ny, nz)]
                if (nx, ny, nz) == (gx, gy, gz):
                    # Found path
                    first = new_path[0]
                    dx0, dy0, dz0 = first[0] - sx, first[1] - sy, first[2] - sz
                    for idx0, (ox, oy, oz) in enumerate(DIR_OFFSETS):
                        if (ox, oy, oz) == (dx0, dy0, dz0):
                            return idx0
                    return None
                visited.add((nx, ny, nz))
                q.append((nx, ny, nz, new_path))
        return None

    async def sync_state(self):
        state = await self.request_state()
        if state is None:
            return False
        s = state.get("state", {})
        if s.get("current"):
            self.current = tuple(s["current"])
        if s.get("goal"):
            self.goal = tuple(s["goal"])
        if "steps" in s:
            self.steps_taken = s["steps"]
        if "deadline" in s:
            self.deadline = s["deadline"]
        if "block_id" in s:
            self.block_id = s["block_id"]
        if "max_path_length" in s:
            self.max_path_length = s["max_path_length"]
        return True

    async def navigate(self):
        if self.maze is None or self.current is None or self.goal is None:
            return

        # Sync state at start to get latest goal and steps
        await self.sync_state()

        pos = self.current
        self.block_start_time = time.time()
        self.steps_taken = 0
        self.attempts = 0
        self.accepted = 0
        self._knowledge_confirmed = False
        self._last_move_time = 0

        self._print_block_start()

        # Initial knowledge submission
        print(f"{CYAN}Submitting initial knowledge...{RESET}")
        while not self._knowledge_confirmed and self.round_active:
            ok = await self.submit_knowledge()
            if ok:
                print(f"{GREEN}✓ Knowledge confirmed{RESET}")
                break
            else:
                print(f"{YELLOW}⏳ Waiting for knowledge confirmation...{RESET}")
                await asyncio.sleep(1)

        # Set initial timer to avoid "too fast"
        self._last_move_time = time.time()

        while self.round_active and pos != self.goal and time.time() < self.deadline:
            if self.steps_taken >= self.max_path_length:
                print(f"{YELLOW}⚠️ Reached max path length ({self.max_path_length}), stopping.{RESET}")
                break

            # Refresh knowledge if stale
            if time.time() - self.last_knowledge_submit > 30:
                print(f"{CYAN}Refreshing knowledge...{RESET}")
                ok = await self.submit_knowledge()
                if not ok:
                    print(f"{RED}Knowledge refresh failed, waiting...{RESET}")
                    await asyncio.sleep(1)
                    continue

            remaining = self.max_path_length - self.steps_taken
            dir_idx = self._find_path_step(pos, self.goal, remaining)

            if dir_idx is None:
                # No path within remaining steps – sync and wait (goal may move)
                print(f"{YELLOW}No reachable path within {remaining} steps, syncing state...{RESET}")
                await self.sync_state()
                await asyncio.sleep(1)
                continue

            direction = DIR_NAMES[dir_idx]
            step = self.steps_taken + 1
            prefix = f"{self.block_id}{self.miner_id}{direction}{step}"
            self.attempts += 1
            self._pending_ack = asyncio.get_event_loop().create_future()

            sig = sign_message(self.privkey, prefix)
            payload = {
                "type": "maze_move",
                "miner_id": self.miner_id,
                "direction": direction,
                "step": step,
                "signature": sig
            }
            move_start = time.time()

            # Wait at least 0.5 seconds (node enforces this)
            wait_time = 0.5 - (time.time() - self._last_move_time)
            if wait_time > 0:
                await asyncio.sleep(wait_time)

            try:
                await self.ws.send(json.dumps(payload))
                ack = await asyncio.wait_for(self._pending_ack, timeout=MOVE_ACK_TIMEOUT)
                self.last_move_duration = time.time() - move_start
                success = ack.get("success", False)

                # Always update last move time
                self._last_move_time = time.time()

                status_colour = GREEN if success else RED
                status_text = "ACCEPTED" if success else "REJECTED"
                msg = ack.get("message", "")
                if success:
                    print(f"{status_colour}[Step {step:03d}] {direction.upper():<5} -> {status_text}{RESET}")
                else:
                    print(f"{status_colour}[Step {step:03d}] {direction.upper():<5} -> {status_text}: {msg}{RESET}")

                if success:
                    self.steps_taken += 1
                    self.accepted += 1
                    self.visited[pos] = self.visited.get(pos, 0) + 1
                    self.visited_cells.add(pos)
                    dx, dy, dz = DIR_OFFSETS[dir_idx]
                    new_pos = (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                    self.current = new_pos
                    pos = new_pos
                    if ack.get("state", {}).get("goal"):
                        self.goal = tuple(ack["state"]["goal"])
                    # Submit knowledge periodically after successful moves
                    if self.steps_taken % 3 == 0:
                        await self.submit_knowledge()
                else:
                    # Handle specific rejections
                    if "wall" in str(msg).lower():
                        dx, dy, dz = DIR_OFFSETS[dir_idx]
                        self.local_walls.add((pos[0] + dx, pos[1] + dy, pos[2] + dz))
                    elif "path too long" in str(msg).lower() or "step mismatch" in str(msg).lower():
                        await self.sync_state()
                    elif "too fast" in str(msg).lower():
                        # Already updated timer, so fine
                        pass
                    elif "knowledge" in str(msg).lower():
                        await self.submit_knowledge()

            except asyncio.TimeoutError:
                print(f"{RED}[Step {step:03d}] TIMEOUT - syncing state...{RESET}")
                logger.warning("Move ack timeout")
                self._last_move_time = time.time()
                await self.sync_state()
            except Exception as e:
                print(f"{RED}[Step {step:03d}] ERROR: {e}{RESET}")
                logger.error(f"Move error: {e}")
                self._last_move_time = time.time()

            await asyncio.sleep(0.01)  # small yield

        if pos == self.goal:
            print(f"\n{GREEN}🏁 Goal reached!{RESET}")
        else:
            print(f"\n{YELLOW}⏳ Round finished (time, max steps, or no path).{RESET}")
        self.round_active = False

    async def handle_message(self, data):
        if not data or not isinstance(data, dict):
            return
        msg_type = data.get("type")

        if msg_type == "maze_init":
            logger.info(f"Block #{data['block_id']} started")
            self.block_id = data["block_id"]
            self.size = data["size"]
            self.maze = data["maze"]
            self.start = tuple(data["start"])
            self.current = self.start
            self.goal = tuple(data["goal"])
            self.goal_velocity = tuple(data.get("goal_velocity", (0,0,0)))
            self.deadline = data["deadline"]
            self.max_path_length = data.get("max_path_length", 100)
            self.step_interval = data.get("step_interval", 0.5)  # node enforces 0.5s min
            self.round_active = True
            self.steps_taken = 0
            self.accepted = 0
            self.attempts = 0
            self.local_walls.clear()
            self.visited.clear()
            self.visited_cells.clear()
            self.last_knowledge_submit = 0
            self._knowledge_confirmed = False

            if self.deadline <= time.time() + 2:
                print(f"{YELLOW}⚠️ Block #{self.block_id} already expired. Waiting...{RESET}")
                self.round_active = False
                return

            if self._nav_task and not self._nav_task.done():
                self._nav_task.cancel()
            self._nav_task = asyncio.create_task(self.navigate())

        elif msg_type == "maze_state":
            if self._state_request_future and not self._state_request_future.done():
                self._state_request_future.set_result(data)
            state = data.get("state", {})
            if state.get("current"):
                self.current = tuple(state["current"])
            if state.get("goal"):
                self.goal = tuple(state["goal"])
            if state.get("deadline"):
                self.deadline = state["deadline"]
            if state.get("block_id"):
                self.block_id = state["block_id"]
            if "steps" in state:
                self.steps_taken = state["steps"]
            if "max_path_length" in state:
                self.max_path_length = state["max_path_length"]
            if not self.round_active and self.maze is not None:
                self.round_active = True
                logger.info(f"Resumed round {self.block_id}")
                if self._nav_task and not self._nav_task.done():
                    self._nav_task.cancel()
                self._nav_task = asyncio.create_task(self.navigate())

        elif msg_type == "maze_move_ack":
            if self._pending_ack and not self._pending_ack.done():
                self._pending_ack.set_result(data)

        elif msg_type == "knowledge_ack":
            if self._knowledge_future and not self._knowledge_future.done():
                self._knowledge_future.set_result(data)
            if data.get("accepted", False):
                self._knowledge_confirmed = True
                self.last_knowledge_submit = time.time()
            else:
                self._knowledge_confirmed = False

        elif msg_type == "balance":
            self.balance = data.get("balance", 0)
            bal_mcx = self.balance // 1_000_000
            print(f"\n{CYAN}💳 Balance updated: {bal_mcx:,} MCX{RESET}")

        elif msg_type == "block_accepted":
            reward = data.get("reward", 0)
            self.balance += reward
            bal_mcx = self.balance // 1_000_000
            print(f"\n{GREEN}{BOLD}🎉 BLOCK REWARD: +{reward//1_000_000:,} MCX | New: {bal_mcx:,} MCX{RESET}")

        elif msg_type == "error":
            print(f"\n{RED}Node error: {data.get('message')}{RESET}")

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
        print(f"{CYAN}Wallet: {self.wallet}{RESET}")
        print(f"{CYAN}Miner ID: {self.miner_id}{RESET}\n")

        self._heartbeat_task = asyncio.create_task(self.heartbeat())

        while self.running:
            if not self.node_url:
                nodes = await discover_nodes()
                if not nodes:
                    print(f"{YELLOW}No nodes discovered, retrying in 10s...{RESET}")
                    await asyncio.sleep(10)
                    continue
                self.node_url = nodes[0]
                print(f"{CYAN}Using node: {self.node_url}{RESET}")

            if not await self.connect(self.node_url):
                self._reconnect_attempts += 1
                if self._reconnect_attempts > MAX_RECONNECT_ATTEMPTS:
                    print(f"{YELLOW}Giving up on {self.node_url}, trying next...{RESET}")
                    self.node_url = None
                    self._reconnect_attempts = 0
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
                            logger.warning("Invalid JSON received")
                        except Exception as e:
                            logger.error(f"Message handling error: {e}")
                except websockets.ConnectionClosed:
                    print(f"{YELLOW}Connection closed, reconnecting...{RESET}")
                except Exception as e:
                    logger.error(f"Message loop error: {e}")
                finally:
                    await self.disconnect()
                    self.registered = False
            else:
                print(f"{YELLOW}Registration failed, retrying in 10s...{RESET}")
                await asyncio.sleep(10)

            await asyncio.sleep(1)

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        print(f"{CYAN}Miner stopped.{RESET}")

    async def stop(self):
        self.running = False
        if self._nav_task:
            self._nav_task.cancel()
        await self.disconnect()

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", help="WebSocket URL (overrides discovery)")
    parser.add_argument("--wallet", help="Wallet address")
    parser.add_argument("--privkey", help="Private key hex")
    parser.add_argument("--miner-id", help="Custom miner ID (overrides auto)")
    args = parser.parse_args()

    if not args.wallet or not args.privkey:
        w, pk = load_wallet()
        if w and pk:
            args.wallet, args.privkey = w, pk
            print(f"{CYAN}Loaded saved wallet: {args.wallet}{RESET}")
            print(f"{YELLOW}Private key: {args.privkey}{RESET}")
            print(f"{RED}⚠️  Keep this private key safe!{RESET}")
        else:
            print("\n" + "="*50)
            print("  MCX Wallet Setup (press ENTER to generate new)")
            print("="*50)
            wallet_input = input("Wallet address (0x...): ").strip()
            privkey_input = input("Private key hex: ").strip()
            if wallet_input and privkey_input:
                args.wallet = wallet_input
                args.privkey = privkey_input
                save_wallet(args.wallet, args.privkey)
                print(f"{GREEN}Wallet saved.{RESET}")
            else:
                print(f"{CYAN}Generating new wallet...{RESET}")
                w, pk, _ = generate_keypair()
                args.wallet = w
                args.privkey = pk
                save_wallet(args.wallet, args.privkey)
                print(f"{GREEN}Generated new wallet:{RESET}")
                print(f"{YELLOW}Address: {args.wallet}{RESET}")
                print(f"{YELLOW}Private key: {args.privkey}{RESET}")
                print(f"{RED}⚠️  SAVE YOUR PRIVATE KEY – you will need it to access your funds!{RESET}")

    miner = MobileMiner(
        wallet=args.wallet,
        privkey=args.privkey,
        miner_id=args.miner_id,
        node_url=args.node
    )

    try:
        await miner.run()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Shutdown by user.{RESET}")
    finally:
        await miner.stop()

if __name__ == "__main__":
    asyncio.run(main())
