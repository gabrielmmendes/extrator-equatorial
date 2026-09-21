#!/usr/bin/env python3
"""
Baixa a fatura mais recente da Agencia Virtual da Equatorial Goias.

Fluxo: garante login (login.py) -> tenta a tela de 2a via da fatura pendente
(segunda_via.py) -> so se ela nao resolver, Historico de Faturas -> consulta ano
a ano, do mais recente para tras -> baixa a fatura do topo.

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

import humano as H

from login import (
    ESPERA_BASE, ESPERA_MAX, PRAZO, URL_FATURAS,
    area_logada, conectar, escutar_dialogos, esperar_postback, garantir_login,
    ir_para, liberar_rede, log, preparar_rede, texto_da_pagina,
)

FATURAS = Path(__file__).parent / "faturas"
# Onde o Chrome larga o download antes de virar fatura-<uc>-<mes>-<ano>.pdf.
# Igual a FATURAS no modo de sempre. No paralelo cada worker aponta a sua
# (faturas/.baixando-N), e isso nao e zelo: esperar_arquivo() captura "qualquer
# PDF novo na pasta", entao com dois Chromes gravando no mesmo lugar o worker A
# renomeia o PDF do B para o nome da UC dele. Fatura arquivada sob o titular
# errado e o pior desfecho que este projeto sabe produzir, e o unico que nao
# aparece em log nenhum. Fica DENTRO de faturas/ para o rename final nao
# atravessar volume.
DESCARGA = FATURAS
ANOS_ATRAS = int(os.environ.get("EQUATORIAL_ANOS_ATRAS", "3"))
SEL_CONSULTAR = ("#CONTENT_btEnviar, input[value*='Consultar' i], "
                 "button:has-text('Consultar')")
BASE = "https://goias.equatorialenergia.com.br"
# O botao OK do modal de protocolo. Confirmar aqui e o que de fato dispara o
# download do PDF - abrir a mostrarEspelhoFatura.aspx apenas exibe o modal
# "espelho gerado, protocolo N"; o binario so vem depois deste clique.
SEL_OK_MODAL = "#CONTENT_btnModal, input[value='OK' i]"


def parse_mes(bruto):
    """Le o mes pedido pelo usuario e devolve (mes, ano) como inteiros.

    Aceita as formas que alguem digitaria sem pensar: 7/2026, 07/2026,
    07-2026, 2026-07 e 2026/7. Sem valor, devolve None - que e o modo padrao
    (fatura mais recente). Formato invalido levanta ValueError: seguir em
    frente e baixar o mes errado e pior do que parar aqui.
    """
    if bruto is None:
        return None
    bruto = str(bruto).strip()
    if not bruto:
        return None
    achado = re.fullmatch(r"\s*(\d{1,4})\s*[/\-.]\s*(\d{1,4})\s*", bruto)
    if not achado:
        raise ValueError(f"Mes invalido: {bruto!r}. Use MM/AAAA (ex.: 07/2026)")
    a, b = int(achado.group(1)), int(achado.group(2))
    mes, ano = (b, a) if a > 12 else (a, b)   # 2026-07 ou 07/2026
    if not 1 <= mes <= 12 or not 1900 <= ano <= 2999:
        raise ValueError(f"Mes invalido: {bruto!r}. Use MM/AAAA (ex.: 07/2026)")
    return mes, ano


def mes_do_ambiente():
    """(mes, ano) de EQUATORIAL_MES, ou None. Serve a quem chama pela .ps1."""
    return parse_mes(os.environ.get("EQUATORIAL_MES"))


def mes_do_rotulo(rotulo):
    """(mes, ano) da primeira coluna do grid ("7/2026"), ou None."""
    achado = re.search(r"(\d{1,2})\s*/\s*(\d{4})", rotulo or "")
    if not achado:
        return None
    return int(achado.group(1)), int(achado.group(2))


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
    DESCARGA.mkdir(parents=True, exist_ok=True)
    destino = DESCARGA / f"estrutura-{rotulo}.json"
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


def dispensar_modal(page, timeout_s=10, persona=None):
    """Fecha o aviso de "nao existe faturas" e devolve a mensagem, ou None.

    Um ano sem faturas nao devolve tabela vazia: devolve um modal. Ele nao tem
    id nem name - estrutura-resultado.json registrou so os botoes anonimos "x" e
    "OK" -, entao o unico jeito de alcanca-lo e por texto e por role. Sem
    dispensa-lo, ele fica por cima e bloqueia o proximo clique em Consultar.
    """
    fim = time.time() + timeout_s
    while time.time() < fim:
        texto = texto_da_pagina(page)
        achado = re.search(r"N[aã]o existe Faturas[^\n]*", texto)
        if achado:
            msg = achado.group(0).strip()
            try:
                ok = page.get_by_role("button", name=re.compile(r"^\s*OK\s*$", re.I)).first
                if ok.count():
                    # Ler o aviso antes de dispensar: modal que some em 200ms
                    # nao foi lido por ninguem.
                    H.pensar(page, persona, 1.6, 0.5, teto=9)
                    H.clicar(ok, page, persona)
                    H.pensar(page, persona, 0.9, 0.5)
            except Exception as e:
                log(f"(nao consegui clicar no OK do modal: {str(e)[:60]})")
            return msg
        if tem_resultados(page):
            return None
        page.wait_for_timeout(500)
    return None


def consultar(page, persona=None, ano_alvo=None):
    """Consulta o historico ano a ano, do mais recente para tras.

    Com `ano_alvo`, consulta so aquele ano: quem pediu um mes especifico nao
    ganha nada com a varredura, e cada Consultar a mais e um postback a mais
    para a sessao explicar.

    A pagina lista apenas as faturas JA PAGAS da UC, entao o ano corrente pode
    estar vazio - foi exatamente o que travou a coleta antes: o codigo fixava
    max(anos) e desistia quando 2026 nao trouxe nada. Descer para 2025, 2024...
    e o que faz a consulta render resultado.

    Devolve o ano que trouxe linhas, ou None.
    """
    page.wait_for_selector("select", timeout=30_000)
    H.entre_telas(page, persona, mediana=2.6)
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
    if ano_alvo:
        if str(ano_alvo) not in anos:
            log(f"Ano {ano_alvo} nao esta no select "
                f"(disponiveis: {', '.join(sorted(anos, reverse=True))})")
            return None
        candidatos = [str(ano_alvo)]
    else:
        candidatos = sorted(anos, reverse=True)[:ANOS_ATRAS]
    log(f"Anos a tentar: {', '.join(candidatos)}")

    for ano in candidatos:
        # Consultar e um postback: a pagina volta inteira e o select pode ter
        # perdido a selecao, entao reescolhemos o ano a cada volta.
        page.wait_for_selector(seletor, timeout=20_000)
        # Abrir o select, escolher e so entao ir ao botao: entre uma acao e
        # outra ha o tempo de achar a opcao na lista.
        H.clicar(page.locator(seletor).first, page, persona, hesitacao=0.2)
        page.select_option(seletor, label=ano)
        H.pensar(page, persona, 1.1, 0.55, teto=8)

        botao = page.locator(SEL_CONSULTAR).first
        if botao.count() == 0:
            log("Botao Consultar nao encontrado")
            return None
        log(f"Consultando {ano}")
        H.clicar(botao, page, persona)
        # Pronto = a tabela de faturas do ano, ou o modal de "nao existe". Sao
        # os dois desfechos que dispensar_modal/tem_resultados leem logo abaixo.
        esperar_postback(
            page,
            lambda: tem_resultados(page)
            or bool(re.search(r"N[aã]o existe Faturas", texto_da_pagina(page))),
            rotulo=f"Consultar {ano}")
        H.entre_telas(page, persona, mediana=2.4)
        H.talvez_distrair(page, persona, chance=0.10)

        vazio = dispensar_modal(page, persona=persona)
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


def snapshot():
    return {f: f.stat().st_size for f in DESCARGA.glob("*") if f.is_file()}


def _e_pdf(caminho):
    """Le so os 4 bytes do cabecalho.

    read_bytes()[:4] carregava o arquivo INTEIRO para conferir a assinatura, a
    cada giro do laco de espera, para cada candidato - com faturas/ ja em uma
    centena de PDFs isso e I/O gratuito no caminho critico de toda UC.
    """
    try:
        with open(caminho, "rb") as fh:
            return fh.read(4) == b"%PDF"
    except OSError:
        return False


def permitir_download(ctx, page):
    """Manda o Chrome gravar os downloads em faturas/.

    Como o Chrome e dirigido por CDP externo (nao lancado pelo Playwright),
    download.save_as() grava 0 byte: quem escreve o arquivo e o proprio Chrome,
    no downloadPath configurado aqui.
    """
    try:
        cdp = ctx.new_cdp_session(page)
        cdp.send("Browser.setDownloadBehavior",
                 {"behavior": "allow", "downloadPath": str(DESCARGA)})
        return True
    except Exception as e:
        log(f"(setDownloadBehavior indisponivel: {str(e)[:60]})")
        return False


def esperar_arquivo(page, antes, destino, timeout_s=45):
    """Espera o Chrome terminar de gravar o PDF novo e o renomeia para `destino`.

    Nao usamos o evento de download do Playwright (ver permitir_download): a
    captura e por varredura do diretorio, esperando o arquivo estabilizar - sem
    .crdownload e com o mesmo tamanho em duas leituras seguidas.
    """
    alvo, prev, estavel = None, -1, 0
    fim = time.time() + timeout_s
    while time.time() < fim:
        agora = snapshot()
        novos = [f for f, s in agora.items()
                 if s > 1000 and (f not in antes or antes[f] != s)
                 and not f.name.endswith(".crdownload")]
        pdfs = [f for f in novos
                if f.suffix.lower() == ".pdf" and _e_pdf(f)]
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
                return destino
        # 250ms: sao precisas DUAS leituras de tamanho igual para o arquivo ser
        # dado por estavel, entao o intervalo entra dobrado na latencia. A 1s
        # isso custava 2s por UC sem nada em troca - a estabilidade e do disco,
        # nao do relogio.
        page.wait_for_timeout(250)
    log(f"Download nao estabilizou em {timeout_s}s")
    return None


ESPELHO = BASE + "/AgenciaGO/Servi%C3%A7os/comum/mostrarEspelhoFatura.aspx?invoice="


def baixar_espelho(ctx, invoice, nome, persona=None, base_url=ESPELHO):
    """Baixa o PDF de uma fatura pelo seu indice `invoice` no grid.

    Fluxo real do site (confirmado sondando a pagina): a coluna Download faz
    window.open('mostrarEspelhoFatura.aspx?invoice=N'), que NAO entrega o PDF -
    exibe um modal "espelho gerado, protocolo N". E o clique no botao OK desse
    modal (#CONTENT_btnModal) que dispara o download do binario.

    `base_url` existe porque a tela de 2a via usa a mesma mecanica em outro
    endereco (aberto/mostrarFaturaCompleta.aspx?invoice=N) - mesma pagina de
    protocolo, mesmo OK, mesmo download.

    A gravacao do arquivo fica com permitir_download/esperar_arquivo.
    """
    FATURAS.mkdir(exist_ok=True)
    DESCARGA.mkdir(parents=True, exist_ok=True)
    url = f"{base_url}{invoice}"

    pop = ctx.new_page()
    permitir_download(ctx, pop)
    antes = snapshot()
    try:
        pop.goto(url, wait_until="domcontentloaded")
        # O modal do protocolo demora a montar; alem disso ninguem confirma um
        # protocolo antes de le-lo.
        H.entre_telas(pop, persona, mediana=2.6)
        H.pensar(pop, persona, 1.4, 0.5, teto=10)
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
    caminho = esperar_arquivo(pop, antes, destino)
    try:
        pop.close()
    except Exception:
        pass
    return caminho


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


def baixar_mais_recente(page, ctx, ano, persona=None, mes=None):
    """Baixa o PDF de uma fatura da lista.

    Sem `mes`, pega a do topo (a mais recente). Com `mes` (int 1-12), procura a
    linha cujo "Mes / Ano" bate com mes/ano e baixa aquela; se o mes nao estiver
    na tabela, devolve None em vez de cair no topo - baixar silenciosamente a
    fatura errada seria pior do que falhar.
    """
    dump_estrutura(page, f"download-{ano}")
    linhas = linhas_faturas(page)
    if not linhas:
        log(f"Nao achei linhas com invoice na tabela - veja estrutura-download-{ano}.json")
        return None

    if mes:
        procurado = (int(mes), int(ano))
        alvo = next((l for l in linhas if mes_do_rotulo(l["rotulo"]) == procurado), None)
        if not alvo:
            disponiveis = ", ".join(l["rotulo"] for l in linhas) or "(nenhuma)"
            log(f"Mes {mes}/{ano} nao esta na tabela. Disponiveis: {disponiveis}")
            return None
    else:
        alvo = linhas[0]  # invoice=0 = mais recente
    ref = re.sub(r"[^0-9A-Za-z]+", "-", alvo["rotulo"]).strip("-") or ano
    rotulo_log = "Fatura escolhida" if mes else "Fatura mais recente"
    log(f"{rotulo_log}: {alvo['rotulo']!r} (invoice={alvo['invoice']})")
    # Olhar a tabela antes de clicar no download da linha certa.
    H.ler(page, persona, segundos=H.pausa_s(4.5, 0.5, teto=25))
    return baixar_espelho(ctx, alvo["invoice"], f"fatura-{ref}.pdf", persona)


def coletar(alvo=None):
    """Laco de coleta. `alvo` e (mes, ano) de parse_mes, ou None p/ a mais recente."""
    mes, ano_pedido = alvo if alvo else (None, None)
    with sync_playwright() as p:
        ctx, page = conectar(p)
        dialogos = escutar_dialogos(page)

        if not garantir_login(page, dialogos, ctx=ctx, prazo_s=PRAZO):
            log("Nao consegui logar dentro do prazo")
            return 1

        # Com sessao na mao, a tela de 2a via emite direto a fatura pendente -
        # tres selects e um clique, contra a varredura ano a ano do historico.
        # Importado aqui dentro porque segunda_via importa este modulo (o
        # inverso, no topo, seria import circular).
        import login as _login
        import segunda_via as sv
        try:
            caminho, motivo = sv.tentar(page, ctx, _login.UC, mes=mes, ano=ano_pedido)
            if caminho:
                log(f"PRONTO: {caminho} ({caminho.stat().st_size} bytes)")
                return 0
            log(f"Segunda via nao resolveu ({motivo}); indo pelo historico")
        except Exception as e:
            log(f"Segunda via falhou ({type(e).__name__}: {str(e)[:80]}); "
                "indo pelo historico")

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
                ano = consultar(page, ano_alvo=ano_pedido)
                if ano:
                    caminho = baixar_mais_recente(page, ctx, ano, mes=mes)
                    if caminho and caminho.exists() and caminho.stat().st_size > 1000:
                        log(f"PRONTO: {caminho} ({caminho.stat().st_size} bytes)")
                        return 0
                    log("Consulta feita, mas o download nao veio")
                    if mes:
                        # Mes fixo que nao esta na tabela nao vai aparecer na
                        # rodada seguinte: insistir so gasta sessao.
                        return 1
            except PWTimeout as e:
                log(f"Timeout na rodada {rodada}: {str(e)[:90]}")

            page.screenshot(path=str(DESCARGA / f"rodada-{rodada}.png"), full_page=True)
            espera = random.randint(ESPERA_BASE, ESPERA_MAX)
            if time.time() + espera > limite:
                break
            log(f"Aguardando {espera}s antes de tentar de novo")
            page.wait_for_timeout(espera * 1000)

        log("Prazo esgotado sem baixar a fatura")
        return 1


def mes_dos_argumentos(args):
    """(mes, ano) vindo de --mes/-m na linha de comando, ou do ambiente."""
    for i, a in enumerate(args):
        if a in ("--mes", "-m"):
            if i + 1 >= len(args):
                raise ValueError("--mes exige um valor (ex.: --mes 07/2026)")
            return parse_mes(args[i + 1])
        if a.startswith("--mes="):
            return parse_mes(a.split("=", 1)[1])
    return mes_do_ambiente()


def main():
    FATURAS.mkdir(exist_ok=True)
    try:
        alvo = mes_dos_argumentos(sys.argv[1:])
    except ValueError as e:
        log(str(e))
        return 2
    if alvo:
        log(f"Mes pedido: {alvo[0]:02d}/{alvo[1]}")
    # A rota so muda com EQUATORIAL_ROTACIONAR=1; o finally e o que garante que
    # a maquina nao fique navegando pelo 4G depois que o robo sai.
    estado_rede = preparar_rede()
    try:
        return coletar(alvo)
    finally:
        liberar_rede(estado_rede)


if __name__ == "__main__":
    sys.exit(main())
