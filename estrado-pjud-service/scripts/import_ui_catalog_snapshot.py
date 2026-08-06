"""Import a reviewed, secret-free UI catalog capture into the runtime snapshot."""
import argparse
import json
from pathlib import Path

HISTORICAL_APPEALS_AT = "2026-03-12T00:23:37+00:00"
APPEALS = [
 ("28","Civil"),("29","Familia"),("30","Laboral - Cobranza"),("31","Penal"),("32","Contencioso Administrativo"),("33","Tributario y Aduanero"),("34","Protección"),("35","Amparo"),("36","Policía Local"),("37","Exhorto"),("38","Ley de Navegación"),("39","Ambiental"),("40","Traspaso Corte Marcial"),("41","Ministro 1ª Instancia y Fuero"),("42","Com. Lib. Cond.")]
COMPS=("apelaciones","civil","laboral","penal","cobranza")
YEARS=range(2022,2027)
def rec(options, at): return {"fetched_at":at,"options":options}
def clean(options):
 out=[]; seen=set()
 for x in options:
  code=str(x.get("code","")).strip(); label=" ".join(str(x.get("label","")).split())
  if code and code not in {"0","-1"} and label and not label.lower().startswith("seleccione") and code not in seen:
   seen.add(code); out.append({"code":code,"label":label})
 return out
def main(src,out):
 d=json.loads(src.read_text(encoding="utf-8")); at=d["captured_at"]
 if len(clean(d["courts"]))!=17 or set(d["competencias"])!=set(COMPS): raise ValueError("invalid UI capture coverage")
 s={"generated_at":at,"courts":{"1":rec(clean(d["courts"]),at)},"tribunals":{},"books":{}}
 for comp in COMPS:
  for court in clean(d["courts"]):
   entry=d["competencias"][comp][court["code"]]; ts=HISTORICAL_APPEALS_AT if comp=="apelaciones" else at
   s["tribunals"][f"{comp}:{court['code']}:1"]=rec(clean(entry["tribunals"]),at)
   opts=[{"code":c,"label":l} for c,l in APPEALS] if comp=="apelaciones" else clean(entry["books"])
   for year in YEARS: s["books"][f"{comp}:{court['code']}:{year}"]=rec(opts,ts)
  opts=[{"code":c,"label":l} for c,l in APPEALS] if comp=="apelaciones" else clean(next(iter(d["competencias"][comp].values()))["books"])
  ts=HISTORICAL_APPEALS_AT if comp=="apelaciones" else at
  for year in YEARS: s["books"][f"{comp}::{year}"]=rec(opts,ts)
 if len(s["tribunals"])!=85 or len(s["books"])!=450 or any(not x["options"] for x in s["tribunals"].values()): raise ValueError("incomplete snapshot")
 out.write_text(json.dumps(s,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
if __name__=="__main__":
 p=argparse.ArgumentParser();p.add_argument("source",type=Path);p.add_argument("output",type=Path);a=p.parse_args();main(a.source,a.output)
