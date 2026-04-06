#!/usr/bin/env python3
# =============================================================================
# relay_server.py — Serveur relay HTTP pour Render.com (gratuit a vie)
# =============================================================================
#
# Render endort le serveur apres 15 min d'inactivite sur le tier gratuit.
# Solution integree : un thread interne ping le serveur toutes les 10 min.
# Aucune action requise de votre part.
#
# Bibliotheque standard Python uniquement — aucun pip requis.
# =============================================================================

import json
import threading
import random
import time
import os
import queue
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT     = int(os.environ.get("PORT", 10000))
ROOM_TTL = 300
GAME_TTL = 7200
POLL_MAX = 4    # Render coupe les connexions longues — on garde court et on re-poll vite


class Room:
    def __init__(self, code):
        self.code      = code
        self.created   = time.time()
        self.started   = False
        self.n_players = 0
        self.lock      = threading.Lock()
        self.queues    = [queue.Queue(), queue.Queue()]
        self.last_seen = [time.time(), time.time()]

    def add_player(self):
        with self.lock:
            if self.n_players >= 2:
                return None
            self.n_players += 1
            return self.n_players

    def is_full(self):
        return self.n_players >= 2

    def is_expired(self):
        if self.started:
            return (time.time() - max(self.last_seen)) > GAME_TTL
        return (time.time() - self.created) > ROOM_TTL

    def push(self, to_player_id, msg):
        self.queues[to_player_id - 1].put(msg)

    def poll(self, player_id, timeout=POLL_MAX):
        self.last_seen[player_id - 1] = time.time()
        try:
            return self.queues[player_id - 1].get(timeout=timeout)
        except queue.Empty:
            return None


class RoomManager:
    def __init__(self):
        self._rooms = {}
        self._lock  = threading.Lock()
        threading.Thread(target=self._cleaner, daemon=True).start()

    def create(self):
        with self._lock:
            for _ in range(200):
                code = f"{random.randint(0, 99):02d}"
                if code not in self._rooms:
                    room = Room(code)
                    self._rooms[code] = room
                    return room
        raise RuntimeError("Impossible de generer un code unique")

    def get(self, code):
        with self._lock:
            return self._rooms.get(code.upper().strip())

    def delete(self, code):
        with self._lock:
            self._rooms.pop(code, None)

    def _cleaner(self):
        while True:
            time.sleep(30)
            with self._lock:
                expired = [c for c, r in self._rooms.items() if r.is_expired()]
            for code in expired:
                self.delete(code)
                print(f"[Relay] Room {code} expiree.")


rooms = RoomManager()


class RelayHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        if args and str(args[1]) not in ("200", "204"):
            print(f"[HTTP] {self.path} {args[1]}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path  = self.path.split("?")[0]
        query = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    query[k] = v

        if path == "/status":
            self._send_json({"status": "ok", "rooms": len(rooms._rooms)})

        elif path == "/poll":
            code = query.get("code", "").upper().strip()
            try:
                pid = int(query.get("pid", 0))
            except ValueError:
                pid = 0
            if not code or pid not in (1, 2):
                self._send_error_json("Parametres manquants")
                return
            room = rooms.get(code)
            if room is None:
                self._send_json({"msg": {"type": "error", "message": "Room introuvable."}})
                return
            msg = room.poll(pid, timeout=POLL_MAX)
            self._send_json({"msg": msg})

        else:
            self._send_error_json("Route inconnue", 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_body()

        if path == "/create":
            room = rooms.create()
            pid  = room.add_player()
            self._send_json({"type": "room_created", "code": room.code, "player_id": pid})
            print(f"[Relay] Room {room.code} creee.")

        elif path == "/join":
            code = body.get("code", "").upper().strip()
            room = rooms.get(code)
            if room is None:
                self._send_error_json(f"Code '{code}' introuvable ou expire.")
                return
            if room.is_full():
                self._send_error_json("Cette room est deja pleine.")
                return
            pid = room.add_player()
            if pid is None:
                self._send_error_json("Room pleine.")
                return
            room.push(1, {"type": "relay_ready", "player_id": 1})
            room.started = True
            self._send_json({"type": "room_joined", "code": room.code, "player_id": pid})
            print(f"[Relay] Room {room.code} — J2 a rejoint.")

        elif path == "/send":
            code = body.get("code", "").upper().strip()
            try:
                pid = int(body.get("player_id", 0))
            except (ValueError, TypeError):
                pid = 0
            msg = body.get("msg")
            if not code or pid not in (1, 2) or msg is None:
                self._send_error_json("Parametres manquants")
                return
            room = rooms.get(code)
            if room is None:
                self._send_error_json("Room introuvable.")
                return
            other_pid = 2 if pid == 1 else 1
            room.push(other_pid, msg)
            self._send_json({"ok": True})

        else:
            self._send_error_json("Route inconnue", 404)


def _keepalive_loop(port):
    """Ping toutes les 10 min pour empecher Render d'endormir le serveur."""
    import urllib.request
    time.sleep(90)
    while True:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/status", timeout=5)
            print("[Keepalive] Ping ok.")
        except Exception as e:
            print(f"[Keepalive] Erreur : {e}")
        time.sleep(600)


def main():
    from socketserver import ThreadingMixIn

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        """Chaque requete est traitee dans son propre thread — indispensable
        pour que les long-polls de J1 ne bloquent pas le /join de J2."""
        daemon_threads = True

    port = int(os.environ.get("PORT", PORT))
    server = ThreadedHTTPServer(("0.0.0.0", port), RelayHandler)
    server.timeout = POLL_MAX + 3

    threading.Thread(target=_keepalive_loop, args=(port,), daemon=True).start()

    print("=" * 50)
    print(f"  Relay HTTP demarre sur le port {port}")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Relay] Arret.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
