#!/usr/bin/env python3
"""
Cliente da API de worker do gcenergy (rotas /worker/**).

O contrato, resumido do guia de integracao:

    POST  /worker/jobs/reservar?limite=N   200 [jobs] | 204 fila vazia
    GET   /worker/jobs/{id}                200 job    | 404
    PATCH /worker/jobs/{id}/status         200 | 400 | 404 | 409

Toda chamada leva X-Worker-Key (a chave do ambiente) e X-Worker-Id (o nome
desta instancia). O X-Worker-Id fica gravado no job na reserva e o PATCH so e
aceito com o MESMO valor - por isso ele e fixo por maquina, e nao sorteado: um
worker reiniciado continua sendo dono do job que reservou antes de cair.

Este modulo so fala HTTP e aplica as regras de repeticao do guia. Quem decide o
que fazer com o job e o worker.py.

Ajustes (variaveis de ambiente ou credenciais.env):
    GCENERGY_URL          base da API (default: producao)
    GCENERGY_WORKER_KEY   valor do X-Worker-Key (obrigatorio)
    GCENERGY_WORKER_ID    valor do X-Worker-Id (default: robozin-<hostname>)
    GCENERGY_TIMEOUT      timeout de cada chamada HTTP, em segundos (default 30)
"""

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request

BASE = os.environ.get("GCENERGY_URL", "https://api.synexsaas.com.br").rstrip("/")
CHAVE = os.environ.get("GCENERGY_WORKER_KEY", "").strip()
WORKER_ID = (os.environ.get("GCENERGY_WORKER_ID", "").strip()
             or f"robozin-{socket.gethostname().lower()}")
TIMEOUT = int(os.environ.get("GCENERGY_TIMEOUT", "30"))

# Espera entre repeticoes do PATCH em erro de rede ou 5xx. Somam ~2 min: mais
# que isso e melhor deixar o job vencer no servidor (60 min) do que segurar o
# laco - o PDF ja esta em disco e no Drive, entao refazer o job sai barato.
ESPERAS_PATCH = (5, 15, 30, 60)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] gcenergy: {msg}", flush=True)


class ErroAPI(RuntimeError):
    """Resposta que nao sabemos tratar. `status` e o HTTP (None = rede)."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class ErroRede(ErroAPI):
    """Nao houve resposta: DNS, conexao recusada, timeout."""


class ErroChave(ErroAPI):
    """401. Repetir nao resolve: e chave ausente, errada ou nao configurada."""


class ErroPayload(ErroAPI):
    """400. O corpo que mandamos e invalido - corrigir, nao repetir."""


def _json(bruto):
    if not bruto:
        return None
    try:
        return json.loads(bruto.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"message": bruto.decode("utf-8", "replace")[:300]}


def _mensagem(dados):
    if isinstance(dados, dict):
        return str(dados.get("message") or dados.get("status") or dados)[:300]
    return str(dados)[:300] if dados else ""


def _chamar(metodo, caminho, corpo=None):
    """Uma chamada HTTP. Devolve (status, json|None); so levanta ErroRede."""
    if not CHAVE:
        raise ErroChave("GCENERGY_WORKER_KEY nao definida - defina em credenciais.env")
    cabecalhos = {
        "X-Worker-Key": CHAVE,
        "X-Worker-Id": WORKER_ID,
        "Accept": "application/json",
        "User-Agent": "robozin-worker/1.0",
    }
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        cabecalhos["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{caminho}", data=dados, method=metodo,
                                 headers=cabecalhos)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json(e.read())
        except (OSError, http.client.HTTPException):
            return e.code, None
    except (OSError, http.client.HTTPException) as e:
        raise ErroRede(f"{metodo} {caminho}: {getattr(e, 'reason', e)}")


def _falhar(status, dados, onde):
    msg = f"{onde}: HTTP {status} {_mensagem(dados)}".strip()
    if status == 401:
        raise ErroChave(msg, status)
    if status == 400:
        raise ErroPayload(msg, status)
    raise ErroAPI(msg, status)


# ------------------------------------------------------------------- rotas

def reservar(limite=1):
    """Reserva ate `limite` jobs (1-10). Devolve a lista; vazia = fila vazia."""
    status, dados = _chamar("POST", f"/worker/jobs/reservar?limite={int(limite)}")
    if status == 204:
        return []
    if status == 200:
        return dados or []
    _falhar(status, dados, "reservar")


def consultar(job_id):
    """GET do job. Devolve o dict, ou None se ele nao existe."""
    status, dados = _chamar("GET", f"/worker/jobs/{job_id}")
    if status == 200:
        return dados
    if status == 404:
        return None
    _falhar(status, dados, f"consultar {job_id}")


def atualizar(job_id, corpo):
    """PATCH de status com as regras de repeticao do guia.

    Devolve "ok" (o servidor gravou o que mandamos) ou "descartado" (o job nao
    e mais deste worker: expirou, foi cancelado ou o robo de e-mail o concluiu -
    o resultado e jogado fora e o laco segue). Levanta ErroChave no 401,
    ErroPayload no 400 e ErroAPI se as repeticoes se esgotarem.

    409 nao e erro por si so. Pode ser gravacao simultanea (o job continua
    EM_EXECUCAO e nosso: repete) ou um PATCH anterior que chegou sem a resposta
    voltar (o status ja e o que mandamos: sucesso). So o GET do job diz qual.
    """
    caminho = f"/worker/jobs/{job_id}/status"
    incerto = False  # houve PATCH sem resposta: pode ter chegado ao servidor
    for n in range(len(ESPERAS_PATCH) + 1):
        espera = ESPERAS_PATCH[min(n, len(ESPERAS_PATCH) - 1)]
        ultima = n == len(ESPERAS_PATCH)
        try:
            status, dados = _chamar("PATCH", caminho, corpo)
        except ErroRede as e:
            incerto = True
            if ultima:
                raise
            log(f"job {job_id}: PATCH sem resposta ({e}); nova tentativa em {espera}s")
            time.sleep(espera)
            continue

        if status == 200:
            return "ok"
        if status in (400, 401):
            _falhar(status, dados, f"PATCH {job_id}")
        if status == 404:
            log(f"job {job_id}: nao existe mais; resultado descartado")
            return "descartado"
        if status == 409:
            try:
                atual = consultar(job_id)
            except ErroRede as e:
                if ultima:
                    raise
                log(f"job {job_id}: 409 e o GET falhou ({e}); nova tentativa em {espera}s")
                time.sleep(espera)
                continue
            if atual is None:
                return "descartado"
            if incerto and atual.get("status") == corpo.get("status"):
                log(f"job {job_id}: o PATCH anterior tinha chegado ({atual.get('status')})")
                return "ok"
            if atual.get("status") == "EM_EXECUCAO" and atual.get("workerId") == WORKER_ID:
                if ultima:
                    break
                log(f"job {job_id}: gravacao simultanea no servidor; repetindo o PATCH")
                time.sleep(2)
                continue
            log(f"job {job_id}: 409 - agora esta {atual.get('status')} "
                f"(worker {atual.get('workerId') or '-'}); resultado descartado")
            return "descartado"
        if status >= 500 and not ultima:
            log(f"job {job_id}: HTTP {status} {_mensagem(dados)}; nova tentativa em {espera}s")
            time.sleep(espera)
            continue
        _falhar(status, dados, f"PATCH {job_id}")
    raise ErroAPI(f"PATCH {job_id}: repeticoes esgotadas")


def checar_chave():
    """Confere chave e base sem reservar nada. Devolve a descricao do resultado.

    Reservar para testar tiraria um job da fila de verdade. Um GET num id que
    nao existe nao tira: 404 prova que a chave passou (401 viria antes).
    """
    status, dados = _chamar("GET", "/worker/jobs/0")
    if status in (200, 404):
        return f"chave aceita em {BASE} (X-Worker-Id {WORKER_ID})"
    _falhar(status, dados, "checar")
