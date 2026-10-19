#!/usr/bin/env python3
"""
Mist MAC Update Utility
=======================
Small Flask web app for maintaining a Mist org's Client List (usermacs)
without giving people the full Mist dashboard.

  * Search by Site (built live from the org), VLAN and Client Label (both from
    the .ini). "All" means every entry in the .ini, not every value in Mist.
  * Edit the Site, VLAN and/or Client Label of a matching entry - only values
    from the .ini (VLANs, labels) or the org (sites) can be chosen.
  * Add a new MAC with a Site, VLAN and Client Label.

There is deliberately no delete and no MAC change.

Usage:
    python mist_mac_update_utility.py [path/to/mist_mac_update_utility.ini]
then open http://127.0.0.1:5050 (host/port are set in the .ini).
"""

import configparser
import datetime
import functools
import hmac
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import quote, urlparse

import requests
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
INI_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(APP_DIR, "mist_mac_update_utility.ini")
INI_NAME = os.path.basename(INI_PATH)
REQUEST_TIMEOUT = 60
ALL = "__all__"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config():
    if not os.path.isfile(INI_PATH):
        sys.exit(f"{INI_PATH} not found - copy mist_mac_update_utility.ini.example to "
                 f"mist_mac_update_utility.ini and fill it in.")
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str  # keep label case exactly as written
    config.read(INI_PATH, encoding="utf-8")
    return config


config = load_config()
ORG_ID = config.get("mist", "org_id", fallback="").strip()
API_TOKEN = config.get("mist", "api_token", fallback="").strip()
CLOUD = config.get("mist", "cloud", fallback="").strip()
if not ORG_ID or not API_TOKEN or not CLOUD or "YOUR_" in ORG_ID + API_TOKEN:
    sys.exit(f"Set org_id, api_token and cloud in [mist] of {INI_NAME} before running.")

HOST = config.get("app", "host", fallback="127.0.0.1").strip() or "127.0.0.1"
PORT = config.getint("app", "port", fallback=5050)
PIN = config.get("app", "pin", fallback="").strip()
CHANGE_LOG = config.get("app", "change_log", fallback="").strip()
if CHANGE_LOG and not os.path.isabs(CHANGE_LOG):
    CHANGE_LOG = os.path.join(APP_DIR, CHANGE_LOG)


def ini_list(section):
    """[(value, description), ...] in .ini order, de-duplicated."""
    if section not in config:
        return []
    seen, items = set(), []
    for key, desc in config[section].items():
        key = key.strip()
        if key and key not in seen:
            seen.add(key)
            items.append((key, (desc or "").strip()))
    return items


VLANS = ini_list("vlans")
LABELS = ini_list("labels")
VLAN_IDS = [v for v, _ in VLANS]
LABEL_NAMES = [l for l, _ in LABELS]
if not VLANS or not LABELS:
    sys.exit(f"{INI_NAME} needs at least one entry in both [vlans] and [labels].")
bad_vlans = [v for v in VLAN_IDS if not re.fullmatch(r"\d{1,4}", v) or not 1 <= int(v) <= 4094]
if bad_vlans:
    sys.exit(f"[vlans] in {INI_NAME} has invalid VLAN IDs (must be 1-4094): {', '.join(bad_vlans)}")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class MistError(Exception):
    """An API problem worth showing to the user as-is."""


def build_session():
    """Shared HTTP session, wired up for a TLS-inspecting proxy (e.g. Zscaler)
    when the optional [network] section is present in the .ini."""
    s = requests.Session()
    s.headers.update({"Authorization": f"Token {API_TOKEN}"})
    if "network" not in config:
        return s

    section = config["network"]
    ca_bundle = section.get("ca_bundle", "").strip()
    if ca_bundle:
        ca_path = os.path.expanduser(ca_bundle)
        if not os.path.isfile(ca_path):
            sys.exit(f"[network] ca_bundle does not exist: {ca_path}\n"
                     f"This should be a PEM file containing your proxy's root CA "
                     f"certificate (e.g. exported from Zscaler).")
        s.verify = ca_path
    elif not section.getboolean("verify_ssl", fallback=True):
        s.verify = False
        print("WARNING: TLS certificate verification is DISABLED ([network] verify_ssl = false). "
              "Only use this as a last resort.")
        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

    for scheme in ("http", "https"):
        proxy = section.get(f"{scheme}_proxy", "").strip()
        if proxy:
            s.proxies[scheme] = proxy
    return s


SESSION = build_session()


def cloud_to_host(cloud):
    """Normalise the .ini cloud value to an API host URL like https://api.eu.mist.com."""
    value = cloud.lower().strip()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.strip("/")
    if value.endswith("mist.com"):
        if value.startswith("manage."):
            value = "api." + value[len("manage."):]
        return f"https://{value}"
    if value in ("us", "global", "global01"):
        return "https://api.mist.com"
    return f"https://api.{value}.mist.com"


API_HOST = cloud_to_host(CLOUD)
API_BASE = f"{API_HOST}/api/v1"
USERMACS = f"{API_BASE}/orgs/{ORG_ID}/usermacs"


def api(method, url, body=None, max_retries=5):
    """Call the Mist API with 429 back-off; raise MistError with a readable reason."""
    url = url if url.startswith("http") else f"{API_BASE}{url}"
    for attempt in range(1, max_retries + 1):
        try:
            resp = SESSION.request(method, url, json=body, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.SSLError as e:
            raise MistError(f"TLS certificate verification failed: {e}. If you're behind a "
                            f"TLS-inspecting proxy (e.g. Zscaler), set [network] ca_bundle in {INI_NAME}.")
        except requests.exceptions.ProxyError as e:
            raise MistError(f"Could not reach the proxy: {e}. Check [network] http_proxy / "
                            f"https_proxy in {INI_NAME} or your HTTP(S)_PROXY environment variables.")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            raise MistError(f"Could not connect to {urlparse(url).netloc}: {e}")
        if resp.status_code == 429 and attempt < max_retries:
            time.sleep(int(resp.headers.get("Retry-After", 10)))
            continue
        if resp.status_code == 401:
            raise MistError(f"api_token is not valid on {urlparse(API_HOST).netloc} - check the "
                            f"token and cloud in {INI_NAME}.")
        if resp.status_code == 403:
            raise MistError(f"api_token does not have permission for this ({method} {urlparse(url).path}). "
                            f"Editing the Client List needs a token with write access to the org.")
        if not resp.ok:
            detail = resp.text[:300]
            try:
                detail = resp.json().get("detail", detail)
            except ValueError:
                pass
            raise MistError(f"Mist API returned HTTP {resp.status_code} for {method} "
                            f"{urlparse(url).path}: {detail}")
        return resp.json() if resp.content else None
    raise MistError("Mist API kept rate-limiting (HTTP 429) - try again in a minute.")


def api_search(url):
    """Search endpoints - follow the `next` cursor (never page=, total is unreliable)."""
    results = []
    while url:
        data = api("GET", url)
        results.extend(data.get("results", []))
        nxt = data.get("next")
        url = f"{API_HOST}{nxt}" if nxt else None
    return results


# --------------------------------------------------------------------------
# Mist data
# --------------------------------------------------------------------------

ORG_NAME = ""


def check_org():
    """Fail fast at start-up if the cloud / token / org don't line up."""
    global ORG_NAME
    try:
        ORG_NAME = api("GET", f"/orgs/{ORG_ID}").get("name", ORG_ID)
    except MistError as e:
        sys.exit(f"{e}\nCheck [mist] in {INI_NAME}.")


def get_sites():
    """{site_id: site_name}, sorted by name. Fetched live every time."""
    sites, page = [], 1
    while True:
        batch = api("GET", f"/orgs/{ORG_ID}/sites?limit=1000&page={page}")
        sites.extend(batch)
        if len(batch) < 1000:
            break
        page += 1
    return dict(sorted(((s["id"], s.get("name") or s["id"]) for s in sites),
                       key=lambda kv: kv[1].lower()))


def search_usermacs(label=None):
    """All Client List entries, or only those carrying one label (server-side filter)."""
    url = f"{USERMACS}/search?limit=1000"
    if label:
        url += f"&labels={quote(label)}"
    return api_search(url)


def find_by_mac(mac):
    """Exact-match lookup (the search's mac= filter is a substring match)."""
    return [r for r in api_search(f"{USERMACS}/search?limit=1000&mac={mac}")
            if r.get("mac", "").lower() == mac]


def get_usermac(usermac_id):
    """One entry, with created_at/updated_at (GET by id leaves those out, search has them)."""
    rec = api("GET", f"{USERMACS}/{usermac_id}")
    full = next((r for r in find_by_mac(rec.get("mac", "")) if r.get("id") == usermac_id), None)
    return full or rec


def normalise_mac(value):
    mac = re.sub(r"[^0-9a-fA-F]", "", value or "").lower()
    return mac if len(mac) == 12 else None


def fmt_mac(mac):
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2)) if mac and len(mac) == 12 else mac


def record_view(rec, sites):
    """Flatten a usermac for the table / JSON."""
    site_ids = rec.get("site_ids") or []
    labels = rec.get("labels") or []
    return {
        "id": rec["id"],
        "mac": rec.get("mac", ""),
        "mac_display": fmt_mac(rec.get("mac", "")),
        "name": rec.get("name", ""),
        "notes": rec.get("notes", ""),
        "vlan": str(rec.get("vlan", "")),
        "labels": labels,
        # editable label = the first label that is in the .ini list
        "label": next((l for l in labels if l in LABEL_NAMES), labels[0] if labels else ""),
        "site_ids": site_ids,
        "site_id": site_ids[0] if site_ids else "",
        "site_names": [sites.get(s, s) for s in site_ids],
        "updated_at": rec.get("updated_at"),
        "updated_display": (datetime.datetime.fromtimestamp(rec["updated_at"]).strftime("%d/%m/%Y %H:%M")
                            if rec.get("updated_at") else ""),
    }


def log_change(action, before, after):
    if not CHANGE_LOG:
        return
    entry = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "client": request.remote_addr,
        "action": action,
        "mac": (after or before or {}).get("mac"),
        "before": before and {k: before.get(k) for k in ("site_ids", "vlan", "labels")},
        "after": after and {k: after.get(k) for k in ("site_ids", "vlan", "labels")},
    }
    with open(CHANGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # new each start - PIN sessions don't survive a restart


def pin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if PIN and not session.get("ok"):
            if request.path.startswith("/api/"):
                return jsonify(error="Session expired - reload the page and enter the PIN."), 401
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def globals_for_templates():
    return {"org_name": ORG_NAME, "cloud": urlparse(API_HOST).netloc, "pin_enabled": bool(PIN)}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PIN:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("pin", ""), PIN):
            session["ok"] = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") else "/")
        error = "Wrong PIN."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@pin_required
def index():
    error = None
    try:
        sites = get_sites()
    except MistError as e:
        return render_template("index.html", sites={}, vlans=VLANS, labels=LABELS, error=str(e),
                               searched=False, results=[], sel={}, all_value=ALL)

    sel = {
        "site": request.args.get("site", ALL),
        "vlan": request.args.get("vlan", ALL),
        "label": request.args.get("label", ALL),
    }
    searched = "search" in request.args
    results = []
    if searched:
        want_sites = set(sites) if sel["site"] == ALL else {sel["site"]}
        want_vlans = set(VLAN_IDS) if sel["vlan"] == ALL else {sel["vlan"]}
        want_labels = set(LABEL_NAMES) if sel["label"] == ALL else {sel["label"]}
        if not (want_sites <= set(sites) and want_vlans <= set(VLAN_IDS) and want_labels <= set(LABEL_NAMES)):
            abort(400)
        try:
            recs = search_usermacs(None if sel["label"] == ALL else sel["label"])
        except MistError as e:
            recs, error = [], str(e)
        for rec in recs:
            if (want_sites & set(rec.get("site_ids") or [])
                    and str(rec.get("vlan", "")) in want_vlans
                    and want_labels & set(rec.get("labels") or [])):
                results.append(record_view(rec, sites))
        results.sort(key=lambda r: (r["site_names"][:1], r["vlan"].zfill(4), r["label"], r["mac"]))

    return render_template("index.html", sites=sites, vlans=VLANS, labels=LABELS, error=error,
                           searched=searched, results=results, sel=sel, all_value=ALL)


def validate_choice(data, sites):
    """Check site / vlan / label against the allowed lists; return (site, vlan, label) or raise."""
    site, vlan, label = (str(data.get(k, "")).strip() for k in ("site_id", "vlan", "label"))
    if site not in sites:
        raise ValueError("Choose a valid site.")
    if vlan not in VLAN_IDS:
        raise ValueError(f"VLAN {vlan or '(blank)'} is not in the allowed list.")
    if label not in LABEL_NAMES:
        raise ValueError(f"Client Label {label or '(blank)'} is not in the allowed list.")
    return site, vlan, label


@app.route("/api/usermacs/<usermac_id>", methods=["PUT"])
@pin_required
def update_usermac(usermac_id):
    if not re.fullmatch(r"[0-9a-f-]{36}", usermac_id):
        return jsonify(error="Invalid record id."), 400
    data = request.get_json(silent=True) or {}
    try:
        sites = get_sites()
        site, vlan, label = validate_choice(data, sites)
        current = get_usermac(usermac_id)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except MistError as e:
        return jsonify(error=str(e)), 502

    # Someone else changed it since the page was loaded? Don't silently overwrite.
    seen = data.get("updated_at")
    if seen and current.get("updated_at") and abs(float(seen) - float(current["updated_at"])) > 0.001:
        return jsonify(error="This entry was changed in Mist since you loaded it. "
                             "Run the search again and re-apply your change.",
                       record=record_view(current, sites)), 409

    # Only the .ini-managed label is swapped; any other labels on the entry are kept.
    old_labels = current.get("labels") or []
    new_labels = [l for l in old_labels if l not in LABEL_NAMES]
    new_labels.insert(0, label)

    body = {
        "mac": current["mac"],  # PUT needs the MAC; it is never changed here
        "name": current.get("name", ""),
        "notes": current.get("notes", ""),
        "radius_group": current.get("radius_group", ""),
        "labels": new_labels,
        "vlan": vlan,
        "site_ids": [site],
    }
    if (old_labels == new_labels and str(current.get("vlan")) == vlan
            and (current.get("site_ids") or []) == [site]):
        return jsonify(record=record_view(current, sites), unchanged=True)

    try:
        api("PUT", f"{USERMACS}/{usermac_id}", body)
        # Don't trust the 200 - re-read and confirm the change stuck.
        after = get_usermac(usermac_id)
    except MistError as e:
        return jsonify(error=str(e)), 502
    if (str(after.get("vlan")) != vlan or (after.get("site_ids") or []) != [site]
            or (after.get("labels") or []) != new_labels):
        return jsonify(error="Mist accepted the update but the entry does not show the new values. "
                             "Check it in the Mist dashboard.", record=record_view(after, sites)), 502

    log_change("update", current, after)
    return jsonify(record=record_view(after, sites))


@app.route("/api/usermacs", methods=["POST"])
@pin_required
def add_usermac():
    data = request.get_json(silent=True) or {}
    mac = normalise_mac(data.get("mac"))
    if not mac:
        return jsonify(error="Enter a MAC address with 12 hex digits (e.g. 00:11:22:33:44:55)."), 400
    if int(mac[0:2], 16) & 1:
        return jsonify(error="That is a multicast MAC address, not a device."), 400
    try:
        sites = get_sites()
        site, vlan, label = validate_choice(data, sites)
        # Mist's POST silently overwrites an existing entry with the same MAC,
        # so check first - this utility only adds, it never replaces.
        existing = find_by_mac(mac)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except MistError as e:
        return jsonify(error=str(e)), 502
    if existing:
        return jsonify(error=f"{fmt_mac(mac)} is already in the Client List. Search for it and edit "
                             f"it instead.", record=record_view(existing[0], sites)), 409

    body = {
        "mac": mac,
        "name": str(data.get("name", "")).strip()[:64],
        "notes": str(data.get("notes", "")).strip()[:256],
        "labels": [label],
        "vlan": vlan,
        "site_ids": [site],
    }
    try:
        created = api("POST", USERMACS, body)
        after = get_usermac(created["id"])
    except MistError as e:
        return jsonify(error=str(e)), 502
    log_change("add", None, after)
    return jsonify(record=record_view(after, sites)), 201


@app.errorhandler(400)
def bad_request(_):
    return "Bad request - pick values from the drop-downs.", 400


if __name__ == "__main__":
    check_org()
    print(f"Mist MAC Update Utility - org '{ORG_NAME}' on {urlparse(API_HOST).netloc}")
    print(f"  {len(VLANS)} VLANs and {len(LABELS)} Client Labels allowed (from {INI_NAME})")
    if HOST not in ("127.0.0.1", "localhost") and not PIN:
        print("  WARNING: listening on the network with no PIN - anyone who can reach this "
              "port can change the Client List. Set [app] pin.")
    print(f"  Open http://{'127.0.0.1' if HOST == '0.0.0.0' else HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
