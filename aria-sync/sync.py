#!/usr/bin/env python3
"""Cérebro da Aria: sincroniza Supabase <-> Hermes (~/.hermes). Sem dependências.
  pull   baixa skills e memória, monta contexto.md e reserva os pedidos novos (pedidos.json)
  push   sobe skills/memórias alteradas e as entregas em saida/<id>.md
Só este script vê a chave de serviço; o modelo nunca a recebe."""
import hashlib, json, os, pathlib, sys, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("SUPABASE_URL", "").rstrip("/") + "/rest/v1"
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
HOME = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
OUT = pathlib.Path(os.environ.get("ARIA_SAIDA", "saida"))
MAN = HOME / ".sync.json"
LIMITE = 24000  # caracteres do contexto fixo (economiza tokens)


def api(metodo, caminho, corpo=None, prefer=None):
    cab = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    if prefer:
        cab["Prefer"] = prefer
    req = urllib.request.Request(BASE + caminho, method=metodo, headers=cab,
                                 data=None if corpo is None else json.dumps(corpo).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read().decode()
    return json.loads(t) if t else None


def h(t):
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def seguro(rel):
    p = pathlib.PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not rel:
        raise ValueError("caminho inválido: " + rel)
    return rel


def descricao(md):
    for l in md.splitlines()[:12]:
        if l.lower().startswith("description:"):
            return l.split(":", 1)[1].strip()[:300]
    return ""


def pull():
    skills = api("GET", "/skills?select=nome,descricao,conteudo&order=nome")
    mem = api("GET", "/memoria?select=caminho,conteudo,fixa&order=caminho")
    man = {}
    for s in skills:
        p = HOME / "skills" / seguro(s["nome"]) / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(s["conteudo"], encoding="utf-8")
        man["skills/" + s["nome"]] = h(s["conteudo"])
    for m in mem:
        p = HOME / "memories" / seguro(m["caminho"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(m["conteudo"], encoding="utf-8")
        man["memories/" + m["caminho"]] = h(m["conteudo"])
    HOME.mkdir(parents=True, exist_ok=True)
    MAN.write_text(json.dumps(man))
    partes = ["# Sua memória oficial (vem do Supabase; vale mais que qualquer lembrança sua)\n"]
    for m in mem:
        if m.get("fixa"):
            partes.append(f"## {m['caminho']}\n{m['conteudo']}\n")
    resto = [m["caminho"] for m in mem if not m.get("fixa")]
    if resto:
        partes.append("## Outras memórias (leia em ~/.hermes/memories/ só se precisar)\n" + "\n".join("- " + c for c in resto))
    partes.append("## Skills (em ~/.hermes/skills/<nome>/SKILL.md)\n" + "\n".join(f"- {s['nome']}: {s.get('descricao') or ''}" for s in skills))
    ctx = "\n".join(partes)
    if len(ctx) > LIMITE:
        ctx = ctx[:LIMITE] + "\n[contexto cortado: mova memórias longas para não fixas]"
    pathlib.Path("contexto.md").write_text(ctx, encoding="utf-8")
    novos = [] if os.environ.get("ARIA_SO_MEMORIA") else api("GET", "/pedidos?status=eq.novo&select=id,texto,origem,nivel,time,modelo,decisao&order=criado&limit=5") or []
    reservados = []
    for p in novos:  # reserva já na leitura, para duas execuções não pegarem o mesmo pedido
        ok = api("PATCH", "/pedidos?id=eq.%s&status=eq.novo" % urllib.parse.quote(p["id"]),
                 {"status": "em_andamento"}, prefer="return=representation")
        if ok:
            reservados.append(p)
    pathlib.Path("pedidos.json").write_text(json.dumps(reservados, ensure_ascii=False))
    print(f"pull: {len(skills)} skills, {len(mem)} memórias, {len(reservados)} pedidos")


def push():
    man = json.loads(MAN.read_text()) if MAN.exists() else {}
    n = 0
    for f in sorted((HOME / "skills").glob("*/SKILL.md")):
        t = f.read_text(encoding="utf-8")
        if man.get("skills/" + f.parent.name) != h(t):
            api("POST", "/skills?on_conflict=nome", {"nome": f.parent.name, "descricao": descricao(t), "conteudo": t, "origem": "hermes"},
                prefer="resolution=merge-duplicates")
            n += 1
    base = HOME / "memories"
    for f in sorted(base.rglob("*.md")) if base.exists() else []:
        rel = f.relative_to(base).as_posix()
        t = f.read_text(encoding="utf-8")
        if man.get("memories/" + rel) != h(t):
            api("POST", "/memoria?on_conflict=caminho", {"caminho": rel, "conteudo": t, "origem": "hermes"},
                prefer="resolution=merge-duplicates")
            n += 1
    ped = json.loads(pathlib.Path("pedidos.json").read_text()) if pathlib.Path("pedidos.json").exists() else []
    agora = datetime.now(timezone.utc).isoformat()
    for p in ped:
        arq = OUT / (p["id"] + ".md")
        if arq.exists() and arq.read_text(encoding="utf-8").strip():
            t = arq.read_text(encoding="utf-8")
            st = "aguardando_aprovacao" if t.lstrip().upper().startswith("APROVACAO") else "concluido"
            api("PATCH", "/pedidos?id=eq.%s" % urllib.parse.quote(p["id"]), {"resultado": t, "status": st, "concluido": agora})
        else:
            api("PATCH", "/pedidos?id=eq.%s" % urllib.parse.quote(p["id"]), {"status": "erro", "resultado": "Hermes não gerou entrega.", "concluido": agora})
    print(f"push: {n} arquivos de memória/skill, {len(ped)} pedidos fechados")


if __name__ == "__main__":
    if not BASE.startswith("http") or not KEY:
        sys.exit("Faltam SUPABASE_URL e SUPABASE_SERVICE_KEY.")
    {"pull": pull, "push": push}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
