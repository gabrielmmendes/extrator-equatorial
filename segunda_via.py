#!/usr/bin/env python3
"""
Segunda via da fatura PENDENTE, pela tela propria do portal.

    AgenciaGO/Servicos/aberto/SegundaVia.aspx

"aberto" ali nao e "area aberta": e "faturas em aberto". A pagina exige sessao -
aberta deslogado, o ASP.NET estoura e o site joga para Suporte.aspx. Sondagem de
04/09/2026 (logado) mostrou o formulario real, que nao pede UC nem documento:

    #CONTENT_comboBoxUC     select com TODAS as UCs do titular
    #CONTENT_cbTipoEmissao  "Emitir fatura completa" | "apenas codigo de barras"
    #CONTENT_cbMotivo       motivo da 2a via (campo obrigatorio do CRM)
    #CONTENT_btEnviar       botao "Emitir"

Emitir nao baixa nada: devolve o grid das faturas PENDENTES da UC
("Mes/Ano de referencia | Valor | Download"), e o link Download vai para
aberto/mostrarFaturaCompleta.aspx?invoice=N - mesma mecanica ja conhecida do
espelho: a pagina mostra o modal de protocolo e o binario so vem no clique do OK.

Por que ela vem antes do historico: sao tres selects e um clique, contra o
HistoricoFaturas, que consulta ano a ano ate achar linha e so entao abre o
espelho. Alem disso o historico lista as faturas JA PAGAS - a fatura do mes
corrente, que e o que a coleta quer, esta justamente aqui.

Quando ela NAO resolve, o caminho antigo (baixar_fatura.py) assume:
  - nao ha fatura pendente (ja paga): so o historico tem;
  - mes pedido com --mes que ja foi pago: nao aparece entre as pendentes;
  - a sessao caiu, ou a pagina mudou de forma.

Uso direto (Chrome de pe pelo chrome.ps1 e sessao ja autenticada):
    python segunda_via.py             # emite a 2a via da UC do credenciais.env
    python segunda_via.py --sondar    # so abre e dumpa a estrutura, sem emitir
"""

import os
import re
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import humano as H
import baixar_fatura as bf
import login
from login import ir_para, log, texto_da_pagina

URL = ("https://goias.equatorialenergia.com.br/AgenciaGO/"
       "Servi%C3%A7os/aberto/SegundaVia.aspx")

SEL_UC = "#CONTENT_comboBoxUC"
SEL_TIPO = "#CONTENT_cbTipoEmissao"
SEL_MOTIVO = "#CONTENT_cbMotivo"
SEL_EMITIR = "#CONTENT_btEnviar"

RE_TIPO = re.compile(r"completa", re.I)
# O motivo vai para o CRM da distribuidora. "Nao recebeu a fatura" e o mais
# banal da lista; EQUATORIAL_MOTIVO_2VIA troca por outro sem mexer no codigo.
RE_MOTIVO = re.compile(os.environ.get("EQUATORIAL_MOTIVO_2VIA", r"n[aã]o recebeu"), re.I)
# "Nao existem faturas em aberto", "nao ha faturas pendentes", "nenhum debito".
RE_SEM_FATURA = re.compile(
    r"(n[aã]o (existe|existem|h[aá]|foram encontrad\w*|possui|consta\w*)|nenhum\w*)"
    r"[^\n]{0,60}(fatura|d[eé]bito|conta|pendencia|pend[eê]ncia)[^\n]*", re.I)
# O grid escreve a competencia como "AGO/2026"; o mes pedido chega como numero.
MESES = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
     "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"])}
RE_MES = re.compile(r"\b(\d{1,2}|[A-Za-z]{3})\s*/\s*(\d{4})\b")
# A pagina do PDF: mesma mecanica do espelho, outro endereco.
URL_PDF = ("https://goias.equatorialenergia.com.br/AgenciaGO/"
           "Servi%C3%A7os/aberto/mostrarFaturaCompleta.aspx?invoice=")


def _opcoes(page, seletor):
    """[(value, texto)] do select, na ordem em que aparecem."""
    return page.evaluate(
        """(sel) => {
            const s = document.querySelector(sel);
            return s ? [...s.options].map(o => [o.value, o.text.trim()]) : [];
        }""", seletor)


def escolher(page, seletor, casar, persona=None, rotulo=""):
    """Seleciona no `seletor` a opcao cujo texto (ou value) casa com `casar`.

    `casar` e regex ou string exata de value. Devolve o texto escolhido, ou None.
    Cada select e aberto com o mouse antes de escolher: WebForms escuta change,
    e a diferenca de custo e uma pausa curta.
    """
    opcoes = _opcoes(page, seletor)
    if not opcoes:
        log(f"Segunda via: select {rotulo or seletor} nao encontrado")
        return None

    alvo = None
    for valor, texto in opcoes:
        if not valor:
            continue  # a opcao "Selecione"
        if (casar.search(texto) if hasattr(casar, "search") else valor == casar):
            alvo = (valor, texto)
            break
    if alvo is None:
        disp = ", ".join(t for _, t in opcoes if t)[:200]
        log(f"Segunda via: nenhuma opcao de {rotulo or seletor} casa. Ha: {disp}")
        return None

    H.clicar(page.locator(seletor).first, page, persona, hesitacao=0.2)
    page.select_option(seletor, value=alvo[0])
    H.pensar(page, persona, 1.0, 0.5, teto=7)
    log(f"Segunda via: {rotulo or seletor} = {alvo[1]!r}")
    return alvo[1]


def na_pagina(page):
    """True se a SegundaVia.aspx carregou de fato (e nao o erro/Login)."""
    if page.locator(SEL_UC).count() == 0:
        return False
    return page.locator(SEL_EMITIR).count() > 0


def mes_do_rotulo(rotulo):
    """(mes, ano) de "AGO/2026" ou "08/2026", ou None."""
    achado = RE_MES.search(rotulo or "")
    if not achado:
        return None
    bruto, ano = achado.group(1), int(achado.group(2))
    mes = int(bruto) if bruto.isdigit() else MESES.get(bruto.upper())
    return (mes, ano) if mes and 1 <= mes <= 12 else None


def linhas_pendentes(page):
    """[{invoice, rotulo}] do grid que o Emitir devolve, na ordem da propria tela.

    Confirmado em 04/09/2026: a tela lista da mais ANTIGA para a mais recente
    (JUL/2026 invoice=0, AGO/2026 invoice=1) - o inverso do historico. Por isso
    quem quer "a mais recente" escolhe pela competencia, nao pela posicao.
    """
    return page.evaluate(r"""() => {
        const out = [];
        for (const tr of document.querySelectorAll('table tr')) {
            const a = [...tr.querySelectorAll('a, button, input[type=button]')]
                .find(x => /invoice=/i.test((x.getAttribute('onclick') || '') +
                                            (x.getAttribute('href') || '')));
            if (!a) continue;
            const m = ((a.getAttribute('onclick') || '') +
                       (a.getAttribute('href') || '')).match(/invoice=(\d+)/);
            if (!m) continue;
            out.push({
                invoice: parseInt(m[1], 10),
                rotulo: (tr.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120),
            });
        }
        return out;
    }""")


def emitir_grid(page, persona=None):
    """Clica Emitir e devolve (linhas, motivo) do grid de pendentes.

    motivo: "ok" (linhas nao vazias), "sem_fatura" (o site disse que nao ha
    nenhuma) ou "falhou". Nao baixa nada: o grid fica na tela e cada linha pode
    ser baixada depois com baixar_linha() - o download abre em aba propria, entao
    a pagina do grid continua de pe para a linha seguinte.
    """
    bf.FATURAS.mkdir(exist_ok=True)

    botao = page.locator(SEL_EMITIR).first
    if botao.count() == 0:
        log("Segunda via: botao Emitir nao encontrado")
        return [], "falhou"
    # Ninguem preenche tres selects e clica no mesmo instante.
    H.ler(page, persona, segundos=H.pausa_s(4.0, 0.5, teto=20))
    H.clicar(botao, page, persona)
    # Pronto = o grid de pendentes montou, ou o aviso de que nao ha nenhuma. Sao
    # os dois unicos desfechos do Emitir, e e o que a proxima linha vai ler.
    login.esperar_postback(
        page,
        lambda: bool(linhas_pendentes(page))
        or bool(RE_SEM_FATURA.search(texto_da_pagina(page))),
        rotulo="Emitir")
    H.entre_telas(page, persona, mediana=2.4)
    bf.dump_estrutura(page, "segundavia-emissao")

    linhas = linhas_pendentes(page)
    if not linhas:
        achado = RE_SEM_FATURA.search(texto_da_pagina(page))
        if achado:
            log(f"Segunda via: {achado.group(0).strip()[:120]}")
            return [], "sem_fatura"
        log("Segunda via: o Emitir nao devolveu grid nem aviso - veja "
            "estrutura-segundavia-emissao.json")
        return [], "falhou"

    log(f"Segunda via: {len(linhas)} fatura(s) pendente(s): "
        + " | ".join(l["rotulo"][:40] for l in linhas))
    return linhas, "ok"


def _ordem(linha):
    """Chave de ordenacao por competencia: (ano, mes). Sem competencia, (0, 0).

    mes_do_rotulo devolve (mes, ano); comparar essa tupla crua punha DEZ/2025
    acima de JAN/2026 e fazia "a mais recente" sair do ano errado.
    """
    ref = mes_do_rotulo(linha["rotulo"])
    return (ref[1], ref[0]) if ref else (0, 0)


def escolher_pendente(linhas, mes=None, ano=None, mais_antiga=False):
    """A linha do grid a baixar, ou None.

    Com mes/ano, so aquela competencia. Sem eles, a mais recente pela
    competencia - ou a mais antiga, com `mais_antiga`. Linha sem "MMM/AAAA"
    legivel fica por ultimo, mas nao e descartada: sem nenhuma outra, ela e a
    resposta.
    """
    if not linhas:
        return None
    if mes and ano:
        return next((l for l in linhas
                     if mes_do_rotulo(l["rotulo"]) == (int(mes), int(ano))), None)
    if mais_antiga:
        datadas = [l for l in linhas if mes_do_rotulo(l["rotulo"])]
        return min(datadas, key=_ordem) if datadas else linhas[0]
    return max(linhas, key=_ordem)


def baixar_linha(page, ctx, linha, persona=None, nome=None):
    """Baixa o PDF de uma linha do grid de pendentes. Devolve o caminho ou None."""
    ref = mes_do_rotulo(linha["rotulo"])
    if not nome:
        nome = f"fatura-{ref[0]}-{ref[1]}.pdf" if ref else "fatura-2via.pdf"
    log(f"Segunda via: baixando {linha['rotulo'][:70]!r} (invoice={linha['invoice']})")
    # Olhar o grid antes de clicar no download da linha certa.
    H.ler(page, persona, segundos=H.pausa_s(4.0, 0.5, teto=20))
    caminho = bf.baixar_espelho(ctx, linha["invoice"], nome, persona, base_url=URL_PDF)
    if caminho and caminho.exists() and caminho.stat().st_size > 1000:
        return caminho
    return None


def emitir(page, ctx, persona=None, mes=None, ano=None, nome=None, mais_antiga=False):
    """Clica Emitir, escolhe a fatura no grid e traz o PDF. (caminho|None, motivo)."""
    linhas, motivo = emitir_grid(page, persona)
    if not linhas:
        return None, motivo
    alvo = escolher_pendente(linhas, mes, ano, mais_antiga)
    if not alvo:
        log(f"Segunda via: {mes:02d}/{ano} nao esta entre as pendentes")
        return None, "mes_ausente"
    caminho = baixar_linha(page, ctx, alvo, persona, nome)
    return (caminho, "ok") if caminho else (None, "falhou")


def abrir(page, uc, persona=None):
    """Abre a SegundaVia.aspx e preenche UC, tipo e motivo, sem emitir.

    Devolve None quando o formulario esta pronto para o Emitir, ou o motivo da
    falha: "login" (a pagina nao carregou o formulario - sessao caida),
    "uc_ausente" (a UC nao esta no combo do titular) ou "falhou".
    """
    ir_para(page, URL)
    try:
        page.wait_for_selector(SEL_UC, timeout=20_000)
    except PWTimeout:
        pass
    if not na_pagina(page):
        log(f"Segunda via: a pagina nao carregou o formulario (url={page.url[:90]})")
        bf.dump_estrutura(page, "segundavia-form")
        return "login"

    H.entre_telas(page, persona, mediana=2.8)
    digitos = "".join(filter(str.isdigit, str(uc)))
    valor = next((v for v, _ in _opcoes(page, SEL_UC)
                  if "".join(filter(str.isdigit, v)) == digitos), None)
    if valor is None:
        disp = ", ".join(v for v, _ in _opcoes(page, SEL_UC) if v)[:200]
        log(f"Segunda via: UC {uc} nao esta no combo do titular. Ha: {disp}")
        return "uc_ausente"
    if escolher(page, SEL_UC, valor, persona, "UC") is None:
        return "falhou"
    if escolher(page, SEL_TIPO, RE_TIPO, persona, "tipo de emissao") is None:
        return "falhou"
    if escolher(page, SEL_MOTIVO, RE_MOTIVO, persona, "motivo") is None:
        return "falhou"
    return None


def tentar(page, ctx, uc, persona=None, mes=None, ano=None, nome=None):
    """Emite a 2a via da fatura pendente da `uc`. Devolve (caminho, motivo).

    motivo: "ok", "sem_fatura", "mes_ausente", "uc_ausente", "login" ou
    "falhou". Quem chama usa isso para decidir se vai ao historico.

    Com mes/ano, so baixa se aquela competencia estiver entre as pendentes: o
    que ja foi pago nao aparece aqui, e ai o historico e que resolve.
    """
    motivo = abrir(page, uc, persona)
    if motivo:
        return None, motivo
    return emitir(page, ctx, persona, mes=mes, ano=ano, nome=nome)


def main():
    argv = sys.argv[1:]
    uc = login.UC or "".join(filter(str.isdigit, os.environ.get("EQUATORIAL_UC", "")))
    if not uc:
        log("EQUATORIAL_UC nao definido")
        return 2

    with sync_playwright() as p:
        ctx, page = login.conectar(p)
        login.escutar_dialogos(page)
        if "--sondar" in argv:
            ir_para(page, URL)
            page.wait_for_timeout(4000)
            bf.dump_estrutura(page, "segundavia-form")
            log(f"Na pagina: {na_pagina(page)} | url={page.url[:90]}")
            for sel, rotulo in ((SEL_UC, "UC"), (SEL_TIPO, "tipo"), (SEL_MOTIVO, "motivo")):
                log(f"{rotulo}: {_opcoes(page, sel)}")
            return 0

        caminho, motivo = tentar(page, ctx, uc)
        if caminho:
            log(f"PRONTO: {caminho} ({caminho.stat().st_size} bytes)")
            return 0
        log(f"Segunda via nao resolveu ({motivo}); o historico e o baixar_fatura.py")
        return 1


if __name__ == "__main__":
    sys.exit(main())
