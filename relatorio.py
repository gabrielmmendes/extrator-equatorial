#!/usr/bin/env python3
"""
Relatorio de cobertura: quais UCs da fila tem a fatura do mes e quais nao.

Por que um arquivo em vez de olhar faturas/: o nome do PDF diz UC, mes e ano,
mas nao diz quem ficou de fora - para isso e preciso cruzar com a fila da API,
que muda a cada rodada (a API serve UCs novas). Depois de um lote a pergunta
nunca e "quantos PDFs baixei", e "de quem eu ainda nao tenho".

O status de cada UC sai de tres fontes, nesta ordem de confianca:

  faturas/*.pdf     verdade final - o PDF existe ou nao existe
  estado/agenda.json  motivo da ultima falha e falhas seguidas
  perfis/<doc>/     mtime do perfil Chrome = houve tentativa naquela sessao

A terceira e indireta e so serve para separar "tentou e falhou" de "nem chegou
a rodar" quando nao ha log do lote - o orquestrador com --mes nao escreve na
agenda (ver concluir()), entao sem isso as duas situacoes ficam identicas.

Uso:
    python relatorio.py                 # mes corrente da fila (ou EQUATORIAL_MES)
    python relatorio.py --mes 08/2026
    python relatorio.py --mes 08/2026 --csv saida/status.csv
    python relatorio.py --mes 08/2026 --desde "2026-09-06 07:00"  # janela do lote
"""

import csv
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import baixar_fatura as bf
import personas as P

AQUI = Path(__file__).resolve().parent
AGENDA = AQUI / "estado" / "agenda.json"
PERFIS = AQUI / "perfis"
PADRAO_PDF = re.compile(r"fatura-(\d+)-(\d+)-(\d+)\.pdf$")


def pdfs_por_uc():
    """Mapa uc -> [(ano, mes, nome, mtime)], lido do disco."""
    achados = {}
    for caminho in glob.glob(str(bf.FATURAS / "fatura-*.pdf")):
        m = PADRAO_PDF.search(os.path.basename(caminho))
        if not m:
            continue
        uc, mes, ano = m.group(1), int(m.group(2)), int(m.group(3))
        achados.setdefault(uc, []).append(
            (ano, mes, os.path.basename(caminho), os.path.getmtime(caminho)))
    for lista in achados.values():
        lista.sort()
    return achados


def agenda_por_uc():
    """O que a agenda sabe de cada UC. Vazio se o arquivo ainda nao existe."""
    try:
        return json.loads(AGENDA.read_text(encoding="utf-8"))["ucs"]
    except (OSError, ValueError, KeyError):
        return {}


def perfis_por_doc():
    """mtime do perfil Chrome de cada titular - marca de tentativa recente."""
    return {p.name: p.stat().st_mtime for p in PERFIS.glob("*") if p.is_dir()}


def quando(ts):
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else ""


def levantar(contas, mes, ano, desde=0):
    """Uma linha por conta da fila, ja com status e evidencias."""
    pdfs, ag, perfis = pdfs_por_uc(), agenda_por_uc(), perfis_por_doc()
    linhas = []
    for c in contas:
        uc = c["uc"]
        do_mes = [t for t in pdfs.get(uc, []) if t[0] == ano and t[1] == mes]
        a = ag.get(uc, {})
        tocou = perfis.get(c.get("doc"), 0)
        linhas.append({
            "uc": uc,
            "apelido": c.get("apelido", ""),
            "razao_social": c.get("razao_social", ""),
            "doc": c.get("doc", ""),
            "grupo": c.get("grupo", ""),
            "ref_month": c.get("reference_month", ""),
            "status": "OK" if do_mes else "FALTA",
            "arquivo": do_mes[0][2] if do_mes else "",
            "baixado_em": quando(do_mes[0][3]) if do_mes else "",
            "outros_meses": ",".join(f"{me:02d}/{an}"
                                     for an, me, _, _ in pdfs.get(uc, [])),
            "motivo_agenda": a.get("ultimo_motivo") or "",
            "falhas_seguidas": a.get("falhas_seguidas", 0),
            "tentou_na_janela": "sim" if desde and tocou > desde else "nao",
            "perfil_visto_em": quando(tocou),
            # A UC pode nao ter fatura no mes pedido: se a API diz que a
            # referencia dela e outra, cobrar 08/2026 e cobrar o que nao existe.
            "mes_nao_e_o_dela": "sim" if (
                c.get("reference_month") or "")[:7] != f"{ano}-{mes:02d}" else "",
        })
    linhas.sort(key=lambda l: (l["status"], l["grupo"], l["apelido"]))
    return linhas


def escrever_csv(linhas, destino):
    """CSV com ; e BOM: e o que o Excel em pt-BR abre sem pedir nada."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0]), delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    return destino


def imprimir(linhas, mes, ano, desde):
    ok = [l for l in linhas if l["status"] == "OK"]
    falta = [l for l in linhas if l["status"] == "FALTA"]
    total = len(linhas)
    print(f"=== Faturas de {mes:02d}/{ano} - {total} UC(s) na fila ===")
    print(f"  OK    {len(ok):>4}  ({100 * len(ok) / total:.1f}%)")
    print(f"  FALTA {len(falta):>4}")
    if desde:
        baixadas = [l for l in ok if l["baixado_em"] >= quando(desde)]
        print(f"  (na janela: {len(baixadas)} baixada(s), "
              f"{sum(1 for l in falta if l['tentou_na_janela'] == 'sim')} "
              f"tentativa(s) sem PDF)")
    if not falta:
        print("\nNada faltando.")
        return
    fora = [l for l in falta if l["mes_nao_e_o_dela"]]
    print(f"\n--- {len(falta)} sem PDF de {mes:02d}/{ano} ---")
    cab = f"{'uc':<16} {'apelido':<14} {'grupo':<10} {'ref':<8} " \
          f"{'motivo':<12} {'falhas':<6} tentou"
    print(cab)
    for l in falta:
        print(f"{l['uc']:<16} {l['apelido'][:14]:<14} {l['grupo'][:10]:<10} "
              f"{l['ref_month'][:7]:<8} {l['motivo_agenda'][:12]:<12} "
              f"{l['falhas_seguidas']:<6} {l['tentou_na_janela']}")
    print(f"\npor grupo: {dict(Counter(l['grupo'] for l in falta))}")
    if fora:
        print(f"{len(fora)} dessas tem referencia diferente de {mes:02d}/{ano} "
              f"na API - pode nao existir fatura para cobrar: "
              f"{', '.join(l['apelido'] for l in fora)}")
    duras = [l for l in falta if l["falhas_seguidas"] >= 3]
    if duras:
        print(f"{len(duras)} com 3+ falhas seguidas (olhar caso a caso): "
              f"{', '.join(l['uc'] for l in duras)}")


def extrair(args, chave):
    """Tira `--chave valor` de args e devolve o valor (ou None)."""
    if chave not in args:
        return None
    i = args.index(chave)
    if i + 1 >= len(args):
        sys.exit(f"{chave} exige um valor")
    valor = args[i + 1]
    del args[i:i + 2]
    return valor


def main():
    args = sys.argv[1:]
    csv_saida = extrair(args, "--csv")
    desde_txt = extrair(args, "--desde")
    mes_txt = extrair(args, "--mes")
    try:
        alvo = bf.parse_mes(mes_txt) if mes_txt else bf.mes_do_ambiente()
    except ValueError as e:
        sys.exit(str(e))
    if not alvo:
        hoje = time.localtime()
        alvo = (hoje.tm_mon, hoje.tm_year)
    mes, ano = alvo

    desde = 0
    if desde_txt:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m %H:%M"):
            try:
                desde = time.mktime(time.strptime(desde_txt, fmt))
                break
            except ValueError:
                continue
        if not desde:
            sys.exit("--desde espera 'AAAA-MM-DD HH:MM' ou 'AAAA-MM-DD'")

    # atualizar=False de proposito: sincronizar com a API reserva tarefas por
    # horas. Um relatorio rodado no meio de um lote roubaria a reserva de quem
    # esta coletando. Relatar o que a fila tem em disco basta.
    contas = P.carregar_contas(atualizar=False)
    if not contas:
        sys.exit("Fila vazia: nao ha o que relatar")
    linhas = levantar(contas, mes, ano, desde)
    imprimir(linhas, mes, ano, desde)
    if csv_saida:
        print(f"\nCSV: {escrever_csv(linhas, csv_saida)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
