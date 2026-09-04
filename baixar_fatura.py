#!/usr/bin/env python3
"""
Baixa a fatura mais recente da Agencia Virtual da Equatorial Goias.

Fluxo: garante login (login.py) -> Historico de Faturas -> consulta ano a ano,
do mais recente para tras -> baixa a fatura do topo.

Uso (Windows):
    .\\chrome.ps1
    .\\coletar.ps1
Uso (direto):
    python baixar_fatura.py
"""

import os
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from login import (
    ESPERA_BASE, ESPERA_MAX, PRAZO, URL_FATURAS,
    area_logada, conectar, escutar_dialogos, garantir_login, ir_para,
    liberar_rede, log, preparar_rede,
)

FATURAS = Path(__file__).parent / "faturas"
ANOS_ATRAS = int(os.environ.get("EQUATORIAL_ANOS_ATRAS", "3"))
SEL_CONSULTAR = ("#CONTENT_btEnviar, input[value*='Consultar' i], "
                 "button:has-text('Consultar')")
BASE = "https://goias.equatorialenergia.com.br"
# O botao OK do modal de protocolo. Confirmar aqui e o que de fato dispara o
# download do PDF - abrir a mostrarEspelhoFatura.aspx apenas exibe o modal
# "espelho gerado, protocolo N"; o binario so vem depois deste clique.
SEL_OK_MODAL = "#CONTENT_btnModal, input[value='OK' i]"


def dump_estrutura(page, rotulo):
    """Registra selects, botoes e links da pagina.

    O portal e ASP.NET WebForms: ids sao gerados e mudam de pagina para pagina,
    entao vale guardar o mapa de cada estado para ajustar os seletores.
    """
    dados = page.evaluate("""() => ({
        selects: [...document.querySelectorAll('select')].map(s => ({
            id: s.id, name: s.name, visivel: s.offsetParent !== null,
            valor: s.value,
            opcoes: [...s.options].slice(0, 25).map(o => ({v: o.value, t: o.text.trim()}))
        })),
        botoes: [...document.querySelectorAll('button, input[type=submit], input[type=button], a.button')]
            .filter(b => b.offsetParent !== null)
            .map(b => ({txt: (b.innerText || b.value || '').trim().slice(0, 40),
                        id: b.id, name: b.name,
                        onclick: (b.getAttribute('onclick') || '').slice(0, 120)})),
        tabelas: [...document.querySelectorAll('table')].map(t => ({
            id: t.id, linhas: t.rows.length,
            cabecalho: [...(t.rows[0] ? t.rows[0].cells : [])].map(c => c.innerText.trim()).slice(0, 10),
            amostra: [...t.rows].slice(1, 4).map(r =>
                [...r.cells].map(c => c.innerText.trim().slice(0, 30)))
        })),
        links: [...document.querySelectorAll('a')]
            .filter(a => a.offsetParent !== null)
            .map(a => ({txt: (a.innerText || '').trim().slice(0, 40),
                        href: (a.getAttribute('href') || '').slice(0, 120),
                        onclick: (a.getAttribute('onclick') || '').slice(0, 140)}))
            .filter(a => /fatura|segunda|pdf|boleto|download|visualiz|imprim|\\d{2}\\/\\d{4}/i
                         .test(a.txt + a.href + a.onclick)).slice(0, 25)
    })""")
    destino = FATURAS / f"estrutura-{rotulo}.json"
    destino.write_text(__import__("json").dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Estrutura ({rotulo}) salva em {destino.name}")
    return dados


def tem_resultados(page):
    """True se a consulta trouxe tabela com linhas de dados.

    A pagina de consulta nao tem nenhuma <table> no estado inicial (confirmado
    em estrutura-historico.json: "tabelas": []), entao qualquer tabela com mais
    de uma linha e resultado de verdade, nao layout.
    """
    return page.locator("table tr").count() > 1


def dispensar_modal(page, timeout_s=10):
    """Fecha o aviso de "nao existe faturas" e devolve a mensagem, ou None.

    Um ano sem faturas nao devolve tabela vazia: devolve um modal. Ele nao tem
    id nem name - estrutura-resultado.json registrou so os botoes anonimos "x" e
    "OK" -, entao o unico jeito de alcanca-lo e por texto e por role. Sem
    dispensa-lo, ele fica por cima e bloqueia o proximo clique em Consultar.
    """
    fim = time.time() + timeout_s
    while time.time() < fim:
        texto = page.evaluate("document.body.innerText || ''")
        achado = re.search(r"N[aã]o existe Faturas[^\n]*", texto)
        if achado:
            msg = achado.group(0).strip()
            try:
                ok = page.get_by_role("button", name=re.compile(r"^\s*OK\s*$", re.I)).first
                if ok.count():
                    ok.click()
                    page.wait_for_timeout(1000)
            except Exception as e:
                log(f"(nao consegui clicar no OK do modal: {str(e)[:60]})")
            return msg
        if tem_resultados(page):
            return None
        page.wait_for_timeout(500)
    return None


def consultar(page):
    """Consulta o historico ano a ano, do mais recente para tras.

    A pagina lista apenas as faturas JA PAGAS da UC, entao o ano corrente pode
    estar vazio - foi exatamente o que travou a coleta antes: o codigo fixava
    max(anos) e desistia quando 2026 nao trouxe nada. Descer para 2025, 2024...
    e o que faz a consulta render resultado.

    Devolve o ano que trouxe linhas, ou None.
    """
    page.wait_for_selector("select", timeout=30_000)
    est = dump_estrutura(page, "historico")

    # O select do ano e o que tem anos como opcoes (o outro select e a UC).
    alvo = None
    for s in est["selects"]:
        if not s["visivel"]:
            continue
        anos = [o["t"] for o in s["opcoes"] if re.fullmatch(r"(19|20)\d{2}", o["t"])]
        if anos and (alvo is None or len(anos) > len(alvo[1])):
            alvo = (s, anos)
    if alvo is None:
        log("Nao encontrei o select de ano")
        return None

    sel_ano, anos = alvo
    seletor = f"#{sel_ano['id']}" if sel_ano["id"] else f"select[name='{sel_ano['name']}']"
    candidatos = sorted(anos, reverse=True)[:ANOS_ATRAS]
    log(f"Anos a tentar: {', '.join(candidatos)}")

    for ano in candidatos:
        # Consultar e um postback: a pagina volta inteira e o select pode ter
        # perdido a selecao, entao reescolhemos o ano a cada volta.
        page.wait_for_selector(seletor, timeout=20_000)
        page.select_option(seletor, label=ano)
        page.wait_for_timeout(random.randint(600, 1400))

        botao = page.locator(SEL_CONSULTAR).first
        if botao.count() == 0:
            log("Botao Consultar nao encontrado")
            return None
        log(f"Consultando {ano}")
        botao.click()
        try:
            page.wait_for_load_state("networkidle", timeout=45_000)
        except PWTimeout:
            pass

        vazio = dispensar_modal(page)
        dump_estrutura(page, f"resultado-{ano}")
        if vazio:
            log(f"Ano {ano}: {vazio}")
            continue
        if tem_resultados(page):
            log(f"Ano {ano}: tabela com resultados")
            return ano
        log(f"Ano {ano}: nem modal nem tabela; tentando o ano anterior")

    log(f"Nenhum dos {len(candidatos)} anos mais recentes retornou faturas")
    return None


def _snapshot():
    return {f: f.stat().st_size for f in FATURAS.glob("*") if f.is_file()}


def baixar_espelho(ctx, invoice, nome):
    """Baixa o PDF de uma fatura pelo seu indice `invoice` no grid.

    Fluxo real do site (confirmado sondando a pagina): a coluna Download faz
    window.open('mostrarEspelhoFatura.aspx?invoice=N'), que NAO entrega o PDF -
    exibe um modal "espelho gerado, protocolo N". E o clique no botao OK desse
    modal (#CONTENT_btnModal) que dispara o download do binario.

    Como o Chrome e dirigido por CDP externo (nao lancado pelo Playwright),
    download.save_as() grava 0 byte: quem escreve o arquivo e o proprio Chrome,
    no downloadPath que configuramos. Entao nao usamos o evento do Playwright -
    deixamos o Chrome baixar e capturamos o arquivo novo por varredura do
    diretorio, esperando ele estabilizar (sem .crdownload).
    """
    FATURAS.mkdir(exist_ok=True)
    url = f"{BASE}/AgenciaGO/Servi%C3%A7os/comum/mostrarEspelhoFatura.aspx?invoice={invoice}"

    pop = ctx.new_page()
    try:
        cdp = ctx.new_cdp_session(pop)
        cdp.send("Browser.setDownloadBehavior",
                 {"behavior": "allow", "downloadPath": str(FATURAS)})
    except Exception as e:
        log(f"(setDownloadBehavior indisponivel: {str(e)[:60]})")

    antes = _snapshot()
    try:
        pop.goto(url, wait_until="domcontentloaded")
        pop.wait_for_timeout(random.randint(1800, 2600))
        clicou = pop.evaluate(
            """(sel) => {
                const cand = [...document.querySelectorAll(
                    'input[type=button],input[type=submit],button,a')]
                  .find(x => (x.matches(sel) ||
                              /^\\s*ok\\s*$/i.test((x.value||x.innerText||'').trim()))
                             && x.offsetParent !== null);
                if (cand) { cand.click(); return true; }
                return false;
            }""",
            SEL_OK_MODAL,
        )
        if not clicou:
            log("Botao OK do modal nao encontrado no espelho")
            return None
    finally:
        # o download segue no Chrome mesmo com a aba fechada; fechar evita
        # acumular abas ao longo das varias faturas
        pop.wait_for_timeout(1500)

    destino = FATURAS / nome
    alvo, prev, estavel = None, -1, 0
    fim = time.time() + 45
    while time.time() < fim:
        agora = _snapshot()
        novos = [f for f, s in agora.items()
                 if s > 1000 and (f not in antes or antes[f] != s)
                 and not f.name.endswith(".crdownload")]
        pdfs = [f for f in novos if f.suffix.lower() == ".pdf" and f.read_bytes()[:4] == b"%PDF"]
        if pdfs:
            cand = pdfs[0]
            estavel = estavel + 1 if (cand == alvo and agora[cand] == prev) else 0
            alvo, prev = cand, agora[cand]
            if estavel >= 2:
                if cand != destino:
                    if destino.exists():
                        destino.unlink()
                    cand.rename(destino)
                log(f"Baixado: {destino.name} ({destino.stat().st_size} bytes)")
                try:
                    pop.close()
                except Exception:
                    pass
                return destino
        pop.wait_for_timeout(1000)

    log("Download nao estabilizou em 45s")
    try:
        pop.close()
    except Exception:
        pass
    return None


def linhas_faturas(page):
    """Le a tabela e devolve [(invoice, rotulo)] da mais recente para a mais antiga.

    O invoice sai do onclick (window.open(...invoice=N...)); o rotulo vem do
    "Mes / Ano" da primeira coluna, para nomear o arquivo.
    """
    return page.evaluate(r"""() => {
        const linhas = [...document.querySelectorAll('table tr')].slice(1);
        const out = [];
        for (const tr of linhas) {
            const a = tr.querySelector('a[onclick*="invoice="]');
            if (!a) continue;
            const m = (a.getAttribute('onclick') || '').match(/invoice=(\d+)/);
            if (!m) continue;
            const ref = (tr.cells[0] ? tr.cells[0].innerText : '').trim();
            out.push({ invoice: parseInt(m[1], 10), rotulo: ref });
        }
        return out;
    }""")


def baixar_mais_recente(page, ctx, ano):
    """Baixa o PDF da fatura do topo da lista (a mais recente)."""
    dump_estrutura(page, f"download-{ano}")
    linhas = linhas_faturas(page)
    if not linhas:
        log(f"Nao achei linhas com invoice na tabela - veja estrutura-download-{ano}.json")
        return None

    alvo = linhas[0]  # invoice=0 = mais recente
    ref = re.sub(r"[^0-9A-Za-z]+", "-", alvo["rotulo"]).strip("-") or ano
    log(f"Fatura mais recente: {alvo['rotulo']!r} (invoice={alvo['invoice']})")
    return baixar_espelho(ctx, alvo["invoice"], f"fatura-{ref}.pdf")


def coletar():
    with sync_playwright() as p:
        ctx, page = conectar(p)
        dialogos = escutar_dialogos(page)

        if not garantir_login(page, dialogos, ctx=ctx, prazo_s=PRAZO):
            log("Nao consegui logar dentro do prazo")
            return 1

        # A consulta tambem pode falhar por instabilidade; insiste com a mesma
        # politica de espera curta usada no login.
        limite = time.time() + PRAZO
        rodada = 0
        while time.time() < limite:
            rodada += 1
            log(f"=== Consulta, rodada {rodada} ===")
            if not area_logada(page, timeout_s=8):
                log("Sessao caiu; refazendo login")
                if not garantir_login(page, dialogos, ctx=ctx,
                                      prazo_s=int(limite - time.time())):
                    return 1
            ir_para(page, URL_FATURAS)
            page.wait_for_timeout(2500)

            try:
                ano = consultar(page)
                if ano:
                    caminho = baixar_mais_recente(page, ctx, ano)
                    if caminho and caminho.exists() and caminho.stat().st_size > 1000:
                        log(f"PRONTO: {caminho} ({caminho.stat().st_size} bytes)")
                        return 0
                    log("Consulta feita, mas o download nao veio")
            except PWTimeout as e:
                log(f"Timeout na rodada {rodada}: {str(e)[:90]}")

            page.screenshot(path=str(FATURAS / f"rodada-{rodada}.png"), full_page=True)
            espera = random.randint(ESPERA_BASE, ESPERA_MAX)
            if time.time() + espera > limite:
                break
            log(f"Aguardando {espera}s antes de tentar de novo")
            page.wait_for_timeout(espera * 1000)

        log("Prazo esgotado sem baixar a fatura")
        return 1


def main():
    FATURAS.mkdir(exist_ok=True)
    # A rota so muda com EQUATORIAL_ROTACIONAR=1; o finally e o que garante que
    # a maquina nao fique navegando pelo 4G depois que o robo sai.
    estado_rede = preparar_rede()
    try:
        return coletar()
    finally:
        liberar_rede(estado_rede)


if __name__ == "__main__":
    sys.exit(main())
