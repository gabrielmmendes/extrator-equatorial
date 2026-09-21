#!/usr/bin/env python3
"""
Tempos e movimentos com forma humana.

Duas coisas erradas no jeito antigo, que este modulo corrige:

1. random.randint(700, 2200) e uma distribuicao UNIFORME. Pausa humana nao e
   uniforme, e log-normal: quase toda pausa e curta, e de vez em quando vem uma
   bem longa (o telefone tocou, a pessoa releu o campo). Uma serie de pausas
   uniformes tem media e variancia caracteristicas, e distinguir uniforme de
   log-normal em ~20 amostras e estatistica de primeiro semestre - exatamente o
   tipo de coisa que uma pilha de biometria comportamental faz de graca.

2. O clique acontecia no centro exato do elemento, imediatamente depois do
   movimento. Ponteiro real chega torto, passa um pouco do alvo, corrige, para
   um instante e so entao aperta.

Tudo aqui aceita uma persona (personas.py) para que o ritmo seja consistente
com o mesmo dispositivo ao longo dos meses, em vez de sorteado do zero a cada
execucao.
"""

import math
import random
import time


def _p(persona, atributo, padrao):
    return getattr(persona, atributo, padrao) if persona else padrao


def _fator(persona):
    """Quao rapida e esta persona, relativo a media (~155ms/tecla).

    Quem digita devagar tambem costuma clicar e ler devagar; deixar os dois
    sinais correlacionados e mais plausivel que sortear cada um por conta.
    """
    return _p(persona, "tecla_ms", 155) / 155.0


def pausa_s(mediana, sigma=0.55, teto=None):
    """Sorteia uma pausa log-normal em torno de `mediana` (segundos)."""
    v = random.lognormvariate(math.log(max(0.05, mediana)), sigma)
    return min(v, teto if teto else mediana * 8)


def pensar(page, persona=None, mediana=0.9, sigma=0.55, teto=None):
    """Pausa entre duas acoes: o tempo de decidir o proximo passo."""
    s = pausa_s(mediana * _fator(persona), sigma, teto)
    page.wait_for_timeout(int(s * 1000))
    return s


def _curva(x0, y0, x1, y1, passos):
    """Pontos de uma curva com desvio lateral - o ponteiro nao anda em reta.

    Bezier quadratica com o ponto de controle deslocado perpendicularmente ao
    trajeto, e velocidade com aceleracao/desaceleracao (ease-in-out): o mouse
    parte devagar, cruza rapido e freia perto do alvo.
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0
    desvio = random.uniform(-0.18, 0.18) * dist
    cx = (x0 + x1) / 2 - dy / dist * desvio
    cy = (y0 + y1) / 2 + dx / dist * desvio
    for i in range(1, passos + 1):
        t = i / passos
        t = t * t * (3 - 2 * t)  # ease-in-out
        u = 1 - t
        yield (u * u * x0 + 2 * u * t * cx + t * t * x1,
               u * u * y0 + 2 * u * t * cy + t * t * y1)


_ultimo_ponto = {}


def mover(page, x, y, persona=None):
    """Leva o ponteiro ate (x, y) por um caminho curvo, em varios passos."""
    x0, y0 = _ultimo_ponto.get(id(page), (x - random.randint(80, 400),
                                          y - random.randint(60, 300)))
    dist = math.hypot(x - x0, y - y0)
    passos = max(6, min(40, int(dist / random.uniform(12, 30))))
    for px, py in _curva(x0, y0, x, y, passos):
        page.mouse.move(px, py)
    _ultimo_ponto[id(page)] = (x, y)


def alvo_no_elemento(caixa):
    """Ponto dentro do elemento, puxado para o centro mas nunca exato.

    Cliques humanos se concentram perto do centro com dispersao gaussiana; o
    centro matematico exato, repetido, e assinatura de script.
    """
    cx = caixa["x"] + caixa["width"] / 2
    cy = caixa["y"] + caixa["height"] / 2
    x = cx + random.gauss(0, caixa["width"] / 6)
    y = cy + random.gauss(0, caixa["height"] / 6)
    x = min(max(x, caixa["x"] + 2), caixa["x"] + caixa["width"] - 2)
    y = min(max(y, caixa["y"] + 2), caixa["y"] + caixa["height"] - 2)
    return x, y


def clicar(alvo, page=None, persona=None, hesitacao=0.35):
    """Clica um Locator do jeito que uma mao clica.

    Sequencia: rola ate ver -> caminho curvo ate um ponto do elemento -> as
    vezes passa do alvo e corrige -> hover -> pausa curta -> clica.
    """
    page = page or alvo.page
    try:
        alvo.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    pensar(page, persona, 0.35, 0.5)
    caixa = alvo.bounding_box()
    if not caixa:  # elemento sem geometria: clica pelo Playwright mesmo
        alvo.click()
        return

    x, y = alvo_no_elemento(caixa)
    # Overshoot: passa do alvo e volta. Nao acontece sempre - so quando o
    # movimento foi longo o bastante para o olho errar a frenagem.
    if random.random() < 0.28:
        mover(page, x + random.uniform(-45, 45), y + random.uniform(-35, 35), persona)
        page.wait_for_timeout(random.randint(60, 200))
    mover(page, x, y, persona)
    if random.random() < hesitacao:
        pensar(page, persona, 0.45, 0.6, teto=4)
    else:
        page.wait_for_timeout(random.randint(70, 260))
    page.mouse.click(x, y, delay=random.randint(45, 130))


def ler(page, persona=None, segundos=None, mediana=None):
    """Fica na pagina interagindo, como quem esta lendo.

    reCAPTCHA v3 e o DRS pontuam permanencia e eventos de interacao. Submeter
    logo depois do load, sem scroll nem movimento, e padrao de bot ainda que o
    fingerprint do navegador seja perfeito.
    """
    if segundos is None:
        segundos = pausa_s((mediana or _p(persona, "dwell_base", 30)) * _fator(persona),
                           0.35)
    fim = time.time() + segundos
    largura = (page.viewport_size or {}).get("width", 1280)
    altura = (page.viewport_size or {}).get("height", 800)
    while time.time() < fim:
        acao = random.random()
        if acao < 0.40:
            mover(page, random.randint(60, largura - 60),
                  random.randint(60, altura - 60), persona)
        elif acao < 0.72:
            # Rolagem em varios empurroes, como roda de mouse de verdade.
            for _ in range(random.randint(1, 4)):
                page.mouse.wheel(0, random.choice([-1, 1, 1, 1]) * random.randint(60, 220))
                page.wait_for_timeout(random.randint(45, 190))
        elif acao < 0.80:
            # Parada seca: a pessoa parou de mexer e esta lendo.
            page.wait_for_timeout(int(pausa_s(2.2, 0.6, teto=12) * 1000))
        page.wait_for_timeout(int(pausa_s(1.0, 0.55, teto=8) * 1000))
    return segundos


def entre_telas(page, persona=None, mediana=2.2):
    """Pausa depois de trocar de pagina: o tempo de reconhecer a tela nova.

    Ir da pagina que carregou direto para o proximo clique, em 200ms, e o
    padrao mais facil de marcar num fluxo de varias telas - ninguem le uma
    pagina que nunca viu nesse tempo.
    """
    pensar(page, persona, mediana, 0.5, teto=mediana * 6)
    if random.random() < 0.55:
        page.mouse.wheel(0, random.randint(80, 420))
        pensar(page, persona, 0.6, 0.5)


def talvez_distrair(page, persona=None, chance=0.12):
    """De vez em quando a pessoa sai do fluxo por alguns segundos.

    Uma sessao em que TODAS as acoes avancam o objetivo, sem nenhum desvio, e
    uma sessao otimizada - e otimizacao e o que separa script de gente.
    """
    if random.random() > chance:
        return False
    page.wait_for_timeout(int(pausa_s(6, 0.7, teto=45) * 1000))
    if random.random() < 0.5:
        page.mouse.wheel(0, random.randint(-500, 500))
    return True
