#!/usr/bin/env python3
r"""
Rotacao de IP via tethering USB de Androids (Windows).

Ideia: o celular plugado por USB vira um adaptador de rede no Windows. Ha dois
jeitos de fazer o trafego sair por ele, e este modulo serve aos dois:

  1. UM celular (login.preparar_rede): a rota padrao da maquina aponta para o
     tether e TUDO sai pelo IP da operadora - inclusive o que nao e do robo.
     Exige Administrador (Set-NetIPInterface).
  2. VARIOS celulares (coletar_paralelo.py): a rota padrao continua no Wi-Fi e
     cada worker amarra os proprios sockets ao IP local do adaptador do SEU
     aparelho (ver proxy_local.py). Nao exige Administrador, e e o que permite
     N extracoes simultaneas com N IPs de operadora diferentes.

Desligar e religar o dado movel faz a operadora reatribuir um IP novo.

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
  - terminal como Administrador SO no modo (1); o modo (2) dispensa

Diagnostico sem efeito colateral: `python rede.py`.

CUIDADO: no modo (1), enquanto a rota padrao estiver no celular, a maquina
inteira navega pelo 4G - inclusive fora deste script, e consumindo franquia. Por
isso todo caminho de saida (fim normal, excecao, Ctrl+C, atexit) restaura o
estado.
"""

import atexit
import http.client
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import time
import urllib.request

# O adaptador de tethering nao tem nome fixo: cada fabricante batiza o seu.
# Casamos pela descricao, que e onde a familia do driver aparece (RNDIS/NCM).
# "NDIS" solto casava tambem "Fortinet Virtual Ethernet Adapter (NDIS 6.30)",
# que existe nesta maquina e so nao quebrou nada por viver Disconnected. O nome
# da familia e RNDIS - mas escrito de duas formas, e as duas aparecem aqui:
#   Motorola: "Remote NDIS based Internet Sharing Device"
#   Samsung:  "SAMSUNG Mobile USB Remote NDIS Network Device #2"
# So "RNDIS" (sem espaco) deixava o Samsung de fora, e ele sumia do pareamento
# como se a ancoragem estivesse desligada. O filtro de PnPDeviceID em
# adaptadores_tether() fecha o resto: adaptador virtual e ROOT\NET\* e tethering
# USB e USB\VID_*.
RE_TETHER = re.compile(
    r"R\s?NDIS|Remote NDIS|Internet Sharing|Tethering|NCM"
    r"|USB.*(Ethernet|LAN)|Ethernet.*USB", re.I)

# Ecos de IP publico: seguem a rota padrao (ou a origem amarrada, ver
# ip_publico), entao respondem com o IP pelo qual a maquina sai de verdade.
# Mais de um porque qualquer um deles pode estar fora.
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


def adaptadores_tether():
    """Todos os adaptadores de tethering USB prontos - um por celular plugado.

    Cada celular com ancoragem USB ligada vira um adaptador proprio no Windows.
    Devolve por adaptador: ifIndex, nome, descricao, mac, pnp (o InstanceId do
    dispositivo, por onde dispositivos.py chega ao numero de serie USB) e ip (o
    IPv4 que o DHCP do proprio celular entregou).

    Adaptador sem endereco util (169.254.*, APIPA) fica de fora de proposito: e
    o sintoma de celular plugado com a ancoragem DESLIGADA - o Windows cria o
    adaptador e ninguem entrega endereco nenhum.
    """
    adaptadores = ps_json(
        "Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object "
        "Name,InterfaceDescription,ifIndex,MacAddress,PnPDeviceID")
    enderecos = {}
    for i in ps_json("Get-NetIPAddress -AddressFamily IPv4 "
                     "| Select-Object ifIndex,IPAddress"):
        enderecos.setdefault(i["ifIndex"], i["IPAddress"])

    saida = []
    for a in adaptadores:
        pnp = a.get("PnPDeviceID") or ""
        if not pnp.upper().startswith("USB\\"):
            continue
        if not RE_TETHER.search(a.get("InterfaceDescription") or ""):
            continue
        ip = enderecos.get(a["ifIndex"])
        if not ip or ip.startswith("169.254."):
            continue
        saida.append({"ifIndex": a["ifIndex"], "nome": a["Name"],
                      "descricao": a["InterfaceDescription"],
                      "mac": a.get("MacAddress"), "pnp": pnp, "ip": ip,
                      "gateway": gateway_de(a["ifIndex"])})
    return saida


# Interfaces do lado do celular, vistas por `adb shell ip -4 -o addr`.
RE_WIFI_ANDROID = re.compile(r"^wlan\d", re.I)
RE_MOVEL_ANDROID = re.compile(r"^(v4-)?(rmnet|ccmni|rmnet_data|pdp)", re.I)


def uplink_do_aparelho(serial=None):
    """Por onde o celular sai: 'wifi', 'movel' ou '?'.

    Checagem que decide se o aparelho serve de worker, e ela mora AQUI e nao na
    medicao de IP publico por um motivo pratico: o Android prefere Wi-Fi quando
    as duas conexoes estao ligadas. Um celular com dado movel ativo E Wi-Fi
    conectado ancora o WI-FI - e se for o Wi-Fi da casa, ele sai pelo mesmo IP
    publico do notebook. O aparelho pareceria perfeito (adaptador de pe,
    endereco, IP publico respondendo) e nao compraria IP nenhum.

    Medido aqui com dois aparelhos plugados:
      SM_G781B      wlan0 10.1.22.15 + rmnet_data0  -> saiu por 45.191.204.245 (a casa)
      moto_g54_5G   so v4-ccmni1                    -> saiu por 177.174.217.194 (operadora)
    """
    try:
        saida = adb("shell", "ip", "-4", "-o", "addr", serial=serial)
    except RuntimeError:
        return "?"
    tem_wifi = tem_movel = False
    for linha in saida.splitlines():
        campos = linha.strip().split()
        if len(campos) < 4 or campos[2] != "inet":
            continue
        nome = campos[1]
        tem_wifi = tem_wifi or bool(RE_WIFI_ANDROID.match(nome))
        tem_movel = tem_movel or bool(RE_MOVEL_ANDROID.match(nome))
    if tem_wifi:
        return "wifi"   # vence o movel no Android, mesmo com os dois ligados
    if tem_movel:
        return "movel"
    return "?"


def saidas_nao_celular():
    """IPs publicos por onde a maquina sai SEM contar celular nenhum.

    Serve a uma checagem que nao e teorica: um Android com ancoragem ligada mas
    dado movel desligado compartilha o WI-FI ao qual ele proprio esta conectado.
    O adaptador aparece, pega endereco, o pareamento acerta o dono - e o celular
    sai pelo mesmo IP publico do notebook. Como worker ele nao compraria IP
    nenhum, e ainda por cima seria o IP residencial, que e justamente o que o
    diagnostico deste projeto ja mostrou recusado.

    Medido aqui com dois aparelhos plugados:
      motorola (dado movel) -> 177.174.217.194
      samsung  (wi-fi)      -> 45.191.204.245   = o mesmo do notebook
    """
    celulares = {a["ifIndex"] for a in adaptadores_tether()}
    saidas = set()
    for i in ps_json("Get-NetIPAddress -AddressFamily IPv4 "
                     "| Select-Object ifIndex,IPAddress,PrefixOrigin"):
        ip = i.get("IPAddress") or ""
        if (i["ifIndex"] in celulares or i.get("PrefixOrigin") == "WellKnown"
                or ip.startswith(("127.", "169.254."))):
            continue
        publico = ip_publico(timeout=5, origem=ip)
        if publico:
            saidas.add(publico)
    return saidas


def dono_da_rota_padrao():
    """ifIndex por onde a maquina sai por padrao agora, ou None.

    Serve de aviso, nao de controle: o adaptador RNDIS anuncia banda alta e
    costuma receber metrica automatica MENOR que a do Wi-Fi (35 nesta maquina),
    entao plugar celulares pode entregar a rota padrao a um deles sem ninguem
    pedir. No modo paralelo isso nao quebra a coleta - toda saida do robo e
    amarrada -, mas gasta a franquia daquele aparelho com o trafego solto da
    maquina e faz qualquer medicao NAO amarrada mentir.
    """
    rotas = ps_json("Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
                    "| Select-Object ifIndex,InterfaceMetric,RouteMetric")
    if not rotas:
        return None
    return min(rotas, key=lambda r: (r.get("InterfaceMetric", 0)
                                     + r.get("RouteMetric", 0)))["ifIndex"]


def detectar_tether():
    """Devolve o PRIMEIRO adaptador de tethering, para o caminho de um celular.

    So existe com o celular plugado E o tethering USB ligado no aparelho - se
    voce plugar sem ligar o tethering, o Windows nao cria adaptador nenhum (ou
    cria um sem endereco) e esta funcao levanta erro. Isso e proposital: e o
    sintoma exato do problema.

    Continua sendo "o primeiro" porque quem chama e o caminho de UM celular
    (login.preparar_rede, que disputa a rota padrao). Com varios aparelhos quem
    manda e o coletar_paralelo.py, que usa adaptadores_tether() e nao mexe na
    rota de ninguem.
    """
    achados = adaptadores_tether()
    if achados:
        return achados[0]
    vistos = ", ".join(
        f"{a['Name']} ({a['InterfaceDescription']})"
        for a in ps_json("Get-NetAdapter | Where-Object Status -eq 'Up' "
                         "| Select-Object Name,InterfaceDescription"))
    raise RuntimeError(
        "Nao achei adaptador de tethering USB com endereco. Plugue o celular e "
        f"ligue 'Ancoragem USB' nas configuracoes dele.\nAdaptadores ativos: {vistos}")


def origem_de(if_index):
    """IPv4 que o adaptador tem AGORA, ou None.

    Lido na hora, e nao guardado, porque o endereco vem do DHCP do proprio
    celular: o tranco do modo aviao pode derrubar e reerguer o tethering, e o
    Android 11+ sorteia um /24 novo a cada sessao de ancoragem. Endereco velho
    em socket amarrado da WSAEADDRNOTAVAIL em toda conexao seguinte.
    """
    # Tolerante de proposito: durante o tranco do modo aviao o adaptador some do
    # Windows por alguns segundos, e Get-NetIPAddress num ifIndex que nao existe
    # devolve codigo de erro. "Ainda nao tem endereco" e resposta, nao falha - e
    # e justamente o estado que esperar_origem() fica esperando passar.
    try:
        enderecos = ps_json(f"Get-NetIPAddress -AddressFamily IPv4 -ifIndex "
                            f"{int(if_index)} -ErrorAction SilentlyContinue "
                            "| Select-Object IPAddress")
    except RuntimeError:
        return None
    for i in enderecos:
        ip = i.get("IPAddress")
        if ip and not ip.startswith("169.254."):
            return ip
    return None


def esperar_origem(if_index, teto_s=30):
    """Espera o adaptador voltar com endereco depois de um tranco. None se nao volta."""
    fim = time.time() + teto_s
    while time.time() < fim:
        ip = origem_de(if_index)
        if ip:
            return ip
        time.sleep(2)
    return None


def gateway_de(if_index):
    """NextHop da rota padrao daquele adaptador - o IP do celular no tethering.

    E o segundo criterio de pareamento: o Android se poe em 192.168.x.129 do
    lado do cabo, e `adb -s SERIAL shell ip -4 -o addr` mostra esse mesmo
    endereco. Custa uma chamada e nao mexe em nada - ao contrario da sonda
    empirica, que precisa derrubar o dado do aparelho para descobrir de quem
    ele e.
    """
    try:
        rotas = ps_json(f"Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ifIndex "
                        f"{int(if_index)} -ErrorAction SilentlyContinue "
                        "| Select-Object NextHop")
    except RuntimeError:
        return None  # adaptador sem rota padrao, ou ja desaparecido
    for r in rotas:
        alvo = r.get("NextHop")
        if alvo and alvo != "0.0.0.0":
            return alvo
    return None


def serial_usb(pnp_id, teto=4):
    """Numero de serie USB do dispositivo, subindo pelos pais. None se nao houver.

    O adaptador de tethering e uma FUNCAO de um dispositivo composto, entao o
    proprio InstanceId dele termina em '&MI_00\\...' e nao carrega serial. Quem
    carrega e o pai, no formato USB\\VID_xxxx&PID_xxxx\\<serial>. Quando o
    aparelho nao publica iSerialNumber, o Windows inventa um id pelo caminho
    fisico ('5&1a2b3c4d&0&2') - reconhecivel pelo '&' e descartado aqui, porque
    nao ha nada ali para casar com o adb.
    """
    atual = pnp_id
    for _ in range(teto):
        if not atual:
            return None
        partes = atual.split("\\")
        if (len(partes) == 3 and partes[0].upper() == "USB"
                and "&" not in partes[2] and partes[2]):
            return partes[2]
        alvo = atual.replace("'", "''")
        try:
            dados = ps_json(
                f"Get-PnpDeviceProperty -InstanceId '{alvo}' "
                "-KeyName DEVPKEY_Device_Parent -ErrorAction Stop "
                "| Select-Object Data")
        except RuntimeError:
            return None
        atual = dados[0].get("Data") if dados else None
    return None


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

    So o caminho de UM celular passa por aqui. O modo multi-aparelho nao chama
    esta funcao: amarrar o socket a origem certa (proxy_local.py) resolve o
    mesmo problema sem tomar a rota da maquina nem pedir elevacao.
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
    _restaurar_no_fim(lambda: restaurar_rota(estado))
    log(f"Rota padrao assumida pela interface {if_index}")
    return estado


def _restaurar_no_fim(restaurador):
    """Roda `restaurador` em todo caminho de saida: fim normal, Ctrl+C, SIGTERM.

    Generalizado a partir do que a assumir_rota ja fazia, porque o modo
    multi-aparelho precisa da MESMA disciplina para outra coisa: devolver o
    celular com o modo aviao desligado e o dado ligado. Um worker morto dentro
    da janela de 6s do modo aviao deixaria o aparelho mudo ate alguem notar.
    """
    atexit.register(restaurador)
    for sinal in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sinal, _sair(sinal, restaurador))
        except (ValueError, OSError):
            pass  # sem console / thread secundaria: o atexit ainda cobre


def _sair(sinal, restaurador):
    anterior = signal.getsignal(sinal)

    def handler(s, frame):
        restaurador()
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


def _abridor(origem):
    """urlopen que sai por `origem`. None = rota padrao (o de sempre).

    Sem isto, um worker que roda atras do celular B mediria o IP do Wi-Fi: o
    urllib nao conhece proxy nem interface, ele segue a rota padrao. Amarrar o
    endereco de origem faz o Windows restringir a busca de rota a interface
    dona daquele IP - e por isso que N celulares podem sair ao mesmo tempo.
    """
    if origem is None:
        return urllib.request.urlopen

    class _Conexao(http.client.HTTPSConnection):
        def connect(self):
            self.sock = socket.create_connection(
                (self.host, self.port), self.timeout, source_address=(origem, 0))
            self.sock = ssl.create_default_context().wrap_socket(
                self.sock, server_hostname=self.host)

    class _Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_Conexao, req)

    return urllib.request.build_opener(_Handler).open


def ip_publico(timeout=8, origem=None):
    """IP pelo qual a maquina sai agora, ou None.

    Com `origem` (o IPv4 local do adaptador de um celular), mede o IP DAQUELE
    aparelho, mesmo que a rota padrao esteja no Wi-Fi.
    """
    abrir = _abridor(origem)
    for url in ECOS:
        try:
            with abrir(url, timeout=timeout) as r:
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


def adb(*args, serial=None, timeout=30):
    """Chama o adb. Erro legivel se nao estiver instalado ou sem aparelho.

    `serial` vira `-s <serial>`: sem ele, com dois celulares plugados TODO
    comando de shell morre em "more than one device/emulator". No caminho de um
    aparelho so, continua valendo omitir.
    """
    exe = caminho_adb()
    if exe is None:
        raise RuntimeError(
            "adb nao encontrado. Baixe o platform-tools da Google e "
            "descompacte em %USERPROFILE%\\platform-tools, ou aponte "
            "EQUATORIAL_ADB para o adb.exe.")
    alvo = ["-s", serial] if serial else []
    try:
        r = subprocess.run([exe, *alvo, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"adb nao executavel em {exe}") from None
    saida = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} falhou: {saida.strip()[:160]}")
    return saida.strip()


def listar_aparelhos():
    """[{'serial', 'modelo'}] dos aparelhos autorizados, na ordem do adb.

    `adb devices -l` traz "SERIAL  device product:... model:... device:...".
    Aparelho em "unauthorized" (o pedido de depuracao ainda parado na tela do
    celular) fica de fora: ele nao aceita comando nenhum, entao lista-lo como
    disponivel so adiaria a descoberta do problema.
    """
    saida = []
    for linha in adb("devices", "-l").splitlines()[1:]:
        campos = linha.split()
        if len(campos) < 2 or campos[1] != "device":
            continue
        modelo = next((c.split(":", 1)[1] for c in campos[2:]
                       if c.startswith("model:")), "?")
        saida.append({"serial": campos[0], "modelo": modelo})
    return saida


def aparelho_pronto(serial=None):
    """Confirma que da para mandar comando. Com `serial`, para aquele aparelho.

    Sem serial exige EXATAMENTE um aparelho - que e o que o caminho de um
    celular sempre pressupos (a docstring antiga ja prometia isso; o codigo so
    checava "pelo menos um", e com dois plugados quem escolhia era o adb).
    Com varios, a mensagem manda para o coletar_paralelo.py.
    """
    aparelhos = listar_aparelhos()
    if not aparelhos:
        raise RuntimeError(
            "Nenhum aparelho autorizado no adb. Ligue a depuracao USB e "
            f"aceite o pedido na tela do celular.\nadb devices:\n{adb('devices')}")
    if serial:
        if not any(a["serial"] == serial for a in aparelhos):
            vistos = ", ".join(a["serial"] for a in aparelhos)
            raise RuntimeError(f"Aparelho {serial} nao esta autorizado. Vejo: {vistos}")
        return True
    if len(aparelhos) > 1:
        vistos = ", ".join(f"{a['serial']} ({a['modelo']})" for a in aparelhos)
        raise RuntimeError(
            f"{len(aparelhos)} aparelhos plugados ({vistos}), e este caminho so "
            "sabe dirigir um. Use o coletar_paralelo.py, ou deixe so um celular "
            "ligado.")
    return True


def estado_do_aparelho(serial=None):
    """{'aviao': bool, 'dados': bool} como o celular esta agora.

    `settings get global mobile_data` volta 'null' em aparelho dual-SIM (a
    chave e por slot: mobile_data0/1). Tratamos null como ligado: a unica acao
    de restauracao e LIGAR, e quem plugou o celular para ancorar queria o dado
    ligado de qualquer jeito.
    """
    def _ler(chave, padrao):
        try:
            v = adb("shell", "settings", "get", "global", chave, serial=serial).strip()
        except RuntimeError:
            return padrao
        return padrao if v in ("", "null") else v == "1"

    return {"aviao": _ler("airplane_mode_on", False),
            "dados": _ler("mobile_data", True)}


def restaurar_aparelho(estado, serial=None):
    """Devolve o celular ao estado guardado. Idempotente e nunca levanta.

    So desfaz o que estraga: modo aviao ligado e dado desligado. Se o aparelho
    ja estava assim quando comecamos, nao mexe - a maquina nao e nossa.
    """
    if not estado:
        return
    try:
        if not estado.get("aviao"):
            adb("shell", "cmd", "connectivity", "airplane-mode", "disable", serial=serial)
    except RuntimeError as e:
        log(f"AVISO: nao desliguei o modo aviao de {serial}: {str(e)[:70]}")
    try:
        if estado.get("dados", True):
            adb("shell", "svc", "data", "enable", serial=serial)
    except RuntimeError as e:
        log(f"AVISO: nao religuei o dado de {serial}: {str(e)[:70]}")


def guardar_aparelho(serial=None):
    """Fotografa o estado do celular e agenda a restauracao para a saida."""
    estado = estado_do_aparelho(serial)
    _restaurar_no_fim(lambda: restaurar_aparelho(estado, serial))
    return estado


def _esperar_ip(diferente_de, teto_s=45, origem=None):
    """Espera um IP publico aparecer; devolve o primeiro diferente do anterior.

    Devolve tambem IP igual se o teto estourar - CGNAT reatribuir o mesmo
    endereco e resultado esperado, nao erro.
    """
    fim = time.time() + teto_s
    ultimo = None
    while time.time() < fim:
        ultimo = ip_publico(timeout=5, origem=origem)
        if ultimo and ultimo != diferente_de:
            return ultimo
        time.sleep(3)
    return ultimo


def _aviao(serial=None, pausa=6):
    """Liga e desliga o modo aviao (Android 11+). Levanta se indisponivel."""
    adb("shell", "cmd", "connectivity", "airplane-mode", "enable", serial=serial)
    time.sleep(pausa)
    adb("shell", "cmd", "connectivity", "airplane-mode", "disable", serial=serial)


def _toggle_dados(serial=None, pausa=4):
    """Desliga e religa o dado movel."""
    adb("shell", "svc", "data", "disable", serial=serial)
    time.sleep(pausa)
    adb("shell", "svc", "data", "enable", serial=serial)


def rotacionar(teto_s=45, metodo=None, serial=None, origem=None, if_index=None):
    """Forca a operadora a reatribuir o IP. Devolve (ip_antes, ip_depois).

    Medido no Galaxy S20 FE / CLARO: `svc data disable/enable` reconecta SEMPRE
    no mesmo IP - nao adianta. O modo aviao troca o IP de fato (e para faixas
    bem diferentes: 187.43.x -> 191.58.x -> 191.56.x), sem derrubar o tethering
    USB nem o adb. Por isso o modo aviao e o metodo primario aqui; o toggle de
    dado fica de reserva para aparelhos onde o aviao nao esteja disponivel
    (Android <11, onde 'cmd connectivity' nao existe).

    EQUATORIAL_ROTACAO=dados forca o toggle de dado primeiro, se algum aparelho
    se comportar ao contrario.

    `serial` escolhe o aparelho e `origem` (o IPv4 local do adaptador dele) diz
    por onde medir. Os dois andam juntos no modo multi-aparelho: sem `origem` a
    medicao sairia pela rota padrao e reportaria o IP de outra conexao.

    Com `if_index` a origem e reconsultada DEPOIS de cada tranco, e nao so no
    comeco: o modo aviao pode derrubar e reerguer o tethering, e o Android 11+
    sorteia um /24 novo por sessao de ancoragem. Medir no endereco velho daria
    "sem IP publico" com o celular perfeitamente vivo.
    """
    aparelho_pronto(serial)
    metodo = (metodo or os.environ.get("EQUATORIAL_ROTACAO", "aviao")).lower()
    ordem = ["dados", "aviao"] if metodo == "dados" else ["aviao", "dados"]
    if if_index is not None and origem is None:
        origem = origem_de(if_index)

    antes = ip_publico(origem=origem)
    log(f"IP antes: {antes}")
    depois = antes
    for m in ordem:
        if depois and depois != antes:
            break
        try:
            if m == "aviao":
                _aviao(serial=serial)
            else:
                _toggle_dados(serial=serial)
        except RuntimeError as e:
            log(f"Metodo '{m}' indisponivel ({str(e)[:60]}); tentando o proximo")
            continue
        if if_index is not None:
            novo = esperar_origem(if_index)
            if novo and novo != origem:
                log(f"Adaptador voltou com endereco novo: {origem} -> {novo}")
            origem = novo or origem
        depois = _esperar_ip(antes, teto_s=teto_s, origem=origem)
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
    print(f"adb em: {caminho_adb()}")
    try:
        aparelhos = listar_aparelhos()
        print(f"Aparelhos autorizados: {len(aparelhos)}")
        for a in aparelhos:
            print(f"  {a['serial']:<20} {a['modelo']}")
    except RuntimeError as e:
        print(f"adb: {e}")
    achados = adaptadores_tether()
    print(f"Adaptadores de tethering: {len(achados)}")
    for t in achados:
        print(f"  ifIndex={t['ifIndex']:<4} ip={t['ip']:<15} gw={t['gateway']} "
              f"{t['nome']} - {t['descricao']}")
        print(f"    serie USB: {serial_usb(t['pnp'])}")
        print(f"    IP publico por ele: {ip_publico(origem=t['ip'])}")
    if not achados:
        print("  (nenhum: plugue o celular e ligue 'Ancoragem USB' nele)")
    padrao = dono_da_rota_padrao()
    print(f"IP publico pela rota padrao: {ip_publico()} (ifIndex {padrao})")
    if any(t["ifIndex"] == padrao for t in achados):
        print("  AVISO: a rota padrao da maquina esta num celular. A coleta "
              "paralela funciona assim mesmo (tudo dela sai amarrado), mas o "
              "trafego solto da maquina gasta a franquia desse aparelho.")
    for i in _metricas():
        print(f"  ifIndex={i['ifIndex']:<4} metrica={i['InterfaceMetric']:<4} "
              f"auto={i['AutomaticMetric']:<9} {i['InterfaceAlias']}")


if __name__ == "__main__":
    diagnostico()
