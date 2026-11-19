"""Réglages des audits par maquette : stockage, lien signé et page de réglage à curseurs.

La page (servie par le serveur MCP, lien signé et expirant) liste, audit par audit, les paramètres de calcul avec
leur valeur par défaut, la valeur retenue, la plage et la justification ; chaque valeur numérique se règle avec une
barre glissante (ou un champ), chaque choix avec une liste. « Enregistrer » les mémorise pour la maquette : le
prochain ``run_audit`` les applique et le rapport les reprend au chapitre « Hypothèses ».
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import time
from pathlib import Path
from urllib.parse import quote

from . import audit_params as AP
from .config import get_settings
from .state import now_iso, slug

KIND = "settings"


def _path_for(owner: str, target_key: str) -> Path:
    d = get_settings().users_dir / slug(owner) / KIND
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slug(target_key)}.json"


def load(owner: str, target_key: str) -> dict:
    p = _path_for(owner, target_key)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("values") or {}
    except Exception:  # noqa: BLE001
        return {}


def save(owner: str, target_key: str, values: dict, *, merge: bool = True) -> dict:
    clean = AP.validate(values)
    current = load(owner, target_key) if merge else {}
    current.update(clean)
    current = {k: v for k, v in current.items() if v is not None}
    _path_for(owner, target_key).write_text(json.dumps({"values": current, "updated_at": now_iso()},
                                                       ensure_ascii=False, indent=1))
    return current


def _sign(*parts: str) -> str:
    return hmac.new(get_settings().file_secret.encode(), "|".join(parts).encode(), hashlib.sha256).hexdigest()[:32]


def link(owner: str, target_key: str, audits: list[str] | None = None) -> dict:
    s = get_settings()
    exp = str(int(time.time()) + s.upload_link_ttl_s)
    sig = _sign("reglages", owner, target_key, exp)
    url = f"{s.public_url}/reglages/{owner}/{exp}/{sig}?t={quote(target_key)}"
    if audits:
        url += "&a=" + quote(",".join(audits))
    return {"reglages_url": url, "expires_in_s": s.upload_link_ttl_s}


def verify(owner: str, target_key: str, exp: str, sig: str) -> bool:
    try:
        if int(exp) < time.time():
            return False
    except ValueError:
        return False
    return hmac.compare_digest(_sign("reglages", owner, target_key, exp), sig or "")


def parameters(owner: str, target_key: str, audits: list[str] | None = None) -> list[dict]:
    """Paramètres par audit, avec valeur par défaut, valeur enregistrée et plage."""
    saved = load(owner, target_key)
    out = []
    for aid, ps in AP.PARAMS.items():
        if audits and aid not in audits:
            continue
        rows = []
        for p in ps:
            d = p.to_dict()
            d["valeur"] = saved.get(p.key, p.default)
            d["modifie"] = p.key in saved and saved[p.key] != p.default
            rows.append(d)
        out.append({"audit": aid, "parametres": rows})
    return out


AUDIT_LABELS = {"c1_structure": "Structuration et intégrité IFC", "c2_clash": "Conflits (clash)",
                "c4_geometrie": "Géométrie et modélisation", "c5_pmr": "Accessibilité PMR",
                "c6_degagements": "Espaces libres et dégagements", "c7_incendie": "Sécurité incendie et évacuation"}

_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Réglages des audits — BIMData MCP</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--primary:#2F374A;--secondary:#F9C72C;--royal:#3375DD;--granite:#606060;--light:#F0F5FF;--silver:#BDBDBD}}
body{{font-family:Roboto,Arial,sans-serif;background:#F7F7F7;color:var(--primary);margin:0}}
header{{background:var(--primary);color:#fff;padding:22px 32px}} header small{{color:var(--secondary);font-weight:700;
letter-spacing:.06em}} header h1{{margin:6px 0 0;font-size:22px}}
main{{max-width:980px;margin:auto;padding:24px 16px 120px}}
section{{background:#fff;border-radius:8px;padding:18px 22px;margin:0 0 18px;box-shadow:0 1px 6px #0001}}
h2{{font-size:16px;margin:0 0 4px}} h2:after{{content:"";display:block;width:56px;height:3px;background:var(--secondary);
margin-top:6px}}
.row{{display:grid;grid-template-columns:1.5fr 2fr .9fr;gap:14px;align-items:center;padding:10px 0;
border-bottom:1px solid #eee}} .row:last-child{{border-bottom:0}}
.lbl b{{display:block;font-size:14px}} .lbl span{{font-size:12px;color:var(--granite)}}
input[type=range]{{width:100%;accent-color:var(--royal)}} .val{{display:flex;gap:6px;align-items:center}}
.val input{{width:80px;padding:4px 6px;border:1px solid var(--silver);border-radius:4px;font-size:14px}}
select{{padding:6px;border:1px solid var(--silver);border-radius:4px;font-size:14px;width:100%}}
.def{{font-size:11px;color:var(--granite)}} .mod{{background:#FEF4D5}}
.reset{{background:none;border:0;color:var(--royal);cursor:pointer;font-size:12px;padding:0}}
footer{{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:3px solid var(--secondary);padding:14px 32px;
display:flex;gap:16px;align-items:center;justify-content:space-between}}
button.save{{background:var(--primary);color:#fff;border:0;border-radius:4px;padding:10px 22px;font-size:15px;cursor:pointer}}
.note{{font-size:12px;color:var(--granite);max-width:640px}} #msg{{font-weight:700}}
</style></head><body>
<header><small>BIMDATA — RÉGLAGES DES AUDITS</small><h1>{title}</h1></header>
<main><p class="note">Valeurs <b>indicatives</b>, à valider par un professionnel qualifié au regard du programme, du
classement et de la catégorie de l'ouvrage. Les lignes surlignées diffèrent de la valeur par défaut. Les réglages
enregistrés s'appliquent au prochain audit de cette maquette et figurent au chapitre « Hypothèses » du rapport.</p>
{sections}</main>
<footer><span class="note">Aide à la décision produite avec assistance d'IA — ne remplace pas un bureau d'études.</span>
<span id="msg"></span><button class="save" onclick="save()">Enregistrer les réglages</button></footer>
<script>
function sync(k,v){{document.querySelectorAll('[data-k="'+k+'"]').forEach(e=>{{if(e.value!=v)e.value=v}});
 const r=document.getElementById('row-'+k);r.classList.toggle('mod',String(v)!==r.dataset.def)}}
function reset(k){{const r=document.getElementById('row-'+k);sync(k,r.dataset.def)}}
async function save(){{const vals={{}};document.querySelectorAll('[data-k]').forEach(e=>{{if(e.type!=='range')
 vals[e.dataset.k]=e.value===''?null:(e.tagName==='SELECT'?e.value:parseFloat(e.value))}});
 const r=await fetch(location.href,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(vals)}});
 const j=await r.json();document.getElementById('msg').textContent=r.ok?'Réglages enregistrés ✓':('Erreur : '+j.error);}}
</script></body></html>"""


def page(owner: str, target_key: str, audits: list[str] | None = None, title: str = "Paramètres de calcul") -> str:
    sections = []
    for block in parameters(owner, target_key, audits):
        rows = []
        for p in block["parametres"]:
            k = html.escape(p["key"])
            val = p["valeur"]
            dflt = "" if p["default"] is None else str(p["default"])
            cls = " mod" if p["modifie"] else ""
            if p["choices"]:
                opts = "".join(f"<option value='{html.escape(c)}'{' selected' if c == val else ''}>{html.escape(c)}</option>"
                               for c in p["choices"])
                ctrl = (f"<select data-k='{k}' onchange=\"sync('{k}',this.value)\">"
                        f"<option value=''{' selected' if val is None else ''}>— à choisir —</option>{opts}</select>")
            else:
                v = "" if val is None else val
                ctrl = (f"<input type='range' data-k='{k}' min='{p['min']}' max='{p['max']}' step='{p['step']}' "
                        f"value='{v}' oninput=\"sync('{k}',this.value)\">")
            unit = html.escape(p["unit"] or "")
            num = "" if p["choices"] else (
                f"<div class='val'><input type='number' data-k='{k}' min='{p['min']}' max='{p['max']}' "
                f"step='{p['step']}' value='{'' if val is None else val}' oninput=\"sync('{k}',this.value)\"> {unit}</div>")
            rows.append(
                f"<div class='row{cls}' id='row-{k}' data-def='{html.escape(dflt)}'><div class='lbl'><b>"
                f"{html.escape(p['label'])}</b><span>{html.escape(p['help'])}</span><br><span>Source : "
                f"{html.escape(p['source'] or '—')}</span></div><div>{ctrl}<div class='def'>Défaut : "
                f"{html.escape(dflt or 'à fournir')} {unit} — plage "
                f"{html.escape(str(p['min']) + ' – ' + str(p['max']) if p['min'] is not None else 'choix')}</div></div>"
                f"<div>{num}<button class='reset' onclick=\"reset('{k}')\">Revenir au défaut</button></div></div>")
        sections.append(f"<section><h2>{html.escape(AUDIT_LABELS.get(block['audit'], block['audit']))}</h2>"
                        + "".join(rows) + "</section>")
    return _PAGE.format(title=html.escape(title), sections="".join(sections))
