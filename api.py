#!/usr/bin/env python3
"""
Fila de UCs vinda da API do Minha Fatura.

Antes as contas vinham de contas.json, escrito a mao. Agora a lista de quem
coletar e servida por https://minha-fatura.vercel.app/api/v1/queues, que
devolve UC, CPF/CNPJ e data de nascimento do titular - exatamente os tres
campos que o login precisa.

O descompasso que este modulo resolve
-------------------------------------
A API reserva cada tarefa por 30 MINUTOS: se o robo nao concluir nesse prazo, a
tarefa volta para a fila. Mas a estrategia do projeto e o oposto de rapida - uma
tentativa por UC por dia, o mes inteiro para fechar o ciclo. Nenhuma reserva de
30 min sobrevive a isso, e correr para caber nela seria voltar a metralhar, que
foi o que causou o bloqueio.

Entao a reserva e ignorada de proposito. O que a API da e a LISTA (uc, doc,
nasc); quem manda no ritmo continua sendo a agenda local. As tarefas nao
concluidas voltam para a fila e reaparecem na proxima busca - e por isso a fila
e acumulada em estado/fila_api.json, deduplicada por UC: reaparecer nao cria
conta nova, so atualiza a que ja existe.

Como o cache tambem e a fonte de verdade offline, um deploy fora do ar ou uma
chave revogada nao param o ciclo em andamento: seguimos com o que ja foi visto.

Ajustes (variaveis de ambiente ou credenciais.env):
    MF_API_KEY          chave X-API-Key (sem ela a fonte volta a ser contas.json)
    MF_API_URL          base da API (default: o deploy de producao)
    MF_API_LIMIT        tarefas por busca, 1-50 (default 50)
    MF_API_GRUPO        filtra por grupo (ex.: Sicoob)
    MF_API_GRUPO_BUSCA  filtra por grupo de leitura, 1-3
    MF_FONTE            auto (default) | api | json

Uso direto, para conferir a integracao sem gastar tentativa:
    python api.py           # busca e mostra o que a API esta servindo
    python api.py --cache   # so mostra o que ja esta em estado/fila_api.json
"""

import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import personas as P

RAIZ = Path(__file__).parent
CACHE = RAIZ / "estado" / "fila_api.json"

BASE = os.environ.get("MF_API_URL", "https://minha-fatura.vercel.app/api/v1").rstrip("/")
CHAVE = os.environ.get("MF_API_KEY", "").strip()
LIMITE = max(1, min(50, int(os.environ.get("MF_API_LIMIT", "50") or 50)))
GRUPO = os.environ.get("MF_API_GRUPO", "").strip()
GRUPO_BUSCA = os.environ.get("MF_API_GRUPO_BUSCA", "").strip()
FONTE = os.environ.get("MF_FONTE", "auto").strip().lower()

TIMEOUT = int(os.environ.get("MF_API_TIMEOUT", "30"))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] api: {msg}", flush=True)


def ativa():
    """A API e a fonte das contas nesta execucao?"""
    if FONTE == "json":
        return False
    if FONTE == "api":
        return True
    return bool(CHAVE)


# ------------------------------------------------------------------ requisicao

class ErroAPI(RuntimeError):
    pass


def _get(caminho, params=None):
    if not CHAVE:
        raise ErroAPI("MF_API_KEY nao definida - defina em credenciais.env")
    url = f"{BASE}/{caminho.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-API-Key": CHAVE,
        "Accept": "application/json",
        # A API identifica o robo pela chave, mas um UA proprio ajuda a separar
        # nosso trafego do de um navegador nos logs dela.
        "User-Agent": "robozin/1.0 (coleta Equatorial GO)",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        corpo = ""
        try:
            corpo = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code in (401, 403):
            raise ErroAPI(f"chave recusada ({e.code}) - confira MF_API_KEY. {corpo}")
        raise ErroAPI(f"HTTP {e.code} em {caminho}. {corpo}")
    except urllib.error.URLError as e:
        raise ErroAPI(f"rede: {e.reason}")


def buscar(limit=None, grupo=None, grupo_busca=None):
    """GET /queues. Devolve (itens, reservation_expires_at)."""
    params = {"limit": limit or LIMITE}
    g = grupo if grupo is not None else GRUPO
    gb = grupo_busca if grupo_busca is not None else GRUPO_BUSCA
    if g:
        params["grupo"] = g
    if gb:
        params["grupo_busca"] = gb
    dados = _get("queues", params)
    itens = dados.get("items") or []
    return itens, dados.get("reservation_expires_at")


# -------------------------------------------------------------- conversao

def _data_br(iso):
    """AAAA-MM-DD -> DD/MM/AAAA, que e o formato que o site pede na 2a etapa."""
    s = (iso or "").strip()[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        a, m, d = s.split("-")
        return f"{d}/{m}/{a}"
    # Ja veio em DD/MM/AAAA? Devolve como esta; qualquer outra coisa vira vazio,
    # e a conta segue sem nascimento (so faz falta se o site pedir a 2a etapa).
    return s if len(s) == 10 and s[2] == "/" else ""


def _apelido(item, uc):
    """Nome curto so para o log. Estavel, porque sai da razao social.

    Sem acento de proposito: o console do Windows costuma estar em cp1252 e
    "antônio" sairia como "ant?nio" em todo log do robo.
    """
    razao = (item.get("razao_social") or "").strip()
    if razao:
        sem_acento = unicodedata.normalize("NFKD", razao.split()[0])
        limpo = "".join(ch for ch in sem_acento if ch.isalnum() and ch.isascii())
        if limpo:
            return limpo[:10].lower()
    return uc[-6:]


def converter(item):
    """Item da API -> conta no formato que o resto do robo ja consome.

    Devolve None para item que o login nao teria como usar (sem UC, ou documento
    que nao e CPF nem CNPJ). Descartar um item e melhor do que derrubar o lote:
    a fila e de terceiros e nao ha o que corrigir daqui.
    """
    uc = P.so_digitos(item.get("uc"))
    doc = P.so_digitos(item.get("cpf_cnpj"))
    if not uc or len(doc) not in (11, 14):
        return None
    return {
        "uc": uc,
        "doc": doc,
        "nasc": _data_br(item.get("data_nascimento")),
        "apelido": _apelido(item, uc),
        # Identificadores da API. Nao sao usados na coleta - ficam guardados
        # para quando existir o endpoint de relatar resultado/enviar a fatura.
        "task_id": item.get("task_id"),
        "queue_id": item.get("id"),
        "uc_id": item.get("uc_id"),
        "grupo": item.get("grupo"),
        "razao_social": item.get("razao_social"),
        "reference_month": item.get("reference_month"),
    }


# ------------------------------------------------------------------- cache

def ler_cache():
    if not CACHE.exists():
        return {"atualizado_em": 0, "contas": {}}
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        d.setdefault("contas", {})
        return d
    except Exception:
        log(f"{CACHE.name} ilegivel; recomecando a fila do zero")
        return {"atualizado_em": 0, "contas": {}}


def gravar_cache(d):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def mesclar(itens, cache=None):
    """Junta os itens buscados ao cache. Dedupe por UC.

    Uma UC que reaparece (reserva vencida, nova tentativa do lado deles) atualiza
    os dados cadastrais mas nao vira conta nova - a agenda local ja sabe o que
    fez com ela.
    """
    cache = cache or ler_cache()
    contas = cache.setdefault("contas", {})
    novos = descartados = 0
    for item in itens:
        c = converter(item)
        if not c:
            descartados += 1
            continue
        antiga = contas.get(c["uc"])
        c["visto_em"] = time.time()
        if antiga:
            c["primeiro_visto_em"] = antiga.get("primeiro_visto_em", c["visto_em"])
        else:
            c["primeiro_visto_em"] = c["visto_em"]
            novos += 1
        contas[c["uc"]] = c
    cache["atualizado_em"] = time.time()
    gravar_cache(cache)
    return novos, descartados


def sincronizar(limit=None):
    """Busca na API e atualiza o cache. Erro de rede nao e fatal.

    Devolve True se a busca aconteceu. O ciclo em andamento nao pode depender do
    deploy estar de pe: sem rede seguimos com a fila que ja esta em disco.
    """
    try:
        itens, expira = buscar(limit=limit)
    except ErroAPI as e:
        log(f"busca falhou ({e}); seguindo com a fila em cache")
        return False
    novos, descartados = mesclar(itens)
    total = len(ler_cache()["contas"])
    log(f"{len(itens)} tarefa(s) recebida(s), {novos} nova(s), "
        f"{descartados} descartada(s); fila local com {total} UC(s)"
        + (f" | reserva ate {expira}" if expira else ""))
    return True


def contas(atualizar=True, limit=None):
    """Lista de contas no formato de personas.carregar_contas()."""
    if atualizar:
        sincronizar(limit=limit)
    cache = ler_cache()
    return sorted(cache["contas"].values(), key=lambda c: c["uc"])


# --------------------------------------------------------------------- CLI

def _mostrar(lista, titulo):
    print(f"{titulo}: {len(lista)} conta(s)")
    for c in lista:
        doc = c["doc"]
        mascara = f"{doc[:3]}***{doc[-2:]}"
        print(f"  {c['apelido']:<10} uc={c['uc']:<16} doc={mascara:<9} "
              f"nasc={c['nasc'] or '-':<10} grupo={c.get('grupo') or '-':<10} "
              f"ref={c.get('reference_month') or '-'}")


if __name__ == "__main__":
    if "--cache" in sys.argv[1:]:
        _mostrar(contas(atualizar=False), f"Fila em {CACHE.name}")
        sys.exit(0)
    if not CHAVE:
        sys.exit("MF_API_KEY nao definida. Coloque a chave em credenciais.env.")
    print(f"Base: {BASE}  limite={LIMITE}"
          + (f"  grupo={GRUPO}" if GRUPO else "")
          + (f"  grupo_busca={GRUPO_BUSCA}" if GRUPO_BUSCA else ""))
    try:
        itens, expira = buscar()
    except ErroAPI as e:
        sys.exit(f"Falhou: {e}")
    print(f"Resposta: {len(itens)} item(ns)"
          + (f", reserva ate {expira}" if expira else ""))
    novos, descartados = mesclar(itens)
    # A diferenca entre itens recebidos e contas novas costuma ser UC repetida
    # (duas tarefas para a mesma UC, ou reserva vencida voltando): dizer isso
    # aqui evita procurar bug onde so houve dedupe.
    print(f"  {novos} nova(s), {len(itens) - novos - descartados} ja conhecida(s), "
          f"{descartados} descartada(s) por dado invalido")
    _mostrar(contas(atualizar=False), "Fila acumulada")
