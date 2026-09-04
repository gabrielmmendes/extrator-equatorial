#!/usr/bin/env python3
r"""
Rotacao de IP via tethering USB de um Android (Windows).

Ideia: o celular plugado por USB vira um adaptador de rede no Windows. Se a rota
padrao da maquina apontar para ele, todo o trafego - inclusive o do Chrome que o
robo dirige - sai pelo IP da operadora. Desligar e religar o dado movel faz a
operadora reatribuir um IP novo.

Expectativa realista: operadoras brasileiras usam CGNAT, entao o "IP novo" quase
sempre vem do mesmo pool e do mesmo ASN - que e a granularidade em que o Imperva
pontua reputacao. Alem disso, o proprio diagnostico do projeto ja mostrou que a
recusa #001/#002 atinge login manual humano na rede de casa. Ou seja: isto e uma
tentativa de baixo retorno esperado, opt-in por EQUATORIAL_ROTACIONAR=1, nao a
correcao principal.

Requisitos:
  - adb: procurado no PATH, em %USERPROFILE%\platform-tools\adb.exe e no SDK do
    Android; ou aponte EQUATORIAL_ADB. O platform-tools da Google e so um zip,
    nao precisa de instalacao com privilegio.
  - depuracao USB ligada no celular, e "Ancoragem USB" (tethering) ativa
  - terminal como Administrador (Set-NetIPInterface exige elevacao)

Diagnostico sem efeito colateral: `python rede.py`.

CUIDADO: enquanto a rota padrao estiver no celular, a maquina inteira navega
pelo 4G - inclusive fora deste script, e consumindo franquia. Por isso todo
caminho de saida (fim normal, excecao, Ctrl+C, atexit) restaura o estado.
"""

import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.request

# O adaptador de tethering nao tem nome fixo: cada fabricante batiza o seu.
# Casamos pela descricao, que e onde a familia do driver aparece (RNDIS/NCM).
RE_TETHER = re.compile(
    r"NDIS|Internet Sharing|Tethering|NCM|USB.*(Ethernet|LAN)|Ethernet.*USB", re.I)

# Ecos de IP publico: seguem a rota padrao, entao respondem com o IP pelo qual a
# maquina sai de verdade. Mais de um porque qualquer um deles pode estar fora.
ECOS = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")

_ESTADO_ATIVO = None  # estado salvo pela assumir_rota() em vigor, se houver


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] rede: {msg}", flush=True)


def ps(comando, timeout=60):
    """Roda um comando PowerShell e devolve o stdout ja limpo.

    O prefixo de OutputEncoding nao e enfeite: o console do Windows PowerShell
    5.1 sai em cp850, e as mensagens de erro em portugues chegariam como
    mojibake justamente quando mais importam.
    """
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + comando],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"PowerShell falhou: {(r.stderr or r.stdout).strip()[:200]}")
    return (r.stdout or "").strip()


def ps_json(comando, timeout=60):
    """Igual a ps(), mas converte a saida de ConvertTo-Json e sempre da lista.

    Sem -AsArray (que so existe no PowerShell 7; aqui e o 5.1), um resultado de
    um item unico sai como objeto e nao como lista - normalizamos no Python.
    """
    saida = ps(f"{comando} | ConvertTo-Json -Depth 3 -Compress", timeout=timeout)
    if not saida:
        return []
    dados = json.loads(saida)
    return dados if isinstance(dados, list) else [dados]


def elevado():
    """True se o processo tem privilegio de Administrador."""
    try:
        r = ps("([Security.Principal.WindowsPrincipal]"
               "[Security.Principal.WindowsIdentity]::GetCurrent())"
               ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")
        return r.strip().lower() == "true"
    except Exception:
        return False


def detectar_tether():
    """Devolve {'ifIndex', 'nome', 'descricao'} do adaptador de tethering.

    So existe com o celular plugado E o tethering USB ligado no aparelho - se
    voce plugar sem ligar o tethering, o Windows nao cria adaptador nenhum e
    esta funcao levanta erro. Isso e proposital: e o sintoma exato do problema.
    """
    adaptadores = ps_json(
        "Get-NetAdapter | Where-Object Status -eq 'Up' "
        "| Select-Object Name,InterfaceDescription,ifIndex")
    for a in adaptadores:
        if RE_TETHER.search(a.get("InterfaceDescription") or ""):
            return {"ifIndex": a["ifIndex"], "nome": a["Name"],
                    "descricao": a["InterfaceDescription"]}
    vistos = ", ".join(f"{a['Name']} ({a['InterfaceDescription']})" for a in adaptadores)
    raise RuntimeError(
        "Nao achei adaptador de tethering USB. Plugue o celular e ligue "
        f"'Ancoragem USB' nas configuracoes dele.\nAdaptadores ativos: {vistos}")


def _metricas():
    """Estado atual de metrica/automatica de todas as interfaces IPv4."""
    return ps_json("Get-NetIPInterface -AddressFamily IPv4 "
                   "| Select-Object ifIndex,InterfaceAlias,InterfaceMetric,AutomaticMetric")


def assumir_rota(if_index):
    """Faz o tethering virar a rota padrao. Devolve o estado para restaurar.

    Metrica 1 com AutomaticMetric desligada vence qualquer outra interface (o
    Wi-Fi desta maquina esta em 35). Guardamos a metrica anterior de TODAS as
    interfaces porque restaurar so a do tether deixaria a maquina num estado
    diferente do que estava.
    """
    global _ESTADO_ATIVO
    if not elevado():
        raise RuntimeError(
            "Set-NetIPInterface exige Administrador. Abra o terminal como "
            "administrador, ou rode com EQUATORIAL_ROTACIONAR=0.")

    estado = _metricas()
    ps(f"Set-NetIPInterface -InterfaceIndex {int(if_index)} -AddressFamily IPv4 "
       f"-AutomaticMetric Disabled -InterfaceMetric 1")
    _ESTADO_ATIVO = estado
    atexit.register(restaurar_rota, estado)
    for sinal in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sinal, _sair(sinal, estado))
        except (ValueError, OSError):
            pass  # sem console / thread secundaria: o atexit ainda cobre
    log(f"Rota padrao assumida pela interface {if_index}")
    return estado


def _sair(sinal, estado):
    anterior = signal.getsignal(sinal)

    def handler(s, frame):
        restaurar_rota(estado)
        if callable(anterior):
            anterior(s, frame)
        else:
            raise KeyboardInterrupt
    return handler


def restaurar_rota(estado):
    """Devolve as metricas ao que eram. Idempotente: pode rodar varias vezes."""
    global _ESTADO_ATIVO
    if estado is None or _ESTADO_ATIVO is None:
        return
    _ESTADO_ATIVO = None
    for i in estado:
        try:
            if str(i.get("AutomaticMetric")).lower() in ("1", "true", "enabled"):
                ps(f"Set-NetIPInterface -InterfaceIndex {i['ifIndex']} "
                   f"-AddressFamily IPv4 -AutomaticMetric Enabled")
            else:
                ps(f"Set-NetIPInterface -InterfaceIndex {i['ifIndex']} "
                   f"-AddressFamily IPv4 -AutomaticMetric Disabled "
                   f"-InterfaceMetric {i['InterfaceMetric']}")
        except Exception as e:
            log(f"AVISO: nao restaurei a interface {i.get('ifIndex')}: {str(e)[:80]}")
    log("Metricas de rede restauradas")


def ip_publico(timeout=8):
    """IP pelo qual a maquina sai agora, ou None."""
    for url in ECOS:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                ip = r.read().decode("utf-8", "replace").strip()
            if re.fullmatch(r"[0-9.]{7,15}", ip):
                return ip
        except Exception:
            continue
    return None


def caminho_adb():
    """Localiza o adb sem exigir instalacao com privilegio.

    O platform-tools da Google e um zip que roda de qualquer pasta, entao vale
    procurar nos lugares onde ele costuma cair antes de desistir - instalar via
    choco exigiria elevacao, que este script nao deve pedir so para isso.
    """
    candidatos = [os.environ.get("EQUATORIAL_ADB"), shutil.which("adb")]
    perfil = os.environ.get("USERPROFILE", "")
    if perfil:
        candidatos += [
            os.path.join(perfil, "platform-tools", "adb.exe"),
            os.path.join(perfil, "AppData", "Local", "Android", "Sdk",
                         "platform-tools", "adb.exe"),
        ]
    for c in candidatos:
        if c and os.path.isfile(c):
            return c
    return None


def adb(*args, timeout=30):
    """Chama o adb. Erro legivel se nao estiver instalado ou sem aparelho."""
    exe = caminho_adb()
    if exe is None:
        raise RuntimeError(
            "adb nao encontrado. Baixe o platform-tools da Google e "
            "descompacte em %USERPROFILE%\\platform-tools, ou aponte "
            "EQUATORIAL_ADB para o adb.exe.")
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"adb nao executavel em {exe}") from None
    saida = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} falhou: {saida.strip()[:160]}")
    return saida.strip()


def aparelho_pronto():
    """True se ha exatamente um aparelho autorizado."""
    linhas = [l for l in adb("devices").splitlines()[1:] if l.strip()]
    autorizados = [l for l in linhas if l.split("\t")[-1].strip() == "device"]
    if not autorizados:
        raise RuntimeError(
            "Nenhum aparelho autorizado no adb. Ligue a depuracao USB e "
            f"aceite o pedido na tela do celular.\nadb devices:\n{linhas}")
    return True


def _esperar_ip(diferente_de, teto_s=45):
    """Espera um IP publico aparecer; devolve o primeiro diferente do anterior.

    Devolve tambem IP igual se o teto estourar - CGNAT reatribuir o mesmo
    endereco e resultado esperado, nao erro.
    """
    fim = time.time() + teto_s
    ultimo = None
    while time.time() < fim:
        ultimo = ip_publico(timeout=5)
        if ultimo and ultimo != diferente_de:
            return ultimo
        time.sleep(3)
    return ultimo


def _aviao(pausa=6):
    """Liga e desliga o modo aviao (Android 11+). Levanta se indisponivel."""
    adb("shell", "cmd", "connectivity", "airplane-mode", "enable")
    time.sleep(pausa)
    adb("shell", "cmd", "connectivity", "airplane-mode", "disable")


def _toggle_dados(pausa=4):
    """Desliga e religa o dado movel."""
    adb("shell", "svc", "data", "disable")
    time.sleep(pausa)
    adb("shell", "svc", "data", "enable")


def rotacionar(teto_s=45, metodo=None):
    """Forca a operadora a reatribuir o IP. Devolve (ip_antes, ip_depois).

    Medido no Galaxy S20 FE / CLARO: `svc data disable/enable` reconecta SEMPRE
    no mesmo IP - nao adianta. O modo aviao troca o IP de fato (e para faixas
    bem diferentes: 187.43.x -> 191.58.x -> 191.56.x), sem derrubar o tethering
    USB nem o adb. Por isso o modo aviao e o metodo primario aqui; o toggle de
    dado fica de reserva para aparelhos onde o aviao nao esteja disponivel
    (Android <11, onde 'cmd connectivity' nao existe).

    EQUATORIAL_ROTACAO=dados forca o toggle de dado primeiro, se algum aparelho
    se comportar ao contrario.
    """
    aparelho_pronto()
    metodo = (metodo or os.environ.get("EQUATORIAL_ROTACAO", "aviao")).lower()
    ordem = ["dados", "aviao"] if metodo == "dados" else ["aviao", "dados"]

    antes = ip_publico()
    log(f"IP antes: {antes}")
    depois = antes
    for m in ordem:
        if depois and depois != antes:
            break
        try:
            if m == "aviao":
                _aviao()
            else:
                _toggle_dados()
        except RuntimeError as e:
            log(f"Metodo '{m}' indisponivel ({str(e)[:60]}); tentando o proximo")
            continue
        depois = _esperar_ip(antes, teto_s=teto_s)
        if depois == antes:
            log(f"Metodo '{m}' nao mudou o IP")

    if depois is None:
        log("AVISO: sem IP publico apos a rotacao - o tethering pode ter caido")
    elif depois == antes:
        log(f"IP nao mudou ({depois})")
    else:
        log(f"IP depois: {depois}")
    return antes, depois


def diagnostico():
    """Checagem manual, sem mexer em nada: `python rede.py`."""
    print(f"Administrador: {elevado()}")
    try:
        t = detectar_tether()
        print(f"Tethering: ifIndex={t['ifIndex']} {t['nome']} - {t['descricao']}")
    except RuntimeError as e:
        print(f"Tethering: {e}")
    try:
        aparelho_pronto()
        print(f"adb: ok - {adb('shell', 'getprop', 'ro.product.model')}")
    except RuntimeError as e:
        print(f"adb: {e}")
    print(f"adb em: {caminho_adb()}")
    print(f"IP publico atual: {ip_publico()}")
    for i in _metricas():
        print(f"  ifIndex={i['ifIndex']:<4} metrica={i['InterfaceMetric']:<4} "
              f"auto={i['AutomaticMetric']:<9} {i['InterfaceAlias']}")


if __name__ == "__main__":
    diagnostico()
