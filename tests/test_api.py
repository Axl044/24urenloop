"""End-to-end tests: start de server in een subprocess met een tijdelijke database."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.opener.open(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        port = free_port()
        env = dict(os.environ, DB_PATH=os.path.join(self.tmp.name, "t.db"), PORT=str(port), HOST="127.0.0.1",
                   TELLER_PIN="1111", ADMIN_PIN="2222", MIN_LAP_SECONDS="1", UNDO_SECONDS="60", BACKUP_MINUTES="0",
                   PUBLIC_DIR=os.path.join(self.tmp.name, "public"), PUBLIC_SECONDS="1")
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/healthz")
                break
            except OSError:
                time.sleep(0.1)
        self.admin = Client(self.base)
        self.teller = Client(self.base)
        self.assertEqual(self.admin.call("/api/login", {"pin": "2222"})[0], 200)
        self.assertEqual(self.teller.call("/api/login", {"pin": "1111"})[0], 200)

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait()
        self.proc.stderr.close()
        self.tmp.cleanup()

    def setup_runners(self, names):
        self.admin.call("/api/admin/gangs/add", {"name": "Gang A"})
        for n in names:
            st, _ = self.admin.call("/api/admin/queue/add", {"name": n, "gang": "Gang A"})
            self.assertEqual(st, 200)

    def test_auth(self):
        anon = Client(self.base)
        self.assertEqual(anon.call("/api/state")[0], 401)
        self.assertEqual(self.teller.call("/api/admin")[0], 403)
        self.assertEqual(self.teller.call("/api/admin/start", {})[0], 403)
        self.assertEqual(anon.call("/api/login", {"pin": "nope"})[0], 401)

    def test_full_flow(self):
        self.setup_runners(["Ann", "Bob", "Cas", "Dirk"])
        st, s = self.admin.call("/api/admin/start", {})
        self.assertEqual(st, 200)
        self.assertEqual(s["current"]["name"], "Ann")
        self.assertEqual([r["name"] for r in s["next"]], ["Bob", "Cas"])

        # Te snel na de start -> geweigerd
        st, _ = self.teller.call("/api/pass", {"id": "p0"})
        self.assertEqual(st, 409)

        time.sleep(1.1)
        st, r = self.teller.call("/api/pass", {"id": "p1"})
        self.assertEqual(st, 200)
        self.assertEqual(r["state"]["current"]["name"], "Bob")
        self.assertEqual(r["state"]["previous"]["name"], "Ann")
        # Retry met hetzelfde id telt niet dubbel
        st, r = self.teller.call("/api/pass", {"id": "p1"})
        self.assertTrue(r["duplicate"])
        self.assertEqual(r["state"]["current"]["name"], "Bob")

        # Undo zet Bob terug vooraan en heropent Ann
        st, r = self.teller.call("/api/undo", {"lap_id": r["state"]["current"]["id"]})
        self.assertEqual(st, 200)
        self.assertEqual(r["state"]["current"]["name"], "Ann")
        self.assertIsNone(r["state"]["current"]["end_ms"])
        self.assertEqual([x["name"] for x in r["state"]["next"]], ["Bob", "Cas"])
        # Dubbele undo met hetzelfde lap_id wordt geweigerd
        self.assertEqual(self.teller.call("/api/undo", {"lap_id": 999})[0], 409)

        # Wissel met tijdstip van de klik (bv. vertraagd door netwerk)
        _, s = self.teller.call("/api/state")
        click = s["current"]["start_ms"] + 1500
        time.sleep(0.6)
        st, r = self.teller.call("/api/pass", {"id": "p2", "ts": click})
        self.assertEqual(st, 200)
        self.assertEqual(r["state"]["current"]["start_ms"], click)
        self.assertEqual(r["state"]["previous"]["end_ms"], click)

        # Volgorde aanpassen: Dirk voor Cas
        _, a = self.admin.call("/api/admin")
        ids = {q["name"]: q["id"] for q in a["queue"]}
        st, a = self.admin.call("/api/admin/queue/move", {"id": ids["Dirk"], "before": ids["Cas"]})
        self.assertEqual([q["name"] for q in a["queue"]], ["Dirk", "Cas"])
        st, a = self.admin.call("/api/admin/queue/move", {"id": ids["Dirk"], "before": None})
        self.assertEqual([q["name"] for q in a["queue"]], ["Cas", "Dirk"])

        # Lege wachtlijst: wissel gaat niet verloren
        time.sleep(1.1); self.teller.call("/api/pass", {"id": "p3"})
        time.sleep(1.1); self.teller.call("/api/pass", {"id": "p4"})
        time.sleep(1.1)
        st, r = self.teller.call("/api/pass", {"id": "p5"})
        self.assertEqual(st, 200)
        self.assertEqual(r["state"]["current"]["name"], "?")
        lap_id = r["state"]["current"]["id"]
        st, a = self.admin.call("/api/admin/laps/update", {"id": lap_id, "name": "Eva", "gang": "Gang A"})
        self.assertEqual(a["current"]["name"], "Eva")

        st, a = self.admin.call("/api/admin/stop", {})
        self.assertEqual(a["state"], "finished")
        self.assertIsNone(a["current"])
        _, stats = self.admin.call("/api/stats")
        self.assertEqual(stats["total"]["n"], 5)
        self.assertEqual(stats["gangs"][0]["n"], 5)
        st, csv = self.admin.call("/export/laps.csv")
        self.assertEqual(st, 200)
        self.assertEqual(len(csv.strip().splitlines()), 6)

        # Laps zijn aaneengesloten: einde van ronde n == start van ronde n+1
        laps = sorted(a["laps"], key=lambda l: l["seq"])
        self.assertEqual([l["seq"] for l in laps], [1, 2, 3, 4, 5])
        for x, y in zip(laps, laps[1:]):
            self.assertEqual(x["end_ms"], y["start_ms"])

    def test_concurrent_passes_counted_once(self):
        import threading
        self.setup_runners(["A", "B", "C"])
        self.admin.call("/api/admin/start", {})
        time.sleep(1.1)
        results = []
        ts = [threading.Thread(target=lambda: results.append(self.teller.call("/api/pass", {"id": "same"})[0]))
              for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        _, s = self.teller.call("/api/state")
        self.assertEqual(s["current"]["seq"], 2)
        self.assertEqual(s["laps_done"], 1)

    def test_public_scoreboard(self):
        anon = Client(self.base)
        self.setup_runners(["Ann", "Bob", "Cas"])
        self.admin.call("/api/admin/start", {})
        time.sleep(1.1)
        self.teller.call("/api/pass", {"id": "p1"})

        st, page = anon.call("/scorebord")
        self.assertEqual(st, 200)
        self.assertIn("scorebord.json", page)
        st, sb = anon.call("/scorebord.json")
        self.assertEqual(st, 200)
        self.assertEqual(sb["state"], "running")
        self.assertEqual(sb["current"]["name"], "Bob")
        self.assertEqual([r["name"] for r in sb["next"]], ["Cas"])
        self.assertEqual(sb["recent"][0]["name"], "Ann")
        self.assertEqual(sb["stats"]["total"]["n"], 1)
        self.assertNotIn("id", sb["current"])
        self.assertNotIn("queue", sb)
        # Anoniem blijft alles behalve het scorebord dicht
        self.assertEqual(anon.call("/api/stats")[0], 401)
        self.assertEqual(anon.call("/api/pass", {"id": "x"})[0], 401)

        # Export voor de publieke site
        pub = os.path.join(self.tmp.name, "public")
        for _ in range(30):
            try:
                with open(os.path.join(pub, "scorebord.json")) as f:
                    data = json.load(f)
                if data["laps_done"] == 1:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.assertEqual(data["current"]["name"], "Bob")
        with open(os.path.join(pub, "index.html")) as f:
            self.assertIn("scorebord.json", f.read())
        self.assertEqual(sorted(os.listdir(pub)), ["index.html", "scorebord.json"])


if __name__ == "__main__":
    unittest.main()
