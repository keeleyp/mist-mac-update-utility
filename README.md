# Mist MAC Update Utility

A small Flask web app for maintaining a Juniper Mist org's **Client List** (`usermacs`, used by Mist Access Assurance MAB) without handing people the full Mist dashboard.

- **Search** by Site and Client Label. Sites are read live from the org. Client Labels come from the `.ini`, and each label is tied to a VLAN, so the drop-down shows e.g. `cctv (VLAN 240)`. **"All" means every label/VLAN pair listed in the `.ini`**, not every value in Mist.
- **Edit** the Site and/or Client Label of a matching entry. Choosing a Client Label also sets its VLAN. You review the change before it is saved.
- **Add** a new MAC address. Site and Client Label are required; Name and Notes are optional.
- **No delete and no MAC change.** Neither is exposed in the UI or the app's API.

## Setup

```bash
pip install -r requirements.txt
cp mist_mac_update_utility.ini.example mist_mac_update_utility.ini
# edit the ini: org_id, api_token, cloud, [vlans]
python mist_mac_update_utility.py
```

Then open http://127.0.0.1:5050. You can pass a different ini path as the first argument, e.g. `python mist_mac_update_utility.py customer_x.ini`.

The token needs **write** access to the org (Org Admin, or Network Admin across all sites).

## The .ini file

| Section | What it holds |
|---|---|
| `[mist]` | `org_id`, `api_token`, `cloud` (`gc3`, `eu`, `api.mist.com`, or a `manage.*` URL) |
| `[app]` | `host` / `port` to listen on, optional `pin`, and `change_log` (JSON-lines file of every add or update, with before → after) |
| `[vlans]` | One line per VLAN: `<vlan id> = <client label>[, <client label> ...]`. Label names are case-sensitive and must match Mist exactly. |
| `[network]` | Optional Zscaler/proxy settings: `ca_bundle`, `verify_ssl`, `http_proxy`, `https_proxy` |

Example `[vlans]`:

```ini
[vlans]
12  = bms_trend
232 = bms_trend
124 = gallery_general_old, gallery_audio
240 = cctv
```

When a label appears under more than one VLAN, the drop-downs list it once per VLAN: `bms_trend (VLAN 12)` and `bms_trend (VLAN 232)`. An entry matches a choice only when it has that label **and** that VLAN.

If you set `host = 0.0.0.0` so other machines can reach the app, also set a `pin`.

## Behaviour worth knowing

- **Adding a MAC that already exists.** Mist's `POST /usermacs` silently overwrites an existing entry with the same MAC. The utility therefore looks the MAC up first and refuses the add. It shows the existing entry so you can edit that instead.
- **Edits only touch Site, VLAN and Label.** The VLAN always comes from the chosen Client Label. The entry is re-read, then written back in full (`mac`, `name`, `notes` and `radius_group` are preserved). It is then read again to confirm the change stuck. Labels on the entry that are not in the `.ini` are kept. Only the `.ini`-managed label is swapped.
- **Concurrent changes.** If an entry changed in Mist after your search, the save is refused and you are asked to search again.
- **Search cost.** One search reads the whole Client List (1,000 entries per call, following the `next` cursor), or only one label's entries when a single Client Label is chosen. Site and VLAN filtering is done locally. The `labels=` search filter is a prefix match (`cctv` also returns `cctv_old`), so results are always filtered exactly afterwards.

## Behind Zscaler or a proxy

Fill in the `[network]` section. `ca_bundle` should be the path to a PEM file holding your proxy's root CA. TLS or proxy errors are shown in the page, with a pointer to that setting.
