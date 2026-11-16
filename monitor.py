"""Monitor de preços: coleta no Mercado Livre (+ URLs extras), guarda histórico
e serve uma página local em http://localhost:PORTA"""
import json, os, re, sqlite3, threading, time, urllib.parse, html, sys
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
DB = BASE / "precos.db"
SITE = BASE / "docs"
SITE.mkdir(exist_ok=True)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}
STATUS = {}  # produto -> mensagem da última coleta


def norm(s):
    return re.sub(r"[\s\-_.,]", "", s.lower())


def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS precos(
        produto TEXT, ts TEXT, loja TEXT, titulo TEXT, preco REAL, url TEXT)""")
    return con


def ok(item, p):
    t = norm(item["titulo"])
    if not all(norm(w) in t for w in p.get("deve_conter", [])):
        return False
    if any(norm(w) in t for w in p.get("nao_conter", [])):
        return False
    return p.get("preco_min", 0) <= item["preco"] <= p.get("preco_max", 1e9)


def buscar_ml(p):
    slug = urllib.parse.quote(p["busca"].strip().replace(" ", "-"))
    r = requests.get(f"https://lista.mercadolivre.com.br/{slug}", headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    vistos, itens = set(), []
    for c in soup.select("li.ui-search-layout__item, div.poly-card"):
        t = c.select_one(".poly-component__title, .ui-search-item__title")
        if not t:
            continue
        a = t if t.name == "a" else (t.find("a") or t.find_parent("a"))
        if not a or not a.get("href"):
            continue
        frac = c.select_one(".poly-price__current .andes-money-amount__fraction") \
            or c.select_one(".andes-money-amount__fraction")
        if not frac:
            continue
        cents = frac.find_next(class_="andes-money-amount__cents")
        preco = int(re.sub(r"\D", "", frac.get_text()) or 0)
        if cents and cents.parent is frac.parent:
            preco += int(re.sub(r"\D", "", cents.get_text()) or 0) / 100
        url = a["href"].split("#")[0]
        if url in vistos:
            continue
        vistos.add(url)
        itens.append({"loja": "Mercado Livre", "titulo": t.get_text(" ", strip=True),
                      "preco": float(preco), "url": url})
    return itens


def buscar_url(url):
    """Página de produto de qualquer loja: tenta JSON-LD, depois meta tags."""
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    titulo = (soup.title.get_text(strip=True) if soup.title else url)[:120]
    preco = None
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack and preco is None:
            d = stack.pop()
            if isinstance(d, dict):
                offers = d.get("offers")
                for o in (offers if isinstance(offers, list) else [offers]):
                    if isinstance(o, dict):
                        v = o.get("price") or o.get("lowPrice")
                        if v:
                            preco = float(str(v).replace(",", "."))
                            break
                stack += [v for v in d.values() if isinstance(v, (dict, list))]
            elif isinstance(d, list):
                stack += d
    if preco is None:
        m = soup.select_one('meta[property="product:price:amount"], meta[property="og:price:amount"], '
                            'meta[itemprop="price"]')
        if m and m.get("content"):
            preco = float(m["content"].replace(",", "."))
    if preco is None:
        raise ValueError("preço não encontrado na página")
    loja = urllib.parse.urlparse(url).netloc.replace("www.", "")
    return {"loja": loja, "titulo": titulo, "preco": preco, "url": url}


def telegram(cfg, msg):
    tg = dict(cfg.get("telegram", {}))
    tg["token"] = os.environ.get("TELEGRAM_TOKEN") or tg.get("token")
    tg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id")
    if tg.get("token") and tg.get("chat_id"):
        try:
            requests.post(f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                          data={"chat_id": tg["chat_id"], "text": msg}, timeout=15)
        except Exception as e:
            print("Telegram falhou:", e)


def coletar(cfg):
    con = db()
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    for p in cfg["produtos"]:
        nome = p["nome"]
        candidatos, erros = [], []
        try:
            candidatos += [i for i in buscar_ml(p) if ok(i, p)]
        except Exception as e:
            erros.append(f"ML: {e}")
        for u in p.get("urls_extras", []):
            try:
                candidatos.append(buscar_url(u))
            except Exception as e:
                erros.append(f"{u[:40]}...: {e}")
        # melhor oferta por loja
        melhores = {}
        for i in candidatos:
            if i["loja"] not in melhores or i["preco"] < melhores[i["loja"]]["preco"]:
                melhores[i["loja"]] = i
        ant = con.execute("SELECT MIN(preco) FROM precos WHERE produto=? AND ts=(SELECT MAX(ts) "
                          "FROM precos WHERE produto=?)", (nome, nome)).fetchone()[0]
        for i in melhores.values():
            con.execute("INSERT INTO precos VALUES(?,?,?,?,?,?)",
                        (nome, ts, i["loja"], i["titulo"], i["preco"], i["url"]))
        con.commit()
        STATUS[nome] = (f"{len(candidatos)} anúncios válidos" if candidatos else "nenhum anúncio válido") \
            + (" | erros: " + "; ".join(erros) if erros else "")
        print(f"[{ts}] {nome}: {STATUS[nome]}")
        if melhores:
            b = min(melhores.values(), key=lambda x: x["preco"])
            alvo = p.get("preco_alvo")
            if alvo and b["preco"] <= alvo and (ant is None or b["preco"] < ant):
                telegram(cfg, f"💸 {nome}: R$ {b['preco']:.2f} ({b['loja']})\n{b['titulo']}\n{b['url']}")
    con.close()


def exportar_json(cfg):
    """Gera docs/dados.json com todo o histórico; a página (docs/index.html) só lê esse arquivo."""
    con = db()
    out = {"atualizado": datetime.now().astimezone().isoformat(timespec="seconds"), "produtos": []}
    for p in cfg["produtos"]:
        n = p["nome"]
        rows = con.execute("SELECT ts,loja,titulo,preco,url FROM precos WHERE produto=? ORDER BY ts", (n,)).fetchall()
        melhor, lojas = {}, {}
        for ts, loja, _, pr, _ in rows:
            melhor[ts] = min(pr, melhor.get(ts, pr))
            lojas.setdefault(loja, []).append([ts, pr])
        last = max(melhor) if melhor else None
        ofertas = sorted(({"loja": l, "titulo": t, "preco": pr, "url": u}
                          for ts, l, t, pr, u in rows if ts == last), key=lambda o: o["preco"])
        out["produtos"].append({"nome": n, "alvo": p.get("preco_alvo"), "status": STATUS.get(n, ""),
                                "ultima_coleta": last, "melhor": [[k, v] for k, v in melhor.items()],
                                "lojas": lojas, "ofertas": ofertas})
    con.close()
    (SITE / "dados.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def main():
    once = "--once" in sys.argv
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    exportar_json(cfg)
    if not once:
        srv = ThreadingHTTPServer((cfg.get("host", "127.0.0.1"), cfg.get("porta", 8000)),
                                  partial(SimpleHTTPRequestHandler, directory=str(SITE)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Página em http://localhost:{cfg.get('porta', 8000)}")
    while True:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))  # recarrega a cada ciclo
        coletar(cfg)
        exportar_json(cfg)
        if once:
            break
        time.sleep(cfg.get("intervalo_minutos", 180) * 60)


if __name__ == "__main__":
    main()
