#!/usr/bin/env python3
"""
runs on iPhone, Android, etc.
PoUP-only – no hashstep.
"""

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("APEX")

RESET = "\033[0m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"

BANNER = fr"""
{CYAN}{BOLD}
  █████╗ ██████╗ ███████╗██╗  ██╗    ███╗   ███╗██╗███╗   ██╗███████╗██████╗ 
 ██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝    ████╗ ████║██║████╗  ██║██╔════╝██╔══██╗
 ███████║██████╔╝█████╗   ╚███╔╝     ██╔████╔██║██║██╔██╗ ██║█████╗  ██████╔╝
 ██╔══██║██╔═══╝ ██╔══╝   ██╔██╗     ██║╚██╔╝██║██║██║╚██╗██║██╔══╝  ██╔══██╗
 ██║  ██║██║     ███████╗██╔╝ ██╗    ██║ ╚═╝ ██║██║██║ ╚████║███████╗██║  ██║
 ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝    ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝
{RESET}{CYAN}{BOLD}  ─── APEX MOBILE MINER – LIGHT & FAST (PoUP) ───{RESET}
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

# -----------------------------------------------------------------------------
# DNS discovery
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Main Mobile Miner Class (PoUP only)
# -----------------------------------------------------------------------------
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

        # Unique miner ID per run
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
        self._pending_ack = None
        self._reconnect_attempts = 0
        self._heartbeat_task = None
        self._nav_task = None

        # For telemetry
        self.block_start_time = 0
        self.last_move_duration = 0.0

        # PoUP void state
        self.current_entropy = ""

    # -------------------------------------------------------------------------
    # State reset
    # -------------------------------------------------------------------------
    def reset_round_state(self):
        """Clear all maze/round state when we're not in a round."""
        self.round_active = False
        self.maze = None
        self.size = 0
        self.start = None
        self.current = None
        self.goal = None
        self.goal_velocity = (0,0,0)
        self.steps_taken = 0
        self.accepted = 0
        self.attempts = 0
        self.local_walls.clear()
        self.visited.clear()
        self.block_id = -1
        self.deadline = 0
        if self._nav_task and not self._nav_task.done():
            self._nav_task.cancel()
        self._nav_task = None

    # -------------------------------------------------------------------------
    # Pretty printing helpers
    # -------------------------------------------------------------------------
    def _print_block_start(self):
        if self.maze is None:
            return
        size = self.size
        start = self.start
        goal = self.goal
        print(f"\n{GREEN}{BOLD}>>> BLOCK #{self.block_id} MINING (start: {start}, current: {self.current}){RESET}")
        print(f"{CYAN}Size: {size}x{size}x{size} | Target: {goal}{RESET}\n")

    def _print_telemetry(self, direction):
        sr = (self.accepted / self.attempts) if self.attempts > 0 else 1.0
        time_left = max(0.0, self.deadline - time.time())
        bal_mcx = self.balance // 1_000_000
        pace_ms = self.last_move_duration * 1000
        line = (f"{BOLD}[Step {self.steps_taken:03d}/{self.size**3*2:03d}]{RESET} "
                f"Pos: ({self.current[0]:2d},{self.current[1]:2d},{self.current[2]:2d}) | "
                f"Dir: {direction.upper():<5} | SR: {sr*100:3.0f}% | "
                f"Pace: {pace_ms:4.1f}ms | Time: {time_left:4.1f}s | "
                f"{MAGENTA}{BOLD}Bal: {bal_mcx:,} MCX{RESET}")
        print(line, flush=True)

    # -------------------------------------------------------------------------
    # WebSocket connection
    # -------------------------------------------------------------------------
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
        # On disconnect, reset round state so we don't hold stale data
        self.reset_round_state()

    # -------------------------------------------------------------------------
    # Balance request
    # -------------------------------------------------------------------------
    async def request_balance(self):
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"type": "get_balance", "wallet": self.wallet}))
        except Exception as e:
            logger.debug(f"Balance request failed: {e}")

    # -------------------------------------------------------------------------
    # Request current maze state (after registration)
    # -------------------------------------------------------------------------
    async def request_maze_state(self):
        if self.ws is None or not self.registered:
            return
        try:
            await self.ws.send(json.dumps({"type": "get_maze_state", "miner_id": self.miner_id}))
            logger.debug("[MAZE] Requested current maze state")
        except Exception as e:
            logger.debug(f"Maze state request failed: {e}")

    # -------------------------------------------------------------------------
    # Registration (with memory challenge)
    # -------------------------------------------------------------------------
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
            # After registration, ask for maze state if a round is active
            asyncio.create_task(self.request_maze_state())
            return True

        if data.get("type") == "memory_challenge_request":
            seed = base64.b64decode(data["seed"])
            memory_size = data.get("memory_size", 8 * 1024 * 1024)
            print(f"{CYAN}Completing memory challenge ({memory_size//1024//1024}MB)...{RESET}")
            h = hashlib.sha256()
            counter = 0
            while h.digest_size * counter < memory_size:
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
                asyncio.create_task(self.request_maze_state())
                return True
            else:
                print(f"{RED}Registration failed after challenge.{RESET}")
                return False

        print(f"{RED}Unexpected registration response.{RESET}")
        return False

    # -------------------------------------------------------------------------
    # A* Pathfinding
    # -------------------------------------------------------------------------
    def _a_star_step(self, start, goal):
        size = self.size
        maze = self.maze
        sx, sy, sz = start
        gx, gy, gz = goal
        if start == goal:
            return -1

        q = deque([(sx, sy, sz, [])])
        visited = {start}
        while q:
            x, y, z, path = q.popleft()
            if (x, y, z) == (gx, gy, gz):
                if path:
                    first = path[0]
                    dx, dy, dz = first[0] - sx, first[1] - sy, first[2] - sz
                    for idx, (ox, oy, oz) in enumerate(DIR_OFFSETS):
                        if (ox, oy, oz) == (dx, dy, dz):
                            return idx
                return -1
            for idx, (dx, dy, dz) in enumerate(DIR_OFFSETS):
                nx, ny, nz = x + dx, y + dy, z + dz
                if 0 <= nx < size and 0 <= ny < size and 0 <= nz < size:
                    if maze[nz][ny][nx] == 0 and (nx, ny, nz) not in visited:
                        if (nx, ny, nz) in self.local_walls:
                            continue
                        visited.add((nx, ny, nz))
                        q.append((nx, ny, nz, path + [(nx, ny, nz)]))
        return -1

    # -------------------------------------------------------------------------
    # Navigation loop (PoUP only – no nonce)
    # -------------------------------------------------------------------------
    async def navigate(self):
        if self.maze is None or self.current is None or self.goal is None:
            return

        pos = self.current
        self.block_start_time = time.time()
        self.steps_taken = 0
        self.attempts = 0
        self.accepted = 0

        self._print_block_start()

        while self.round_active and pos != self.goal and time.time() < self.deadline:
            dir_idx = self._a_star_step(pos, self.goal)
            if dir_idx < 0:
                await asyncio.sleep(0.5)
                continue

            direction = DIR_NAMES[dir_idx]
            step = self.steps_taken + 1
            prefix = f"{self.block_id}{self.miner_id}{direction}{step}"
            sig = sign_message(self.privkey, prefix)

            self.attempts += 1
            self._pending_ack = asyncio.get_event_loop().create_future()

            payload = {
                "type": "maze_move",
                "miner_id": self.miner_id,
                "direction": direction,
                "step": step,
                "signature": sig
            }
            move_start = time.time()
            try:
                await self.ws.send(json.dumps(payload))
                ack = await asyncio.wait_for(self._pending_ack, timeout=MOVE_ACK_TIMEOUT)
                self.last_move_duration = time.time() - move_start
                if ack.get("success"):
                    self.steps_taken += 1
                    self.accepted += 1
                    self.visited[pos] = self.visited.get(pos, 0) + 1
                    dx, dy, dz = DIR_OFFSETS[dir_idx]
                    new_pos = (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                    self.current = new_pos
                    pos = new_pos
                    if ack.get("state", {}).get("goal"):
                        self.goal = tuple(ack["state"]["goal"])
                    self._print_telemetry(direction)
                else:
                    # Handle rejection
                    err = ack.get("message", "")
                    if "no active" in err.lower() or "round" in err.lower():
                        # Node says no round – clear state
                        logger.warning("Round ended (node says no active round)")
                        self.round_active = False
                        break
                    elif "wall" in err.lower():
                        dx, dy, dz = DIR_OFFSETS[dir_idx]
                        self.local_walls.add((pos[0] + dx, pos[1] + dy, pos[2] + dz))
                    else:
                        # Other error – treat as transient, but maybe clear state if repeated
                        logger.warning(f"Move rejected: {err}")
            except asyncio.TimeoutError:
                logger.warning("Move ack timeout")
            except Exception as e:
                logger.error(f"Move error: {e}")

            await asyncio.sleep(0.01)

        if pos == self.goal:
            print(f"\n{GREEN}🏁 Goal reached!{RESET}")
        else:
            print(f"\n{YELLOW}⏳ Round finished (time or steps).{RESET}")
        self.round_active = False

    # -------------------------------------------------------------------------
    # Message handling – includes poup_step_tick
    # -------------------------------------------------------------------------
    async def handle_message(self, data):
        if not data or not isinstance(data, dict):
            return
        msg_type = data.get("type")

        if msg_type == "maze_init":
            # If we are already in this round, just update state
            if self.round_active and data.get("block_id") == self.block_id:
                if "goal" in data:
                    self.goal = tuple(data["goal"])
                if "deadline" in data:
                    self.deadline = data["deadline"]
                if "goal_velocity" in data:
                    self.goal_velocity = tuple(data["goal_velocity"])
                if "maze" in data:
                    self.maze = data["maze"]
                return

            # New block – reset fully
            self.reset_round_state()
            logger.info(f"Block #{data['block_id']} started")
            self.block_id = data["block_id"]
            self.size = data["size"]
            self.maze = data["maze"]
            self.start = tuple(data["start"])
            self.current = self.start
            self.goal = tuple(data["goal"])
            self.goal_velocity = tuple(data.get("goal_velocity", (0,0,0)))
            self.deadline = data["deadline"]
            self.round_active = True
            self.steps_taken = 0
            self.accepted = 0
            self.attempts = 0
            self.local_walls.clear()
            self.visited.clear()

            if self.deadline <= time.time() + 2:
                print(f"{YELLOW}⚠️ Block #{self.block_id} already expired. Waiting for next...{RESET}")
                self.round_active = False
                return

            if self._nav_task and not self._nav_task.done():
                self._nav_task.cancel()
            self._nav_task = asyncio.create_task(self.navigate())

        elif msg_type == "maze_state":
            state = data.get("state", {})
            if not state:
                # No active round for this miner
                self.reset_round_state()
                logger.info("[MAZE] No active round state – waiting for next maze_init")
                return
            if state.get("current"):
                self.current = tuple(state["current"])
            if state.get("goal"):
                self.goal = tuple(state["goal"])
            if state.get("deadline"):
                self.deadline = state["deadline"]
            if state.get("block_id"):
                self.block_id = state["block_id"]
            if not self.round_active and self.maze is not None and self.current is not None:
                self.round_active = True
                logger.info(f"Resumed round {self.block_id}")
                if self._nav_task and not self._nav_task.done():
                    self._nav_task.cancel()
                self._nav_task = asyncio.create_task(self.navigate())
            elif not state:
                self.reset_round_state()

        elif msg_type == "maze_move_ack":
            if self._pending_ack and not self._pending_ack.done():
                self._pending_ack.set_result(data)
            if data.get("success"):
                self.accepted += 1
                if data.get("state", {}).get("current"):
                    self.current = tuple(data["state"]["current"])
                if data.get("state", {}).get("goal"):
                    self.goal = tuple(data["state"]["goal"])
                if data.get("state", {}).get("finished"):
                    self.round_active = False
                    print(f"\n{GREEN}🏁 Goal confirmed by node!{RESET}")
            else:
                # Check if node says no active round
                err = data.get("message", "")
                if "no active" in err.lower() or "round" in err.lower():
                    self.reset_round_state()
                    logger.warning("Round ended – resetting state")
                elif "wall" in err.lower():
                    pass
                else:
                    logger.debug(f"Move rejected: {err}")

        # --- PoUP step tick handling ---
        elif msg_type == "poup_step_tick":
            entropy = data.get("entropy", "")
            self.current_entropy = entropy
            delta = [random.randint(-1, 1) for _ in range(3)]
            timestamp = int(time.time())
            message = f"{self.miner_id}{delta[0]},{delta[1]},{delta[2]}{entropy}{timestamp}"
            sig = sign_message(self.privkey, message)
            await self.ws.send(json.dumps({
                "type": "poup_step",
                "miner_id": self.miner_id,
                "delta": delta,
                "signature": sig,
                "timestamp": timestamp
            }))
            logger.debug(f"[POUP] Sent step {delta} with entropy {entropy[:8]}...")

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
            err = data.get("message", "")
            if "no active" in err.lower() or "round" in err.lower():
                self.reset_round_state()
                logger.warning("Node error: no active block round – resetting state")
            else:
                print(f"\n{RED}Node error: {err}{RESET}")

    # -------------------------------------------------------------------------
    # Heartbeat
    # -------------------------------------------------------------------------
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

    # -------------------------------------------------------------------------
    # Main run loop
    # -------------------------------------------------------------------------
    async def run(self):
        print(BANNER)
        logger.info(f"Wallet: {self.wallet}")
        logger.info(f"Miner ID: {self.miner_id}")
        print(f"{WHITE}Wallet: {self.wallet}{RESET}")
        print(f"{WHITE}Miner ID: {self.miner_id}{RESET}\n")

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
        self.reset_round_state()
        await self.disconnect()

# -----------------------------------------------------------------------------
# Entry point – with wallet input prompt (shows private key)
# -----------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", help="WebSocket URL (overrides discovery)")
    parser.add_argument("--wallet", help="Wallet address")
    parser.add_argument("--privkey", help="Private key hex")
    parser.add_argument("--miner-id", help="Custom miner ID (overrides auto)")
    args = parser.parse_args()

    # If wallet/privkey not provided via args, try loading from file
    if not args.wallet or not args.privkey:
        w, pk = load_wallet()
        if w and pk:
            args.wallet, args.privkey = w, pk
            print(f"{CYAN}Loaded saved wallet: {args.wallet}{RESET}")
            print(f"{YELLOW}Private key: {args.privkey}{RESET}")
            print(f"{RED}⚠️  Keep this private key safe!{RESET}")
        else:
            # Prompt user for wallet and private key (works on mobile)
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
                # Generate new wallet and show private key
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
