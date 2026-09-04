#!/usr/bin/env python3
"""Baixa a fatura mais recente de varias UCs (contas diferentes) numa tacada.

Cada UC aqui pertence a um CPF/CNPJ distinto, entao sao logins separados: antes
de cada uma limpamos os cookies do dominio para derrubar a sessao ASP.NET da
conta anterior e forcar um login novo. O login em si (login.py) ja aceita CPF
(11 digitos) e CNPJ (14) - formatar_documento cuida da mascara dos dois.

Uso:
    .\\chrome.ps1
    python coletar_varias.py
"""
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import login
from login import (
    URL_FATURAS, area_logada, conectar, escutar_dialogos, garantir_login,
    ir_para, liberar_rede, log, preparar_rede,
)
import baixar_fatura as bf

# (UC, documento) - so digitos entram; o resto e ignorado
CONTAS = [
    ("000408683001277", "311.091.791-20"),
    ("000176588701258", "213.001.191-87"),
    ("000422143201201", "24.830.879/0001-67"),
    ("000090076001290", "760.823.001-06"),
    ("000012631801243", "33.638.743/0001-40"),
]

PRAZO_POR_UC = int(__import__("os").environ.get("EQUATORIAL_PRAZO", "600"))


def so_digitos(s):
    return "".join(filter(str.isdigit, s))


def coletar_uc(page, ctx, dialogos, uc, doc):
    # Reaponta as credenciais que login.py usa (modulo-level) para esta conta.
    login.UC = so_digitos(uc)
    login.DOC = so_digitos(doc)
    tipo = "CNPJ" if len(login.DOC) == 14 else "CPF"
    log(f"### UC {uc} | {tipo} {doc} ###")

    # Derruba a sessao da conta anterior: sem isso, garantir_login veria a
    # sessao ainda valida e consultaria a UC errada.
    try:
        ctx.clear_cookies(domain=login.DOMINIO)
    except TypeError:
        ctx.clear_cookies()

    if not garantir_login(page, dialogos, ctx=ctx, prazo_s=PRAZO_POR_UC):
        log(f"UC {uc}: nao consegui logar")
        return None

    ir_para(page, URL_FATURAS)
    page.wait_for_timeout(2000)
    try:
        ano = bf.consultar(page)
    except PWTimeout as e:
        log(f"UC {uc}: timeout na consulta ({str(e)[:70]})")
        return None
    if not ano:
        log(f"UC {uc}: nenhuma fatura encontrada")
        return None

    caminho = bf.baixar_mais_recente(page, ctx, ano)
    if not (caminho and caminho.exists() and caminho.stat().st_size > 1000):
        log(f"UC {uc}: consulta OK mas o download nao veio")
        return None

    # Prefixa com a UC para faturas de meses iguais em contas diferentes nao
    # se sobrescreverem.
    destino = caminho.with_name(f"fatura-{uc}-{caminho.name.replace('fatura-', '')}")
    if destino != caminho:
        if destino.exists():
            destino.unlink()
        caminho.rename(destino)
    log(f"UC {uc}: PRONTO -> {destino.name} ({destino.stat().st_size} bytes)")
    return destino


def main():
    bf.FATURAS.mkdir(exist_ok=True)
    resultados = {}
    # Rota no celular so com EQUATORIAL_ROTACIONAR=1; o finally garante que a
    # maquina nao fique roteando pelo 4G depois. Assumimos a rota uma vez para
    # todas as UCs - trocar_ip() dentro do garantir_login roda a cada recusa.
    estado_rede = preparar_rede()
    try:
        with sync_playwright() as p:
            ctx, page = conectar(p)
            dialogos = escutar_dialogos(page)
            for uc, doc in CONTAS:
                try:
                    resultados[uc] = coletar_uc(page, ctx, dialogos, uc, doc)
                except Exception as e:
                    log(f"UC {uc}: erro inesperado ({type(e).__name__}: {str(e)[:90]})")
                    resultados[uc] = None
    finally:
        liberar_rede(estado_rede)

    log("===== RESUMO =====")
    ok = 0
    for uc, doc in CONTAS:
        r = resultados.get(uc)
        if r:
            ok += 1
            log(f"  OK   {uc} -> {r.name}")
        else:
            log(f"  FALHA {uc}")
    log(f"{ok}/{len(CONTAS)} faturas baixadas")
    return 0 if ok == len(CONTAS) else 1


if __name__ == "__main__":
    sys.exit(main())
