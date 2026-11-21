"""Setter opp og kobler sammen alle appene automatisk via API-ene deres.

Kjøres av «docker compose up -d» (tjenesten autoconfig). Trygt å kjøre igjen
når som helst – den sjekker hva som allerede er gjort og retter bare avvik:
    docker compose run --rm autoconfig

To måter å kjøre på:
  • Alene: egen VPN, qBittorrent, NZBGet og Prowlarr i denne stacken.
  • Delt (SHARED=1): bruker nedlasterne i Plex-stack. Der røres bare det som trengs
    for lydbøker: en kategori i qBittorrent og NZBGet, og Bookshelf i Prowlarr.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(__file__))
from common import (CFG, Session, _has_content, book_dirs, docker_action,  # noqa: E402
                    docker_exec, docker_logs, env, http, ini_set, log, ok, read, step,
                    wait_file, wait_http, warn, write)

PUID, PGID = int(env("PUID", "1000")), int(env("PGID", "1000"))
USER, PASSWORD = env("ADMIN_USER", "admin"), env("ADMIN_PASSWORD")
SHARED = env("SHARED") == "1"
QBIT = env("QBIT_URL", "http://gluetun:8080").rstrip("/")
NZBGET = env("NZBGET_URL").rstrip("/")
PROWLARR = env("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
BOOKSHELF = "http://bookshelf:8787"
ABS = "http://audiobookshelf:13378"
DASH = env("DASHBOARD_HOST", env("LAN_IP", "localhost"))
PROFILES = [p for p in env("COMPOSE_PROFILES").split(",") if p]
PODCASTS = env("PODCASTS", "1") == "1"
BOOKS, PODS = book_dirs()
CATEGORY = "audiobooks"

# Containernavn og porter på maskinen: egne (lydbok-*) eller Plex-stackens
C = {"qbit": "qbittorrent", "nzbget": "nzbget", "prowlarr": "prowlarr", "gluetun": "gluetun"} if SHARED else \
    {"qbit": "lydbok-qbittorrent", "nzbget": "lydbok-nzbget", "prowlarr": "lydbok-prowlarr", "gluetun": "lydbok-gluetun"}
PORT = {"qbit": 8080, "nzbget": 6789, "prowlarr": 9696} if SHARED else {"qbit": 8090, "nzbget": 6790, "prowlarr": 9697}

STATE = {}          # nøkler og ID-er vi finner underveis
RESULTS = []        # (app, status, melding) til oppsummeringen
TODO = []           # det som må gjøres for hånd


def result(app, good, msg):
    RESULTS.append((app, good, msg))
    (ok if good else warn)(f"{app}: {msg}")


def run(app, fn):
    step(app)
    try:
        fn()
    except Exception as e:  # én app som feiler skal ikke stoppe resten
        result(app, False, f"uventet feil: {e}")


def hostport(url, default):
    u = urllib.parse.urlparse(url)
    return u.hostname, u.port or default


# =========================================================================== *arr
class Arr:
    def __init__(self, name, url, key, ver="v1"):
        self.name, self.base, self.key, self.ver = name, url.rstrip("/"), key, ver

    def call(self, method, path, body=None):
        r = http(method, f"{self.base}/api/{self.ver}/{path}", json_body=body,
                 headers={"X-Api-Key": self.key, "Accept": "application/json"}, timeout=60)
        if not r.ok:
            raise RuntimeError(f"{method} {path} → {r.status} {r.text[:200]}")
        return r.json()

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)

    def put(self, path, body):
        return self.call("PUT", path, body)

    def put_config(self, name, obj):
        try:
            return self.put(f"config/{name}/{obj.get('id', 1)}", obj)
        except RuntimeError:
            return self.put(f"config/{name}", obj)

    def set_auth(self):
        if not PASSWORD:
            return
        h = self.get("config/host")
        h.update(authenticationMethod="forms", authenticationRequired="enabled",
                 username=USER, password=PASSWORD, passwordConfirmation=PASSWORD)
        self.put_config("host", h)

    def upsert(self, kind, implementation, name, values, extra=None, match=None):
        """Oppretter eller oppdaterer en nedlastingsklient/app. Setter bare felt appen kjenner."""
        items = [i for i in self.get(kind) if i.get("implementation") == implementation
                 and (match is None or match(i))]
        if items:
            obj, verb = items[0], "oppdatert"
        else:
            obj = next(s for s in self.get(f"{kind}/schema") if s.get("implementation") == implementation)
            obj.update(name=name, enable=True)
            verb = "lagt til"
        for f in obj.get("fields", []):
            if f.get("name") in values:
                f["value"] = values[f["name"]]
        for k, v in (extra or {}).items():
            if k in obj:
                obj[k] = v
        if "id" in obj and items:
            self.put(f"{kind}/{obj['id']}?forceSave=true", obj)
        else:
            self.post(f"{kind}?forceSave=true", obj)
        return verb


def field(obj, name):
    return next((f.get("value") for f in obj.get("fields", []) if f.get("name") == name), None)


def key_from_xml(text):
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", text or "")
    return m.group(1).strip() if m else ""


def arr_key(app):
    path = f"{CFG}/{app}/config.xml"
    if not wait_file(path, 180):
        return ""
    for _ in range(20):                 # fila lages før nøkkelen skrives
        k = key_from_xml(read(path))
        if k:
            return k
        time.sleep(3)
    return ""


def wait_api(a, url):
    if not wait_http(f"{url}/ping", 240, a.name):
        return False
    for _ in range(20):                 # API-et kan bruke litt tid etter /ping
        try:
            a.get("system/status")
            return True
        except RuntimeError:
            time.sleep(3)
    return False


# =========================================================================== qBittorrent
def setup_qbit():
    s = Session()
    h = {"Referer": QBIT, "Origin": QBIT}
    if not wait_http(f"{QBIT}/api/v2/app/version", 240, "qBittorrent"):
        TODO.append(f"qBittorrent/VPN: sjekk «docker logs {C['gluetun']}» – VPN-en må være oppe for nedlasting.")
        return result("qBittorrent", False, "svarer ikke (er VPN-en oppe?)")

    def logged_in():
        return s.http("GET", f"{QBIT}/api/v2/app/version", headers=h).status == 200

    def login(u, p):
        r = s.http("POST", f"{QBIT}/api/v2/auth/login", headers=h, form={"username": u, "password": p})
        if not (r.status in (200, 204) and "fail" not in r.text.lower()):   # qBittorrent 5 svarer 204
            log(f"  innlogging som {u}: {r.status} {r.text.strip()[:80]}")
            return False
        return True

    if not logged_in() and not (PASSWORD and login(USER, PASSWORD)):
        if SHARED:
            TODO.append("qBittorrent i Plex-stack: brukernavn/passord i .env må være de samme som der. "
                        "Rett ADMIN_USER/ADMIN_PASSWORD og kjør «docker compose run --rm autoconfig».")
            return result("qBittorrent", False, "kom ikke inn i Plex-stackens qBittorrent")
        temp = re.findall(r"temporary password[^:]*:\s*(\S+)", docker_logs(C["qbit"]), re.I)
        if not (temp and login("admin", temp[-1])):
            log("  kommer ikke inn med passord – skriver innlogging direkte i qBittorrent sin konfig")
            if not qbit_write_config():
                return result("qBittorrent", False, "fant ikke konfigfila til qBittorrent")
            if not wait_http(f"{QBIT}/api/v2/app/version", 120, "qBittorrent"):
                return result("qBittorrent", False, "startet ikke igjen")
            for _ in range(20):
                if logged_in():
                    break
                time.sleep(3)
            else:
                TODO.append(f"qBittorrent: kom ikke inn – send «docker logs {C['qbit']} --tail 30» til den som hjelper deg.")
                return result("qBittorrent", False, "kom ikke inn selv etter at konfigfila ble skrevet")
            ok("innlogging satt i konfigfila")

    if not SHARED:
        prefs = {"save_path": "/data/downloads", "temp_path_enabled": True,
                 "temp_path": "/data/downloads/incomplete",
                 "bypass_auth_subnet_whitelist_enabled": True,
                 "bypass_auth_subnet_whitelist": "172.16.0.0/12"}
        if PASSWORD:
            prefs.update(web_ui_username=USER, web_ui_password=PASSWORD)
        r = s.http("POST", f"{QBIT}/api/v2/app/setPreferences", headers=h, form={"json": json.dumps(prefs)})
        if not r.ok:
            return result("qBittorrent", False, f"innstillinger feilet ({r.status})")
        ok("nedlastingsmapper, passord og innlogging for containere satt")

    r = s.http("GET", f"{QBIT}/api/v2/torrents/categories", headers=h)
    cats = (r.json() or {}) if r.ok else {}
    if CATEGORY not in cats:
        s.http("POST", f"{QBIT}/api/v2/torrents/createCategory", headers=h,
               form={"category": CATEGORY, "savePath": f"/data/downloads/{CATEGORY}"})
    ok(f"kategorien {CATEGORY} finnes")
    STATE["qbit_ok"] = True
    result("qBittorrent", True, "klar" + (" (Plex-stackens)" if SHARED else ""))


def qbit_pbkdf2(password):
    """qBittorrent sitt passordformat: PBKDF2-HMAC-SHA512, 100 000 runder, 16 byte salt."""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"


def qbit_write_config():
    paths = [os.path.join(d, f) for d, _, files in os.walk(f"{CFG}/qbittorrent")
             for f in files if f == "qBittorrent.conf"]
    if not paths:
        return False
    docker_action(C["qbit"], "stop")      # qBittorrent skriver over fila når den avslutter
    for path in paths:
        txt = read(path)
        sets = {"WebUI\\AuthSubnetWhitelistEnabled": "true",
                "WebUI\\AuthSubnetWhitelist": "172.16.0.0/12",
                "WebUI\\MaxAuthenticationFailCount": "10"}
        if PASSWORD:
            sets.update({"WebUI\\Username": USER, "WebUI\\Password_PBKDF2": f'"{qbit_pbkdf2(PASSWORD)}"'})
        for k, v in sets.items():
            txt = ini_set(txt, "Preferences", k, v, sep="=")
        write(path, txt, owner=(PUID, PGID))
        log(f"  skrev {path.replace(CFG, 'config')}")
    docker_action(C["qbit"], "start")
    return True


# =========================================================================== NZBGet
def setup_nzbget():
    if not wait_http(NZBGET + "/", 180, "NZBGet"):
        return result("NZBGet", False, "svarer ikke")

    def rpc(method, params, creds):
        auth = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        r = http("POST", NZBGET + "/jsonrpc", headers={"Authorization": f"Basic {auth}"},
                 json_body={"method": method, "params": params, "id": 1}, timeout=60)
        return r.json().get("result") if r.ok and r.json() else None

    tries = ((USER, PASSWORD),) if SHARED else ((USER, PASSWORD), ("nzbget", "tegbzn6789"))
    creds = next((c for c in tries if c[1] and rpc("version", [], c) is not None), None)
    if not creds:
        return result("NZBGet", False, "kom ikke inn (sjekk ADMIN_USER/ADMIN_PASSWORD i .env)")
    cfg = rpc("config", [], creds) or []
    if not cfg:
        return result("NZBGet", False, "fikk ikke lest innstillingene")
    current = {o["Name"]: o["Value"] for o in cfg}

    want = {}
    nums = [int(m.group(1)) for k in current if (m := re.fullmatch(r"Category(\d+)\.Name", k))]
    if not any(current.get(f"Category{n}.Name", "").lower() == CATEGORY for n in nums):
        n = max(nums, default=0) + 1
        want.update({f"Category{n}.Name": CATEGORY, f"Category{n}.DestDir": ""})
    if not SHARED:
        want.update({"DestDir": "/data/downloads/usenet/completed",
                     "InterDir": "/data/downloads/usenet/intermediate",
                     "PostStrategy": "sequential",   # én utpakking om gangen – skåner prosessoren
                     "HealthCheck": "delete"})       # ødelagt utgivelse: avbryt, så Bookshelf tar neste
        if PASSWORD:
            want.update(ControlUsername=USER, ControlPassword=PASSWORD)
        host, port = env("USENET_HOST"), env("USENET_PORT", "563")
        if host and env("USENET_USER"):
            want.update({"Server1.Active": "yes", "Server1.Name": host, "Server1.Level": "0",
                         "Server1.Host": host, "Server1.Port": port,
                         "Server1.Encryption": "yes" if port in ("563", "443", "80", "81") else "no",
                         "Server1.Username": env("USENET_USER"), "Server1.Password": env("USENET_PASSWORD"),
                         "Server1.Connections": env("USENET_CONNECTIONS", "20")})
        else:
            TODO.append("NZBGet: legg inn Usenet-serveren under Settings → News-Servers (eller kjør veiviseren).")
    STATE["nzbget_ok"] = True
    if all(current.get(k) == v for k, v in want.items()):
        return result("NZBGet", True, "klar (uendret)")
    opts = [{"Name": o["Name"], "Value": want.pop(o["Name"], o["Value"])} for o in cfg]
    opts += [{"Name": k, "Value": v} for k, v in want.items()]
    if not rpc("saveconfig", [opts], creds):
        return result("NZBGet", False, "lagring av innstillinger feilet")
    rpc("reload", [], creds)
    ok(f"kategorien {CATEGORY} finnes" + ("" if SHARED else ", server og mapper satt"))
    result("NZBGet", True, "klar" + (" (Plex-stackens)" if SHARED else ""))


# =========================================================================== Bookshelf
CAT_FIELDS = ("bookCategory", "musicCategory", "category")


def setup_bookshelf():
    key = arr_key("bookshelf")
    if not key:
        return result("Bookshelf", False, "fant ikke API-nøkkel (har containeren startet?)")
    STATE["bookshelf_key"] = key
    a = Arr("Bookshelf", BOOKSHELF, key)
    if not wait_api(a, BOOKSHELF):
        return result("Bookshelf", False, "svarer ikke")
    a.set_auth()
    ok("innlogging satt")

    qp = a.get("qualityprofile")
    spoken = next((p for p in qp if p["name"].lower() in ("spoken", "audiobook", "lydbok")), None) \
        or next((p for p in qp if any(w in p["name"].lower() for w in ("spoken", "audio"))), qp[0] if qp else None)
    mp = a.get("metadataprofile")
    meta = next((p for p in mp if p["name"].lower() == "standard"), mp[0] if mp else None)
    if not (spoken and meta):
        return result("Bookshelf", False, "fant ingen kvalitets- eller metadataprofil")

    path = f"/data/{BOOKS}"
    roots = a.get("rootfolder")
    mine = next((r for r in roots if r["path"].rstrip("/") == path), None)
    want = {"defaultQualityProfileId": spoken["id"], "defaultMetadataProfileId": meta["id"],
            "defaultMonitorOption": "all", "defaultNewItemMonitorOption": "all"}
    if not mine:
        a.post("rootfolder", {"name": "Lydbøker", "path": path, "defaultTags": [],
                              "isCalibreLibrary": False, **want})
        ok(f"rotmappe {path} lagt til (profil {spoken['name']})")
    elif any(mine.get(k) != v for k, v in want.items() if k in mine):
        a.put(f"rootfolder/{mine['id']}", {**mine, **want})
        ok(f"rotmappe {path} bruker nå profilen {spoken['name']}")
    else:
        ok(f"rotmappe {path} finnes")
    authors = a.get("author")
    for r in roots:                      # rydd bort gamle, tomme rotmapper under /data
        rp = r["path"].rstrip("/")
        if rp == path or not rp.startswith("/data/"):
            continue
        in_use = any((au.get("path") or "").startswith(rp + "/") for au in authors)
        if not in_use and not _has_content(rp):
            try:
                a.call("DELETE", f"rootfolder/{r['id']}")
                ok(f"fjernet ubrukt rotmappe {rp}")
            except RuntimeError:
                pass

    try:
        dc = a.get("config/downloadclient")
        if isinstance(dc, dict) and not dc.get("autoRedownloadFailed", True):
            dc["autoRedownloadFailed"] = True
            a.put_config("downloadclient", dc)
        ok("prøver neste utgivelse automatisk hvis en nedlasting feiler")
    except (RuntimeError, ValueError):
        pass

    mm = a.get("config/mediamanagement")
    if isinstance(mm, dict) and not mm.get("copyUsingHardlinks", True):
        mm["copyUsingHardlinks"] = True
        a.put_config("mediamanagement", mm)
    ok("hardlinks på (ingen ekstra plass brukes mens filen deles)")
    try:
        nm = a.get("config/naming")
        if isinstance(nm, dict) and not nm.get("renameBooks", False):
            nm["renameBooks"] = True
            a.put_config("naming", nm)
        ok("ryddige mappenavn: Forfatter/Tittel")
    except RuntimeError:
        pass

    clients = [("qBittorrent", "QBittorrent", STATE.get("qbit_ok"), (*hostport(QBIT, 8080),))]
    if NZBGET:
        clients.append(("NZBGet", "Nzbget", STATE.get("nzbget_ok"), (*hostport(NZBGET, 6789),)))
    missing = []
    for label, impl, ready, (host, port) in clients:
        if not ready:
            missing.append(label)
            warn(f"{label} hoppet over – ikke klar (se {label} over)")
            continue
        try:
            v = a.upsert("downloadclient", impl, label,
                         {"host": host, "port": port, "useSsl": False, "username": USER,
                          "password": PASSWORD, **{f: CATEGORY for f in CAT_FIELDS}}, {"priority": 1})
            ok(f"{label} {v} (kategori {CATEGORY})")
        except RuntimeError as e:
            missing.append(label)
            warn(f"{label}: {str(e)[:160]}")
    if missing:
        TODO.append(f"{' og '.join(missing)} var ikke klar(e). Når de kjører: «docker compose run --rm autoconfig».")
    result("Bookshelf", True, "klar" if not missing else f"klar, men mangler {' og '.join(missing)}")


# =========================================================================== Prowlarr
def has_audiobooks(ix):
    """Har indekseren lydbokkategorien (3030) eller hele lydkategorien (3000)?"""
    def walk(cats):
        return any(c.get("id") in (3000, 3030) or walk(c.get("subCategories") or []) for c in cats or [])
    return walk((ix.get("capabilities") or {}).get("categories"))


def prowlarr_key():
    if not SHARED:
        return arr_key("prowlarr")
    code, out = docker_exec(C["prowlarr"], ["cat", "/config/config.xml"], timeout=30)
    return key_from_xml(out) if code == 0 else ""


def setup_prowlarr():
    key = prowlarr_key()
    if not key:
        return result("Prowlarr", False, "fant ikke API-nøkkel" + (" (kjører Plex-stack?)" if SHARED else ""))
    STATE["prowlarr_key"] = key
    p = Arr("Prowlarr", PROWLARR, key)
    if not wait_api(p, PROWLARR):
        return result("Prowlarr", False, "svarer ikke")

    tag = None
    if not SHARED:                       # Plex-stack har allerede innlogging og FlareSolverr
        p.set_auth()
        ok("innlogging satt")
        tags = p.get("tag")
        tag = next((t for t in tags if t["label"] == "flaresolverr"), None) or p.post("tag", {"label": "flaresolverr"})
        p.upsert("indexerproxy", "FlareSolverr", "FlareSolverr",
                 {"host": "http://flaresolverr:8191/", "requestTimeout": 60}, {"tags": [tag["id"]]})
        ok("FlareSolverr-proxy med taggen «flaresolverr»")

    if STATE.get("bookshelf_key"):
        v = p.upsert("applications", "Readarr", "Bookshelf (lydbøker)",
                     {"prowlarrUrl": PROWLARR, "baseUrl": BOOKSHELF, "apiKey": STATE["bookshelf_key"],
                      "syncCategories": [3030]},
                     {"syncLevel": "fullSync"},
                     match=lambda i: (field(i, "baseUrl") or "").rstrip("/") == BOOKSHELF)
        ok(f"Bookshelf {v} – lydbokindeksere synkes automatisk dit")

    indexers = p.get("indexer")
    if tag:
        tagged = 0
        for ix in indexers:
            if tag["id"] not in ix.get("tags", []):
                ix.setdefault("tags", []).append(tag["id"])
                try:
                    p.put(f"indexer/{ix['id']}?forceSave=true", ix)
                    tagged += 1
                except RuntimeError:
                    pass
        if tagged:
            ok(f"{tagged} indeksere fikk FlareSolverr-taggen")
    books = [ix for ix in indexers if has_audiobooks(ix)]
    if not books:
        TODO.append(f"Prowlarr (http://{DASH}:{PORT['prowlarr']}): legg til minst én indekser som har "
                    "lydbøker (Indexers → Add Indexer). Den kobles automatisk til Bookshelf.")
    indexers = [f"{len(indexers)} indeksere, {len(books)} med lydbøker"]
    result("Prowlarr", True, f"klar ({indexers[0]}" + (", Plex-stackens)" if SHARED else ")"))


# =========================================================================== Audiobookshelf
def find_value(obj, name):
    """Leter etter en nøkkel i et svar uansett hvor dypt den ligger."""
    if isinstance(obj, dict):
        if isinstance(obj.get(name), str) and obj[name]:
            return obj[name]
        for v in obj.values():
            r = find_value(v, name)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_value(v, name)
            if r:
                return r
    return ""


def setup_abs():
    if not wait_http(f"{ABS}/healthcheck", 300, "Audiobookshelf"):
        return result("Audiobookshelf", False, "svarer ikke")
    st = http("GET", f"{ABS}/status").json() or {}
    if st.get("isInit") is False:
        if not PASSWORD:
            TODO.append(f"Audiobookshelf: åpne http://{DASH}:13378 og lag en administrator.")
            return result("Audiobookshelf", False, "mangler ADMIN_PASSWORD i .env")
        r = http("POST", f"{ABS}/init", json_body={"newRoot": {"username": USER, "password": PASSWORD}})
        if not r.ok:
            return result("Audiobookshelf", False, f"oppstart feilet ({r.status} {r.text[:120]})")
        ok(f"administrator {USER} laget")

    r = http("POST", f"{ABS}/login", headers={"x-return-tokens": "true"},
             json_body={"username": USER, "password": PASSWORD})
    user = (r.json() or {}).get("user") or {} if r.ok else {}
    token = user.get("accessToken") or user.get("token")
    if not token:
        TODO.append("Audiobookshelf: kom ikke inn med brukernavn/passord fra .env. Er passordet endret i appen? "
                    "Sett det tilbake, eller rett ADMIN_PASSWORD og kjør autoconfig igjen.")
        return result("Audiobookshelf", False, f"innlogging feilet ({r.status})")
    h = {"Authorization": f"Bearer {token}"}

    lang = env("ABS_LANGUAGE", "no")
    r = http("PATCH", f"{ABS}/api/settings", headers=h, json_body={"language": lang})
    if r.ok:
        ok(f"språk: {lang}")

    r = http("GET", f"{ABS}/api/libraries", headers=h)
    data = r.json() if r.ok else None
    libs = data.get("libraries", []) if isinstance(data, dict) else (data or [])
    paths = {f.get("fullPath", "").rstrip("/") for lib in libs for f in lib.get("folders", [])}
    wanted = [("Lydbøker", f"/data/{BOOKS}", "book", env("ABS_PROVIDER", "audible"), "audiobookshelf")]
    if PODCASTS:
        wanted.append(("Podkaster", f"/data/{PODS}", "podcast", "itunes", "podcast"))
    for name, path, mtype, provider, icon in wanted:
        if path in paths:
            ok(f"bibliotek for {path} finnes")
            continue
        r = http("POST", f"{ABS}/api/libraries", headers=h, json_body={
            "name": name, "folders": [{"fullPath": path}], "icon": icon, "mediaType": mtype,
            "provider": provider, "settings": {"autoScanCronExpression": "0 4 * * *"}})
        lib = r.json() or {}
        if r.ok and lib.get("id"):
            http("POST", f"{ABS}/api/libraries/{lib['id']}/scan", headers=h)
            ok(f"bibliotek «{name}» laget ({path}, bokinfo fra {provider})")
        else:
            warn(f"klarte ikke lage «{name}» ({r.status} {r.text[:120]})")

    STATE["abs_key"] = abs_api_key(h, user)
    users = http("GET", f"{ABS}/api/users", headers=h).json()
    users = users.get("users") if isinstance(users, dict) else users
    if isinstance(users, list) and len(users) <= 1:
        TODO.append("Audiobookshelf: lag en egen bruker til hver i familien (Innstillinger → Brukere). "
                    "Da får alle sin egen fremdrift og bokmerker.")
    result("Audiobookshelf", True, "klar – lytt i nettleseren eller i appen Audiobookshelf")


def abs_api_key(h, user):
    """Varig API-nøkkel til dashboardet (Audiobookshelf 2.26+). Lagres og gjenbrukes."""
    path = f"{CFG}/lydbok/abs-apikey"
    saved = read(path).strip()
    if saved and http("GET", f"{ABS}/api/me", headers={"Authorization": f"Bearer {saved}"}).ok:
        return saved
    r = http("GET", f"{ABS}/api/api-keys", headers=h)
    if r.status == 404:                  # eldre versjon uten API-nøkler: bruk det gamle tokenet
        return user.get("token", "")
    data = r.json() if r.ok else None
    for k in (data.get("apiKeys", []) if isinstance(data, dict) else []):
        if k.get("name") == "lydbok-stack":
            http("DELETE", f"{ABS}/api/api-keys/{k['id']}", headers=h)
    r = http("POST", f"{ABS}/api/api-keys", headers=h,
             json_body={"name": "lydbok-stack", "userId": user.get("id"), "isActive": True})
    key = find_value(r.json(), "apiKey") if r.ok else ""
    if key:
        write(path, key, 0o600, (PUID, PGID))
        ok("API-nøkkel til dashboardet laget")
    else:
        warn(f"fikk ikke laget API-nøkkel ({r.status}) – dashboardet viser Audiobookshelf uten tall")
    return key


# =========================================================================== Homepage
def setup_homepage():
    k = STATE
    src = " (Plex-stack)" if SHARED else ""

    def svc(name, icon, port, desc, cont=None, widget=None, href=True):
        lines = [f"    - {name}:", f"        icon: {icon}", f"        description: {desc}"]
        if href:
            lines.append(f"        href: http://{DASH}:{port}")
        if cont:
            lines += ["        server: my-docker", f"        container: {cont}"]
        if widget:
            lines.append("        widget:")
            lines += [f"          {a}: {json.dumps(b)}" for a, b in widget.items()]
        return "\n".join(lines)

    def w(typ, url, key=None, **extra):
        d = {"type": typ, "url": url}
        if key:
            d["key"] = key
        d.update(extra)
        return d

    groups = {
        "Lytt": [svc("Audiobookshelf", "audiobookshelf.png", 13378, "Lydbøker og podkaster", "audiobookshelf",
                     w("audiobookshelf", ABS, k["abs_key"]) if k.get("abs_key") else None)],
        "Bibliotek": [svc("Bookshelf", "readarr.png", 8787, "Følg forfattere, hent lydbøker", "bookshelf",
                          w("readarr", BOOKSHELF, k["bookshelf_key"]) if k.get("bookshelf_key") else None)],
        "Nedlasting": [
            svc("qBittorrent", "qbittorrent.png", PORT["qbit"], "Torrent" + (" via VPN" if "gluetun" in QBIT else "") + src,
                C["qbit"], w("qbittorrent", QBIT, None, username=USER, password=PASSWORD) if PASSWORD else None),
            svc("Prowlarr", "prowlarr.png", PORT["prowlarr"], "Indeksere" + src, C["prowlarr"],
                w("prowlarr", PROWLARR, k["prowlarr_key"]) if k.get("prowlarr_key") else None),
        ],
    }
    if "gluetun" in QBIT:
        groups["Nedlasting"].insert(1, svc("Gluetun", "gluetun.png", 0, "VPN-tunnel" + src, C["gluetun"], href=False))
    if NZBGET:
        groups["Nedlasting"].insert(1, svc("NZBGet", "nzbget.png", PORT["nzbget"], "Usenet" + src, C["nzbget"],
                                           w("nzbget", NZBGET, None, username=USER, password=PASSWORD) if PASSWORD else None))
    if "kjoling" in PROFILES:
        groups["Vedlikehold"] = [
            svc("Kjøling", "mdi-thermometer", 3011, "Maskinens belastning – roer ned ved behov", "lydbok-kjoling",
                {"type": "customapi", "url": "http://kjoling:3011/api/status", "refreshInterval": 10000,
                 "mappings": [{"field": "status", "label": "Modus"},
                              {"field": "temp", "label": "Temp", "format": "number", "suffix": " °C"},
                              {"field": "cpu", "label": "Prosessor", "format": "number", "suffix": " %"},
                              {"field": "mem", "label": "Minne", "format": "number", "suffix": " %"}]}),
            svc("FlareSolverr", "flaresolverr.png", 0, "Cloudflare-hjelper", "lydbok-flaresolverr", href=False),
        ]

    out = ["# Laget av autoconfig – kjøres på nytt med «docker compose run --rm autoconfig»", ""]
    for g, items in groups.items():
        out.append(f"- {g}:")
        out += items
    write(f"{CFG}/homepage/services.yaml", "\n".join(out) + "\n", 0o600, (PUID, PGID))
    result("Homepage", True, f"dashboard på http://{DASH}:3100")


# =========================================================================== oppsummering
def summary():
    lines = ["", "═" * 66, "  OPPSUMMERING", "═" * 66]
    for app, good, msg in RESULTS:
        lines.append(f"  {'✔' if good else '⚠'} {app:<15} {msg}")
    lines += ["", f"  Lytt:        http://{DASH}:13378",
              f"  Mobil:       appen «Audiobookshelf» (iPhone/Android), server http://{env('LAN_IP') or DASH}:13378",
              f"  Dashboard:   http://{DASH}:3100",
              f"  Mapper:      {BOOKS}/" + (f" og {PODS}/" if PODCASTS else "") + " i mediemappa"]
    if PASSWORD:
        lines.append(f"  Innlogging:  {USER} / {PASSWORD}")
    if TODO:
        lines += ["", "  Gjør dette for hånd:"]
        lines += [f"   • {t}" for t in dict.fromkeys(TODO)]
    lines += ["", "  Kjør oppsettet på nytt når som helst:  docker compose run --rm autoconfig", ""]
    text = "\n".join(lines)
    log(text)
    write(f"{CFG}/OPPSUMMERING.txt", text.replace(f" / {PASSWORD}", " / (se .env)") if PASSWORD else text,
          owner=(PUID, PGID))


def main():
    log("autoconfig: setter opp og kobler sammen appene. Første gang tar dette 2–5 minutter.")
    log(f"Lydbøker: /data/{BOOKS}" + (f"   Podkaster: /data/{PODS}" if PODCASTS else "")
        + ("   Nedlasting: Plex-stack" if SHARED else ""))
    run("Audiobookshelf", setup_abs)
    run("qBittorrent", setup_qbit)
    if NZBGET:
        run("NZBGet", setup_nzbget)
    run("Bookshelf", setup_bookshelf)
    run("Prowlarr", setup_prowlarr)
    run("Homepage", setup_homepage)
    summary()


if __name__ == "__main__":
    main()
    sys.exit(0)
