#!/usr/bin/env python3
"""
Sobe e derruba um Chrome real por persona.

Substitui o chrome.ps1 no fluxo automatico. A diferenca que importa nao e de
comodidade: como cada persona tem o proprio --user-data-dir, trocar de conta
passa a ser trocar de PROCESSO, e nao limpar cookie dentro do mesmo navegador.
Isso troca cookie, localStorage, IndexedDB, service worker e cache juntos - que
e a unica forma coerente de "ser outro dispositivo". Ver personas.py.

Encerrar direito tambem e requisito, nao higiene: o Chrome so grava o LevelDB de
localStorage/IndexedDB no shutdown limpo. Um taskkill /F perde o device ID do
Transmit gravado na sessao, e a proxima visita da mesma persona chega sem ele -
de novo o padrao de dispositivo novo que estamos tentando evitar.
"""

import json
import os
import random
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import humano as H
import personas

RAIZ = Path(__file__).parent

CAMINHOS_CHROME = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

# Passeio de maturacao de um perfil novo. Nao e enfeite: o score do reCAPTCHA v3
# depende fortemente do historico de cookies do Google no navegador, e um perfil
# zerado nasce com score baixo por definicao. Alem disso Imperva e Transmit
# pontuam idade de dispositivo. Um perfil que so conhece o site da Equatorial e,
# ele proprio, uma anomalia.
# gov.br saiu da lista: reseta a conexao para esta rede (ERR_CONNECTION_RESET,
# e o curl tambem nao passa), entao so gastava tempo. Alem disso ele roda o
# proprio WAF - insistir contra um WAF durante a maturacao e o oposto do
# objetivo, que e construir historico banal.
SITES_MATURACAO = [
    "https://www.google.com/",
    "https://www.google.com/search?q=segunda+via+conta+de+luz",
    "https://www.google.com/search?q=tarifa+de+energia+goias",
    "https://g1.globo.com/",
    "https://www.uol.com.br/",
    "https://www.climatempo.com.br/",
    "https://pt.wikipedia.org/wiki/Energia_el%C3%A9trica",
    "https://www.terra.com.br/",
]
# Abaixo disto o perfil nao ganhou historico que valha o nome, e marca-lo como
# maturado seria pior que nao maturar: ele estrearia na Equatorial com a
# etiqueta de pronto e o score de recem-nascido.
PARADAS_MINIMAS = 2

# Proxy SOCKS local por onde este processo faz o Chrome sair. Vazio no modo de
# sempre (a saida e a rota padrao da maquina); preenchido pelo worker do
# coletar_paralelo.py, que sobe um proxy_local.ProxyLocal amarrado ao adaptador
# do celular dele. E o que permite N Chromes saindo por N celulares ao mesmo
# tempo, sem ninguem disputar a rota padrao.
PROXY = os.environ.get("EQUATORIAL_PROXY", "")
# So vale junto do PROXY. Ligado por padrao porque o vazamento que fecha
# anularia a feature inteira (ver _fechar_vazamento_webrtc).
SEM_VAZAMENTO_WEBRTC = os.environ.get("EQUATORIAL_WEBRTC", "0") != "1"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] nav: {msg}", flush=True)


def achar_chrome():
    for c in CAMINHOS_CHROME:
        p = Path(os.path.expandvars(c))
        if p.is_file():
            return str(p)
    raise RuntimeError("chrome.exe nao encontrado nos caminhos padrao.")


def porta_livre(inicio=None):
    """Porta CDP livre. Fixa por execucao, nao por persona: a porta nao vaza
    para a pagina, entao nao ha ganho em varia-la.

    A base sai de EQUATORIAL_PORTA_BASE porque connect_ex so responde que
    ninguem escuta AGORA: quatro workers varrendo a partir de 9222 no mesmo
    instante escolhem a mesma porta, e entre a checagem e o Chrome subir cabe o
    vizinho inteiro. Uma base por worker resolve sem trava nenhuma.
    """
    inicio = inicio or int(os.environ.get("EQUATORIAL_PORTA_BASE", "9222"))
    for p in range(inicio, inicio + 60):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("nenhuma porta livre para o CDP")


def cdp_no_ar(porta, timeout=1.5):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{porta}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _destrancar(persona):
    """Remove o lock de instancia unica deixado por um encerramento anterior.

    Quando o Chrome nao sai por conta propria, esses arquivos ficam no perfil e
    a proxima execucao acredita que ja existe uma instancia dona dele: em vez de
    abrir o navegador, ela repassa a URL para a instancia "viva" e encerra. O
    sintoma e exatamente o que aparece aqui - o processo sobe, o perfil e
    tocado, e o CDP nunca responde.

    So e seguro porque e chamado depois de confirmar que nao ha processo algum
    usando o perfil.
    """
    if _pids_do_perfil(persona):
        return
    for nome in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        alvo = persona.dir / nome
        try:
            if alvo.exists() or alvo.is_symlink():
                alvo.unlink()
                log(f"{persona.apelido}: lock orfao removido ({nome})")
        except OSError:
            pass


def _fechar_vazamento_webrtc(persona):
    """Impede o WebRTC de revelar o IP da rota padrao, por preferencia do perfil.

    O SOCKS5 nao carrega UDP (o proxy recusa UDP ASSOCIATE), entao o STUN do
    WebRTC sai DIRETO pela rota padrao e devolve um candidato srflx com o IP do
    Wi-Fi - o MESMO nos quatro workers. Seria entregar de graca justamente a
    correlacao que a rotacao de IP existe para quebrar.

    A flag de linha de comando nao resolve. Medido nesta maquina, com o proxy
    amarrado ao Wi-Fi e o Chrome atual:

      sem nada                                  srflx = 45.191.204.245
      --force-webrtc-ip-handling-policy=...     srflx = 45.191.204.245
      pref webrtc.ip_handling_policy no perfil  srflx = NENHUM

    Por isso a preferencia, e nao a flag: a flag virou no-op em algum ponto e
    so acrescentaria uma linha de comando atipica sem comprar nada. A pref e a
    mesma que qualquer usuario de VPN corporativa tem ligada, mora no
    Preferences comum (nao no Secure Preferences, que e assinado) e nao aparece
    na linha de comando.
    """
    arquivo = persona.dir / "Default" / "Preferences"
    try:
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        dados = json.loads(arquivo.read_text(encoding="utf-8")) if arquivo.exists() else {}
        if dados.get("webrtc", {}).get("ip_handling_policy") == "disable_non_proxied_udp":
            return
        dados.setdefault("webrtc", {})["ip_handling_policy"] = "disable_non_proxied_udp"
        arquivo.write_text(json.dumps(dados), encoding="utf-8")
        log(f"{persona.apelido}: WebRTC preso ao proxy no perfil")
    except (OSError, ValueError) as e:
        # Preferences ilegivel nao pode derrubar a coleta, mas precisa aparecer:
        # sem a pref, este worker sai com o IP do Wi-Fi no srflx.
        log(f"{persona.apelido}: AVISO: nao fechei o vazamento de WebRTC "
            f"({str(e)[:70]}). Este perfil pode revelar o IP do Wi-Fi.")


def subir(persona, porta=None, url_inicial="about:blank", espera_s=45,
          _retry=True, proxy=None):
    """Sobe o Chrome da persona e devolve (pid, porta).

    A lista de flags e curta de proposito: toda flag atipica aparece no
    fingerprint (--disable-blink-features, --no-sandbox e afins sao detectaveis),
    entao aqui so entra o minimo - CDP, perfil, idioma e tamanho de janela.

    `proxy` (ou o modulo PROXY) manda o Chrome sair por um SOCKS local. Nao fere
    a regra acima: --proxy-server e linha de comando, invisivel para o JS da
    pagina, e milhoes de navegadores corporativos saem exatamente assim.
    """
    exe = achar_chrome()
    proxy = PROXY if proxy is None else proxy
    porta = porta or porta_livre()
    persona.dir.mkdir(parents=True, exist_ok=True)
    # Um Chrome sobrevivente do ciclo anterior sequestraria este lancamento.
    if _pids_do_perfil(persona):
        log(f"{persona.apelido}: Chrome anterior ainda vivo; encerrando antes de subir")
        derrubar(persona)
    _destrancar(persona)
    largura, altura = persona.janela
    tela_l, tela_a = personas.tela()
    pos_x = random.randint(0, max(0, tela_l - largura - 8))
    pos_y = random.randint(0, max(0, tela_a - altura - 48))  # 48: barra de tarefas

    flags = [
        f"--remote-debugging-port={porta}",
        f"--user-data-dir={persona.dir}",
        f"--window-size={largura},{altura}",
        # Posicao variavel, mas sempre com a janela inteira dentro da tela: uma
        # janela que "vaza" para fora do monitor primario e outra combinacao que
        # nao acontece com quem abriu o navegador na mao.
        f"--window-position={pos_x},{pos_y}",
        "--lang=pt-BR",
        "--accept-lang=pt-BR,pt,en-US,en",
        "--no-first-run",
        "--no-default-browser-check",
        # Evita que cada perfil baixe ~50 MB de modelos e listas do component
        # updater. Vale por dois motivos: com dezenas de personas isso vira
        # dezenas de GB em disco, e o download sairia pelo 4G do celular, que e
        # justamente o link que estamos poupando. Nenhuma das duas e visivel
        # para o JS da pagina, entao nao entram no fingerprint.
        "--disable-component-update",
        "--disable-features=OptimizationHints,OptimizationGuideModelDownloading",
    ]
    if proxy:
        # Sem --proxy-bypass-list de proposito: o Chrome ja ignora o proxy para
        # 127.0.0.1, e o CDP nem passaria por aqui - quem abre aquela conexao e
        # o Playwright, de fora para dentro, e o stack de rede do Chrome nao
        # serve o DevTools.
        flags.append(f"--proxy-server={proxy}")
        if SEM_VAZAMENTO_WEBRTC:
            _fechar_vazamento_webrtc(persona)
    flags.append(url_inicial)
    proc = subprocess.Popen([exe, *flags],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fim = time.time() + espera_s
    while time.time() < fim:
        info = cdp_no_ar(porta)
        if info:
            log(f"{persona.apelido}: Chrome no ar (porta {porta}, pid {proc.pid})")
            if persona.nova:
                persona.gravar_meta(nascido_em=time.time(),
                                    maturacao_h=random.uniform(personas.MATURACAO_H_MIN,
                                                              personas.MATURACAO_H_MAX),
                                    logins=0)
            return proc.pid, porta
        time.sleep(1)

    derrubar(persona, porta, proc.pid)
    if _retry:
        log(f"{persona.apelido}: CDP mudo na porta {porta}; uma nova tentativa")
        time.sleep(3)
        # proxy=proxy nao e detalhe: sem repassar, a segunda tentativa subiria
        # um Chrome SEM proxy, e o worker sairia pelo Wi-Fi sem nada no log
        # dizendo que trocou de rede no meio do lote.
        return subir(persona, None, url_inicial, espera_s, _retry=False,
                     proxy=proxy)
    raise RuntimeError(f"Chrome subiu mas o CDP nao respondeu na porta {porta}")


def _pids_do_perfil(persona):
    """PIDs de chrome.exe cuja linha de comando aponta para este perfil.

    Necessario porque o Chrome pode reaproveitar/relancar processos e o pid
    devolvido pelo Popen nem sempre e o processo-navegador que sobrevive.
    """
    # -like NAO usa barra invertida como escape: escapar aqui (o que este codigo
    # fazia antes) fazia o padrao nunca casar, e o Chrome sobrevivia a cada
    # "encerramento" segurando a porta do CDP. Os curingas de -like sao * ? [ ],
    # e so estes precisam de colchete de escape; a aspa simples dobra.
    alvo = (str(persona.dir).replace("'", "''")
            .replace("[", "`[").replace("]", "`]"))
    cmd = (f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
           f"Where-Object {{ $_.CommandLine -like '*{alvo}*' }} | "
           f"Select-Object -ExpandProperty ProcessId")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", cmd],
                           capture_output=True, text=True, timeout=30)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _fechar_abas(porta):
    """Fecha as abas pelo endpoint HTTP do DevTools.

    /json/close/<id> nao exige websocket, entao serve de encerramento gracioso
    sem arrastar o Playwright para dentro deste modulo.
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{porta}/json/list", timeout=3) as r:
            alvos = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return 0
    fechadas = 0
    for a in alvos:
        if a.get("type") != "page":
            continue
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{porta}/json/close/{a['id']}", timeout=3):
                fechadas += 1
        except Exception:
            pass
    return fechadas


def derrubar(persona, porta=None, pid=None, espera_s=25):
    """Fecha o Chrome da persona preservando o estado gravado em disco.

    Ordem: WM_CLOSE (taskkill sem /F) -> espera o CDP sumir -> /F so no que
    sobrar. O /F direto corromperia o LevelDB do localStorage, que e onde mora a
    identidade da persona.
    """
    pids = _pids_do_perfil(persona) or ([pid] if pid else [])
    if not pids and not (porta and cdp_no_ar(porta, timeout=1)):
        return

    # Primeiro o caminho limpo: fechar as abas pelo proprio DevTools. Sem aba
    # aberta o Chrome sai sozinho, gravando o LevelDB do jeito certo - que e o
    # que preserva o device ID da persona para o mes que vem.
    if porta:
        _fechar_abas(porta)
        fim = time.time() + 12
        while time.time() < fim and cdp_no_ar(porta, timeout=1):
            time.sleep(0.25)

    pids = _pids_do_perfil(persona) or [p for p in pids if p]
    for p in pids:
        subprocess.run(["taskkill", "/PID", str(p), "/T"],
                       capture_output=True, text=True)
    fim = time.time() + espera_s
    while time.time() < fim:
        # Ordem invertida de proposito: a porta morta responde na hora
        # (connection refused), enquanto _pids_do_perfil sobe um PowerShell e
        # custa ~0,45s medidos. Perguntar ao SO so quando o CDP ja calou tira
        # esse spawn de quase todos os giros do laco.
        if porta and cdp_no_ar(porta, timeout=1):
            time.sleep(0.25)
            continue
        if _pids_do_perfil(persona):
            time.sleep(0.5)
            continue
        break

    teimosos = _pids_do_perfil(persona)
    if teimosos:
        log(f"{persona.apelido}: forcando {len(teimosos)} processo(s) restante(s)")
        for p in teimosos:
            subprocess.run(["taskkill", "/PID", str(p), "/T", "/F"],
                           capture_output=True, text=True)
        time.sleep(2)

    liberto = persona.podar()
    log(f"{persona.apelido}: Chrome encerrado"
        + (f", cache podado ({liberto / 1e6:.0f} MB)" if liberto > 1e6 else ""))


def maturar(ctx, persona, passos=None):
    """Da historico a um perfil novo antes do primeiro login na Equatorial.

    Chegar na agencia virtual com um perfil que nunca visitou nada mais e o
    pior cenario para o reCAPTCHA v3: sem cookie do Google, sem historico de
    interacao, score no chao. Este passeio nao tenta enganar ninguem - ele
    simplesmente constroi o historico que qualquer navegador de uso real tem.
    """
    passos = passos or random.randint(3, len(SITES_MATURACAO))
    roteiro = random.sample(SITES_MATURACAO, k=min(passos, len(SITES_MATURACAO)))
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    log(f"{persona.apelido}: maturando perfil ({len(roteiro)} paradas)")

    ok = 0
    for url in roteiro:
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=40_000)
            ok += 1
        except Exception as e:
            # Site fora do ar ou recusando a conexao nao e problema nosso: a
            # maturacao segue com as outras paradas.
            log(f"{persona.apelido}: parada {url[:35]} falhou ({str(e)[:50]})")
            continue
        # Ler de verdade, com as mesmas distribuicoes do resto do robo: e este
        # rastro de interacao que o reCAPTCHA v3 pontua depois.
        H.ler(pg, persona, segundos=H.pausa_s(9, 0.5, teto=40))

    if ok < PARADAS_MINIMAS:
        # Nao marcar e proposital: o perfil volta para a fila de maturacao em
        # vez de estrear no login sem historico nenhum.
        log(f"{persona.apelido}: so {ok} parada(s) vingaram; perfil segue imaturo")
        return False
    persona.gravar_meta(maturado_em=time.time(), paradas=ok)
    log(f"{persona.apelido}: perfil maturado ({ok} paradas)")
    return True


if __name__ == "__main__":
    import sys
    import personas as P

    if len(sys.argv) < 2:
        sys.exit("uso: python navegador.py <cpf_ou_cnpj>   # sobe o perfil e espera Enter")
    pers = P.persona_de(sys.argv[1])
    pid, porta = subir(pers, url_inicial="https://goias.equatorialenergia.com.br/LoginGO.aspx")
    try:
        input(f"Chrome da persona {pers.apelido} na porta {porta}. Enter para encerrar...")
    finally:
        derrubar(pers, porta, pid)
