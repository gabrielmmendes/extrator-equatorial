#!/usr/bin/env python3
"""
Login automatizado na Agencia Virtual da Equatorial Goias.
    https://goias.equatorialenergia.com.br/LoginGO.aspx

Camadas de defesa da pagina (todas verificadas em execucao):
  1. Imperva Incapsula        - WAF + fingerprint JS (cookies visid_incap_/nlbi_)
  2. reCAPTCHA Enterprise v3  - score invisivel, action="login", token expira ~2min
  3. Transmit Security (DRS)  - fingerprint de dispositivo -> hidden #tokenTransmit

Nenhum desses tokens e forjavel por HTTP puro: dependem de JS lendo o ambiente
real do navegador. Por isso dirigimos um Chrome de verdade via CDP (ver
navegador.py) em vez de deixar o Playwright lancar o browser, o que
acrescentaria --enable-automation e afins.

Este modulo faz UMA tentativa e diz o que aconteceu (tentar_login devolve
(ok, motivo)). Quem decide se e quando repetir e a agenda.py, em escala de
horas ou dias. O laco de horas que existia aqui saiu junto com a premissa que
o sustentava - ver o cabecalho de agenda.py.
"""

import os
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import humano as H

URL = "https://goias.equatorialenergia.com.br/LoginGO.aspx"
URL_HOME = "https://goias.equatorialenergia.com.br/"
AQUECER = os.environ.get("EQUATORIAL_AQUECER", "1") != "0"
CDP = f"http://127.0.0.1:{os.environ.get('CDP_PORT', '9222')}"
OUT = Path(__file__).parent / "saida"

UC = "".join(filter(str.isdigit, os.environ.get("EQUATORIAL_UC", "")))
DOC = "".join(filter(str.isdigit, os.environ.get("EQUATORIAL_DOC", "")))
NASC = os.environ.get("EQUATORIAL_NASC", "").strip()  # DD/MM/AAAA
# OBSOLETOS no fluxo do orquestrador.py, mantidos porque baixar_fatura.coletar()
# e o coletar.ps1 (caminho de UC unica e imediata) ainda os leem.
#
# Eles vem da premissa antiga - "a recusa e independente entre tentativas, entao
# vale metralhar" -, que o teste manual derrubou: um login deu certo e o
# seguinte, mesmo navegador e mesmo IP, foi recusado. Repetir a cada 20-45s nao
# aumenta a chance, alimenta a contagem de velocity. No fluxo novo quem manda e
# agenda.BACKOFF_H, em horas.
ESPERA_BASE = int(os.environ.get("EQUATORIAL_ESPERA", "20"))
ESPERA_MAX = int(os.environ.get("EQUATORIAL_ESPERA_MAX", "45"))
PRAZO = int(os.environ.get("EQUATORIAL_PRAZO", "21600"))
# Fallback de permanencia: so vale quando nao ha persona. Com persona, o tempo
# vem de persona.dwell_base, que e estavel para aquele dispositivo.
DWELL = int(os.environ.get("EQUATORIAL_DWELL", "40"))
ROTACIONAR = os.environ.get("EQUATORIAL_ROTACIONAR", "0") == "1"
DOMINIO = "goias.equatorialenergia.com.br"

SEL_UC = "#WEBDOOR_headercorporativogo_txtUC"
SEL_DOC = "#WEBDOOR_headercorporativogo_txtDocumento"
SEL_ENTRAR = 'button.button[onclick*="ValidarCamposAreaLogada"]'
SEL_OK_REAL = "#WEBDOOR_headercorporativogo_okReal"
SEL_TK_RECAPTCHA = "#WEBDOOR_headercorporativogo_Recaptchav3_hfToken"
SEL_TK_TRANSMIT = "#tokenTransmit"
URL_FATURAS = ("https://goias.equatorialenergia.com.br/AgenciaGO/"
               "Servi%C3%A7os/comum/HistoricoFaturas.aspx")
SEL_DATA = "#WEBDOOR_headercorporativogo_txtData"
SEL_VALIDAR = "#WEBDOOR_headercorporativogo_btnValidar"
RECAPTCHA_KEY = "6LfWFzkoAAAAAAo7_vWKW7DIcigZYn0ec9ZV_43E"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def texto_da_pagina(page):
    """innerText do body, ou "" se ainda nao ha body.

    document.body e null em about:blank, em chrome-error:// e no intervalo entre
    o commit da navegacao e o parse do HTML. Ler direto dali estoura com
    "Cannot read properties of null" em momentos que sao normais, sobretudo logo
    depois de uma troca de IP.
    """
    try:
        return page.evaluate("document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def digitar(page, seletor, texto, persona=None):
    """Digita tecla por tecla, no ritmo da persona.

    Obrigatorio: os dois campos tem onpaste="return false" e aplicam mascara via
    onkeypress/onkeydown. page.fill() escreve direto no DOM sem disparar esses
    eventos, entao a mascara nao roda e a validacao do site falha.

    O ritmo vem da persona (personas.py) e nao de um sorteio novo a cada
    execucao. Um dispositivo cujo dono digita a 100ms/tecla hoje e a 200ms
    amanha e menos plausivel que um que digita sempre parecido: a biometria de
    digitacao do Transmit compara justamente com o historico do proprio device
    ID, entao consistencia vale mais que lentidao.
    """
    base = persona.tecla_ms if persona else 115
    desvio = persona.tecla_desvio if persona else 55
    taxa_erro = persona.taxa_erro if persona else 0.0

    campo = page.locator(seletor)
    campo.scroll_into_view_if_needed()
    caixa = campo.bounding_box()
    if caixa:  # caminho curvo ate o campo, nao teletransporte do cursor
        x, y = H.alvo_no_elemento(caixa)
        H.mover(page, x, y, persona)
        H.pensar(page, persona, 0.3, 0.5)
        page.mouse.click(x, y, delay=random.randint(45, 120))
    else:
        campo.click()
    H.pensar(page, persona, 0.35, 0.5)
    for i, ch in enumerate(texto):
        # Erro e correcao: humano digitando numero longo troca digito e apaga.
        # So faz sentido em campo numerico, entao sorteamos outro digito.
        if taxa_erro and i > 1 and random.random() < taxa_erro:
            errado = random.choice([d for d in "0123456789" if d != ch])
            page.keyboard.type(errado, delay=max(40, int(random.gauss(base, desvio))))
            page.wait_for_timeout(random.randint(180, 600))  # percebe o erro
            page.keyboard.press("Backspace")
            page.wait_for_timeout(random.randint(120, 400))
        atraso = max(40, int(random.gauss(base, desvio)))
        # Pausa maior nas fronteiras da mascara, onde o olho confere o campo.
        if i and i % 3 == 0 and random.random() < 0.25:
            atraso += random.randint(150, 700)
        page.keyboard.type(ch, delay=atraso)
    page.wait_for_timeout(random.randint(200, 500))


def permanecer(page, segundos=None, persona=None):
    """Fica na pagina interagindo, como um usuario que acabou de chegar.

    Nao e teatro: reCAPTCHA v3 e o DRS do Transmit pontuam tempo de permanencia
    e eventos de interacao. Submeter 7s depois do load, sem nenhum scroll ou
    movimento de mouse, e um padrao de bot ainda que o fingerprint do navegador
    seja perfeito.
    """
    return H.ler(page, persona, segundos=segundos)


def aquecer(page, persona=None):
    """Navega um pouco pelo site antes de ir ao login.

    Aterrissar direto em LoginGO.aspx, submeter e sumir e um padrao pobre de
    sinais: reCAPTCHA v3 pontua o historico de interacao *dentro da sessao*, e
    tanto o Imperva quanto o DRS do Transmit observam a sequencia de navegacao.
    Chegar pela home, ler um pouco e so entao ir ao login e o percurso que um
    cliente faz de verdade - e rende cookies e telemetria coerentes com ele.
    """
    try:
        log("Aquecendo a sessao pela home")
        page.goto(URL_HOME, wait_until="domcontentloaded", timeout=45_000)
        H.entre_telas(page, persona)
        permanecer(page, H.pausa_s(9, 0.5, teto=45), persona)

        # segue um link interno qualquer, como quem estava navegando
        passos = persona.passos_aquecimento if persona else 1
        for _ in range(passos):
            internos = page.evaluate("""() => [...document.querySelectorAll('a')]
                .map(a => a.href)
                .filter(h => h && h.includes('equatorialenergia.com.br')
                          && !/Login|logout|sair/i.test(h))
                .slice(0, 40)""")
            if not internos:
                break
            destino = random.choice(internos)
            log(f"Visitando {destino[:70]}")
            page.goto(destino, wait_until="domcontentloaded", timeout=45_000)
            H.entre_telas(page, persona)
            permanecer(page, H.pausa_s(8, 0.55, teto=40), persona)
    except PWTimeout:
        log("Aquecimento incompleto (timeout); seguindo para o login")
    except Exception as e:
        log(f"Aquecimento falhou ({str(e)[:60]}); seguindo para o login")


def formatar_documento(digitos):
    if len(digitos) == 11:
        return f"{digitos[:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:]}"
    if len(digitos) == 14:
        return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"
    return digitos


def normalizar_documento(page, digitos):
    """Corrige o separador quando a mascara do site erra.

    mascaraMutuario dispara setTimeout(...,1) no onkeydown, ou seja, roda 1ms
    depois da tecla e as vezes antes do caractere entrar no campo. Digitando
    incrementalmente isso produz "123.456.789.01" - ponto onde deveria haver
    hifen -, e a versao de cpfCnpj ativa na pagina preserva separadores ja
    existentes, entao ela nunca se auto-corrige.
    """
    esperado = formatar_documento(digitos)
    if page.input_value(SEL_DOC) == esperado:
        return esperado, False
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [SEL_DOC, esperado],
    )
    return esperado, True


def esperar_transmit_pronto(page, timeout_s=20):
    """Espera o SDK do Transmit existir no escopo global.

    O site inicializa no evento load do proprio script:
        getElementById("ts-platform-script").addEventListener("load",
            () => tsPlatform.initialize({clientId: "..."}))
    initialize() e assincrona; clicar em Entrar antes dela terminar faz
    triggerActionEvent rejeitar - e ValidarCamposAreaLogada nao tem .catch(),
    entao o login morre em silencio, sem erro na tela e sem postback.

    Testar prontidao de verdade exigiria disparar um evento (o unico caminho que
    resolve e o proprio triggerActionEvent), o que sujaria a telemetria. Entao
    checamos so a presenca da API e deixamos submeter() cuidar do resto: ele tem
    o fallback com o .catch() que falta na pagina.
    """
    try:
        page.wait_for_function(
            "typeof window.tsPlatform?.drs?.triggerActionEvent === 'function'",
            timeout=timeout_s * 1000,
        )
        return True
    except PWTimeout:
        return False


def renovar_recaptcha(page):
    """Gera um token v3 novo imediatamente antes do submit.

    O token nasce no load da pagina e vale ~2 minutos. Com digitacao humanizada
    e a espera do SDK, o token original ja expirou na hora do submit - e o erro
    que volta e generico, dificil de diagnosticar.
    """
    return page.evaluate(
        """([key, sel]) => new Promise((res) => {
            if (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {
                return res('grecaptcha ausente');
            }
            grecaptcha.enterprise.ready(() => {
                grecaptcha.enterprise.execute(key, { action: 'login' })
                    .then((t) => { document.querySelector(sel).value = t; res(`ok (${t.length} chars)`); })
                    .catch((e) => res('erro: ' + e));
            });
        })""",
        [RECAPTCHA_KEY, SEL_TK_RECAPTCHA],
    )


def submeter(page, persona=None):
    """Clica em Entrar e garante que o postback saia.

    Caminho primario: o botao real, que executa ValidarCamposAreaLogada ->
    Transmit -> preenche #tokenTransmit -> setTimeout(2000) -> clica no okReal
    oculto. Deixar a cadeia nativa rodar e mais fiel do que montar o POST na mao.

    Fallback: se o token nao aparecer, refaz o que a pagina faria - com o .catch()
    que ela nao tem - e dispara o postback.
    """
    H.clicar(page.locator(SEL_ENTRAR).first, page, persona)
    try:
        page.wait_for_function(
            f"document.querySelector('{SEL_TK_TRANSMIT}')?.value?.length > 0",
            timeout=12_000,
        )
        log("Token Transmit preenchido pelo fluxo nativo")
        return "nativo"
    except PWTimeout:
        pass

    log("Fluxo nativo nao preencheu o token; aplicando fallback")
    r = page.evaluate(
        """([selTk, uc]) => new Promise((res) => {
            window.tsPlatform.drs.triggerActionEvent("logingo", { claimedUserId: uc })
                .then((resp) => {
                    document.querySelector(selTk).value = resp.actionToken;
                    res('ok');
                })
                .catch((e) => res('erro: ' + e));
            setTimeout(() => res('timeout'), 20000);
        })""",
        [SEL_TK_TRANSMIT, page.input_value(SEL_UC)],
    )
    if r != "ok":
        return f"falhou ({r})"
    page.wait_for_timeout(random.randint(1200, 2200))  # o site espera 2s aqui
    page.evaluate(f"document.querySelector('{SEL_OK_REAL}').click()")
    return "fallback"


def esperar_postback(page, pronto=None, timeout_s=45, rotulo="postback"):
    """Espera o postback do WebForms entregar a tela nova. Devolve os segundos.

    Substitui wait_for_load_state("networkidle"), que era o maior desperdicio do
    ciclo: a pagina carrega Imperva, reCAPTCHA v3 e Transmit, e os tres mantem
    beacon periodico, entao a rede nunca fica ociosa por 500ms seguidos e o
    networkidle so entregava o timeout inteiro - 45s por UC sem nada em troca.

    O que interessa nao e a rede parar, e o conteudo novo aparecer. `pronto` e
    um predicado sem argumentos que responde isso; sem ele esperamos so o
    documento carregar. O tempo gasto vai para o log porque e a unica forma de
    ver, numa corrida real, se a espera esta dimensionada.

    Esperar nao e um sinal que o site observe: quem nos ve e a telemetria de
    interacao, e ela vem das pausas de humano.py, que continuam intactas.
    """
    inicio = time.time()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_s * 1000)
    except PWTimeout:
        pass
    if pronto is not None:
        limite = inicio + timeout_s
        while time.time() < limite:
            try:
                if pronto():
                    break
            except Exception:
                # Documento trocando debaixo dos pes no meio do postback: nao e
                # resposta, so tentar de novo no proximo giro.
                pass
            page.wait_for_timeout(250)
    gasto = time.time() - inicio
    log(f"({rotulo} respondeu em {gasto:.1f}s)")
    return gasto


def area_logada(page, timeout_s=20):
    """Confirma autenticacao pelo que so existe na area logada.

    Checar a URL nao serve: o site responde 200 na pagina de faturas mesmo
    deslogado, devolvendo o formulario de login no lugar do conteudo. E o menu
    da area logada demora a renderizar, entao insistimos por alguns segundos
    antes de concluir que nao estamos autenticados.
    """
    limite = time.time() + timeout_s
    while time.time() < limite:
        try:
            if page.locator(SEL_UC).count() == 0 and page.locator(SEL_DATA).count() == 0:
                texto = texto_da_pagina(page)
                if "Sair" in texto and ("Meus dados" in texto or "Fornecimento" in texto):
                    return True
        except Exception as e:
            # Documento trocando debaixo dos pes (postback, erro de rede,
            # redirect). Nao e resposta sobre estar logado ou nao - so tentar de
            # novo no proximo giro.
            log(f"(area_logada: pagina instavel, {str(e)[:60]})")
        # 300ms e nao 1s: o menu da area logada leva uns segundos para montar, e
        # cada giro perdido aqui e tempo de relogio no caminho critico de toda
        # UC. O custo de olhar mais vezes e nulo - e leitura do DOM local.
        page.wait_for_timeout(300)
    return False


def etapa2(page, dialogos, persona=None):
    """Segunda etapa: data de nascimento do titular.

    Bem mais simples que a primeira - sem Transmit, sem mascara JS. So o campo
    de data e um postback direto. O reCAPTCHA e o mesmo hidden field, e como ja
    se passou mais de um minuto desde a etapa 1, o token precisa ser renovado.
    """
    if not NASC:
        log("EQUATORIAL_NASC nao definido - pare aqui e informe a data do titular")
        return False

    log("Preenchendo data de nascimento")
    digitar(page, SEL_DATA, NASC, persona)
    valor = page.input_value(SEL_DATA)
    log(f"Data no campo: {valor}")

    log(f"reCAPTCHA v3 (etapa 2): {renovar_recaptcha(page)}")
    H.clicar(page.locator(SEL_VALIDAR).first, page, persona)

    # Pronto = o campo de data sumiu (a etapa passou) ou o site gritou por
    # alert(). Qualquer um dos dois encerra a espera; nao ha terceiro desfecho.
    esperar_postback(
        page,
        lambda: bool(dialogos) or page.locator(SEL_DATA).count() == 0,
        rotulo="etapa 2")
    page.wait_for_timeout(3000)
    return not dialogos


def conectar(p):
    """Conecta ao Chrome ja em execucao (chrome.sh) e devolve (ctx, page)."""
    try:
        browser = p.chromium.connect_over_cdp(CDP)
    except Exception as e:
        sys.exit(f"Nao consegui conectar em {CDP}: {e}\nRode ./chrome.sh primeiro.")
    ctx = browser.contexts[0]
    page = next((pg for pg in ctx.pages if "equatorial" in pg.url), None)
    if page is None:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def escutar_dialogos(page):
    """O site reporta falha de login via alert(), e o Playwright descarta
    dialogos por padrao - sem este handler o erro fica invisivel e a execucao
    parece ter "simplesmente nao funcionado"."""
    dialogos = []

    def _dialogo(d):
        dialogos.append(d.message)
        log(f"ALERTA DO SITE: {d.message}")
        d.accept()

    page.on("dialog", _dialogo)
    return dialogos


def classificar_falha(page, dialogos):
    """Diz POR QUE a tentativa falhou. O motivo determina o castigo.

    Sem isso tudo virava "recusa" e recebia a mesma pausa de 20-45s - inclusive
    o challenge do Imperva, onde insistir e a pior acao possivel, e o timeout de
    rede, onde nao ha nada a punir. agenda.py aplica backoff por motivo:
    minutos para 'rede', horas para 'backend', dias para 'imperva'.
    """
    msg = " ".join(dialogos)[:400]
    corpo = texto_da_pagina(page)[:4000]
    tudo = f"{msg} {corpo}"

    if re.search(r"Incapsula|Request unsuccessful|_Incapsula_Resource"
                 r"|Access Denied|Error code: 1[0-9]{2}", tudo, re.I):
        return "imperva"
    if re.search(r"#\s*0*\d+\s*-\s*N[aã]o foi poss[ií]vel realizar o login", tudo, re.I):
        return "backend"
    if re.search(r"captcha|rob[oô]|verifica[cç][aã]o de seguran[cç]a", tudo, re.I):
        return "captcha"
    if re.search(r"ERR_(NETWORK|INTERNET|CONNECTION|NAME_NOT|TIMED)", tudo, re.I):
        return "rede"
    if re.search(r"inv[aá]lid|incorret|n[aã]o (confere|localizada|encontrada)", tudo, re.I):
        return "credencial"
    return "desconhecido"


# Um matcher so, usado tanto para localizar quanto para clicar: manter duas
# copias da regra em strings JS separadas e o tipo de duplicacao que sai do ar
# silenciosamente quando o site muda o rotulo do link.
_JS_ACHADOS = """
    const alvos = [...document.querySelectorAll(
        'a, button, input[type=button], input[type=submit]')];
    const casa = (el) => {
        const t = ((el.innerText || el.value || '') + ' ' +
                   (el.getAttribute('href') || '') + ' ' +
                   (el.getAttribute('onclick') || '') + ' ' +
                   (el.id || '') + ' ' + (el.title || '')).toLowerCase();
        return /(^|[^a-z])sair([^a-z]|$)|logout|logoff|log_off|encerrar\\s*sess/.test(t);
    };
    const achados = alvos.filter(casa);
"""

ACHAR_SAIR = """() => {""" + _JS_ACHADOS + """
    if (!achados.length) return null;
    // Preferir o visivel: clicar nele gera a telemetria de um clique real.
    const visivel = achados.find(el => el.offsetParent !== null);
    const el = visivel || achados[0];
    const c = el.getBoundingClientRect();
    return {
        visivel: !!visivel,
        txt: (el.innerText || el.value || '').trim().slice(0, 40),
        href: (el.getAttribute('href') || '').slice(0, 160),
        caixa: visivel ? {x: c.x, y: c.y, width: c.width, height: c.height} : null,
    };
}"""


def sair(page, persona=None):
    """Encerra a sessao pelo proprio site.

    Limpar cookie no cliente derruba a sessao aqui, mas nao no servidor: a
    sessao ASP.NET fica pendurada ate expirar, e uma conta acumulando sessoes
    abertas e um dos sinais mais baratos que existem. Deslogar tambem e o que um
    cliente faz.

    Nao da para usar Locator.click(): o "Sair" mora num menu de cabecalho que o
    Playwright considera nao-acionavel, e a chamada gastava 30s ate estourar o
    timeout sem deslogar nada. Entao: se o elemento estiver visivel, clique
    humano normal; se nao, clique via JS, que ignora a heuristica de
    acionabilidade. O sucesso e conferido pelo campo de UC voltar a existir -
    ele so aparece deslogado.
    """
    try:
        alvo = page.evaluate(ACHAR_SAIR)
    except Exception as e:
        log(f"(nao localizei o Sair: {str(e)[:60]})")
        return False
    if not alvo:
        log("(link Sair nao encontrado na pagina)")
        return False

    try:
        if alvo["visivel"] and alvo["caixa"]:
            x, y = H.alvo_no_elemento(alvo["caixa"])
            H.mover(page, x, y, persona)
            H.pensar(page, persona, 0.5, 0.5)
            page.mouse.click(x, y, delay=random.randint(45, 120))
        else:
            page.evaluate("() => {" + _JS_ACHADOS +
                          "if (achados.length) achados[0].click(); }")
        page.wait_for_selector(SEL_UC, timeout=15_000)
        log(f"Sessao encerrada ({alvo['txt'] or alvo['href'] or 'Sair'})")
        return True
    except PWTimeout:
        log("(cliquei em Sair mas a pagina de login nao voltou)")
        return False
    except Exception as e:
        log(f"(nao consegui deslogar: {str(e)[:70]})")
        return False


def uma_tentativa_de_login(page, dialogos, persona=None):
    """Executa o fluxo de login uma vez. True se terminou na area logada."""
    dialogos.clear()

    # Sempre recarrega: reaproveitar a aba deixa campos preenchidos de uma
    # execucao anterior, e o maxlength esconde a duplicacao silenciosamente.
    if AQUECER:
        aquecer(page, persona)

    ir_para(page, URL)
    page.bring_to_front()
    page.wait_for_selector(SEL_UC, timeout=30_000)
    H.entre_telas(page, persona)

    if not esperar_transmit_pronto(page):
        log("AVISO: SDK do Transmit nao apareceu; seguindo assim mesmo")

    # Permanencia com base na persona, nao num numero fixo do modulo: cada
    # dispositivo tem o proprio tempo tipico de leitura da pagina.
    base = persona.dwell_base if persona else DWELL
    permanecer(page, max(8, int(random.gauss(base, base * 0.25))))
    digitar(page, SEL_UC, UC, persona)
    digitar(page, SEL_DOC, DOC, persona)
    page.keyboard.press("Tab")  # dispara o onblur do campo de documento
    H.pensar(page, persona, 0.7, 0.5)
    H.talvez_distrair(page, persona)

    valor, corrigido = normalizar_documento(page, DOC)
    log(f"Documento: {valor}" + (" (separador corrigido)" if corrigido else ""))
    valor_uc = page.input_value(SEL_UC)
    if "".join(filter(str.isdigit, valor_uc)) != UC:
        sys.exit(f"UC inconsistente no campo: {valor_uc!r} (esperado {UC})")
    # Pausa antes de Entrar: a conferida final nos dois campos.
    permanecer(page, H.pausa_s(6.5, 0.5, teto=30), persona)
    log(f"UC: {valor_uc} | reCAPTCHA: {renovar_recaptcha(page)}")
    log(f"Submetendo (via {submeter(page, persona)})")

    # Etapa 1 aceita: o site troca o formulario pelo campo de data de
    # nascimento. Mas a sessao ja esta autenticada nesse ponto, entao da para ir
    # direto a area logada e pular a segunda etapa.
    try:
        page.wait_for_selector(SEL_DATA, timeout=25_000)
        log("Etapa 1 aceita (site ofereceu a data de nascimento)")
    except PWTimeout:
        if dialogos:
            log(f"Recusado pelo backend: {dialogos[-1]}")
            return False
        log("Campo de data nao apareceu, mas tambem nao houve alerta")

    H.entre_telas(page, persona, mediana=3.0)
    ir_para(page, URL_FATURAS)
    if area_logada(page):
        return True

    log("Area logada recusada; tentando a segunda etapa (data de nascimento)")
    try:
        page.go_back(wait_until="domcontentloaded")
        page.wait_for_selector(SEL_DATA, timeout=20_000)
        etapa2(page, dialogos, persona)
        ir_para(page, URL_FATURAS)
        return area_logada(page)
    except PWTimeout:
        log("Nao foi possivel retomar a etapa da data de nascimento")
        return False


def preparar_rede():
    """Poe a rota padrao no celular, se a rotacao estiver ligada.

    Devolve o estado a passar para liberar_rede(). Quem chama e responsavel por
    liberar num finally - deixar a maquina roteando tudo pelo 4G depois que o
    robo termina seria bem pior que nao rotacionar nada.
    """
    if not ROTACIONAR:
        return None
    if os.environ.get("EQUATORIAL_PROXY"):
        # Este processo ja sai por um adaptador escolhido a dedo (o SOCKS do
        # coletar_paralelo.py amarra o socket). Tomar a rota padrao aqui daria a
        # maquina inteira a UM celular, e os outros workers continuariam certos
        # so por sorte do roteamento.
        log("Saida por proxy amarrado; nao mexo na rota padrao")
        return None
    import rede

    tether = rede.detectar_tether()
    log(f"Tethering: {tether['nome']} ({tether['descricao']})")
    try:
        estado = rede.assumir_rota(tether["ifIndex"])
    except RuntimeError as e:
        # assumir_rota (metrica 1) exige admin. Sem elevacao, seguimos se o
        # trafego ja sai pelo tether - o adaptador USB costuma ter metrica
        # automatica menor que o Wi-Fi, virando rota padrao sozinho. A rotacao
        # de IP em si (adb) nao precisa de admin.
        log(f"Sem gerenciar a rota ({str(e)[:70]})")
        estado = None
    log(f"Saindo pelo IP {rede.ip_publico()}")
    return estado


def liberar_rede(estado):
    if estado is None:
        return
    import rede

    rede.restaurar_rota(estado)


def ir_para(page, url, tentativas=3):
    """page.goto() tolerante a troca de interface de rede.

    Depois de uma rotacao de IP o Chrome ainda tem sockets presos na interface
    antiga e o primeiro carregamento volta ERR_NETWORK_CHANGED / ERR_NETWORK_IO.
    Nao e falha do site: e so esperar o proximo carregamento.
    """
    for n in range(1, tentativas + 1):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception as e:
            msg = str(e)[:80]
            if n == tentativas:
                raise
            log(f"goto falhou ({msg}); retentando {n + 1}/{tentativas}")
            page.wait_for_timeout(4000)
    return False


def trocar_ip(page, ctx):
    """Rotaciona o IP do celular e reseta o rastro de sessao ligado ao antigo.

    Limpar os cookies do dominio e obrigatorio, nao higiene: visid_incap_/nlbi_/
    reese84 foram emitidos para o IP anterior, e reapresenta-los vindo de um IP
    novo e justamente a incoerencia que o Imperva procura. aquecer() reconstroi
    a sessao do zero na tentativa seguinte.
    """
    import rede  # so no Windows e so quando pedido; nao virar dependencia dura

    try:
        antes, depois = rede.rotacionar()
    except RuntimeError as e:
        log(f"Rotacao de IP falhou: {str(e)[:140]}")
        return False

    if ctx is not None:
        try:
            ctx.clear_cookies(domain=DOMINIO)
            log(f"Cookies de {DOMINIO} limpos")
        except TypeError:
            ctx.clear_cookies()  # Playwright antigo nao filtra por dominio
            log("Cookies limpos (sem filtro de dominio)")
        except Exception as e:
            log(f"AVISO: nao limpei os cookies: {str(e)[:80]}")
    return depois != antes


def tentar_login(page, dialogos, persona=None, sessao_possivel=True):
    """UMA passada pelo fluxo de login. Devolve (ok, motivo).

    `sessao_possivel=False` pula a sonda de sessao anterior. Vale sempre que
    quem chama sabe que nao ha sessao a herdar - Chrome recem-subido cujo ultimo
    login foi ha mais de 20 min, que e o caso normal do orquestrador. A sonda
    custava um page load mais os 8s do laco de area_logada em TODA UC para
    descobrir o que ja se sabia. Pular tambem melhora o percurso: sem ela a
    sessao comeca pela home (aquecer), como um visitante, em vez de bater
    deslogado numa URL de area logada.

    Por que uma so, e nao o laco de 6h que existia aqui: o laco antigo partia da
    hipotese de que as recusas eram independentes, entao metralhar pagava. O
    teste manual mostrou o contrario - um login deu certo e o seguinte, mesmo
    navegador e mesmo IP, foi recusado; a mesma UC entrou de primeira em outro
    dispositivo. Se as tentativas nao sao independentes, repetir rapido nao
    aumenta a chance: reduz, porque cada repeticao alimenta a contagem de
    velocity que gerou o bloqueio.

    Quem decide se e quando repetir e a agenda.py, em escala de horas ou dias,
    com backoff por motivo. Este modulo so faz a tentativa e diz o que houve.
    """
    if sessao_possivel:
        log("Verificando se a sessao anterior ainda vale")
        ir_para(page, URL_FATURAS)
        if area_logada(page, timeout_s=8):
            log("Sessao ainda valida - login desnecessario")
            return True, "sessao"

    try:
        if uma_tentativa_de_login(page, dialogos, persona):
            achado = re.search(
                r"^\s*([A-ZÁÂÃÀÉÊÍÓÔÕÚÇ][A-ZÁÂÃÀÉÊÍÓÔÕÚÇ ]{6,60})\s*$",
                texto_da_pagina(page), re.M)
            titular = achado.group(1).strip() if achado else None
            log(f"Login OK{f' - {titular}' if titular else ''}")
            return True, "ok"
    except PWTimeout as e:
        log(f"Timeout no login: {str(e)[:90]}")
        dialogos.append(str(e)[:200])
    except Exception as e:
        log(f"Erro no login: {type(e).__name__}: {str(e)[:90]}")
        dialogos.append(f"{type(e).__name__}: {str(e)[:200]}")

    motivo = classificar_falha(page, dialogos)
    log(f"Login recusado - motivo classificado: {motivo}")
    return False, motivo


# Quantas passadas por execucao. 1 e o padrao e o recomendado; 2 so faz sentido
# quando o motivo da primeira foi 'rede' (nao houve recusa, houve queda).
TENTATIVAS = int(os.environ.get("EQUATORIAL_TENTATIVAS", "1"))


def garantir_login(page, dialogos, ctx=None, prazo_s=PRAZO, persona=None):
    """Compatibilidade: chama tentar_login e devolve so o booleano.

    Mantida para baixar_fatura.py e coletar_varias.py continuarem funcionando.
    Note que ela NAO insiste mais por horas - retenta no maximo TENTATIVAS
    vezes, e so quando a falha foi de rede.
    """
    global ULTIMO_MOTIVO
    for n in range(1, max(1, TENTATIVAS) + 1):
        ok, motivo = tentar_login(page, dialogos, persona)
        ULTIMO_MOTIVO = motivo
        if ok:
            return True
        if motivo != "rede" or n == TENTATIVAS:
            return False
        espera = random.randint(45, 120)
        log(f"Falha de rede; nova passada em {espera}s ({n + 1}/{TENTATIVAS})")
        page.wait_for_timeout(espera * 1000)
    return False


ULTIMO_MOTIVO = None


def main():
    if not UC or not DOC:
        sys.exit(
            "Defina as credenciais antes de rodar:\n"
            "  export EQUATORIAL_UC=...    # unidade consumidora\n"
            "  export EQUATORIAL_DOC=...   # CPF/CNPJ"
        )
    OUT.mkdir(exist_ok=True)
    estado_rede = preparar_rede()
    try:
        with sync_playwright() as p:
            ctx, page = conectar(p)
            dialogos = escutar_dialogos(page)
            sucesso = garantir_login(page, dialogos, ctx=ctx)

            carimbo = time.strftime("%Y%m%d-%H%M%S")
            page.screenshot(path=str(OUT / f"login-{carimbo}.png"), full_page=True)
            log("RESULTADO: login OK" if sucesso else "RESULTADO: nao consegui logar")
            if sucesso:
                ctx.storage_state(path=str(OUT / "sessao.json"))
            return 0 if sucesso else 1
    finally:
        liberar_rede(estado_rede)


if __name__ == "__main__":
    sys.exit(main())
