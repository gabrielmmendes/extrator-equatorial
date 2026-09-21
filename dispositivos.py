#!/usr/bin/env python3
r"""
Descobre quais celulares estao plugados e de quem e cada adaptador de rede.

O problema: com N Androids ancorados por USB, o Windows cria N adaptadores e o
adb ve N seriais - mas nada diz qual adaptador pertence a qual aparelho. E a
resposta precisa estar certa, porque cada worker do coletar_paralelo.py vai
amarrar os proprios sockets ao IP do SEU adaptador e rotacionar o IP do SEU
serial. Trocar os dois significa um worker desligando o dado movel do celular
que outro esta usando no meio de um login - exatamente a incoerencia que este
projeto inteiro existe para nao produzir.

Escada de criterios, do mais barato para o mais caro:

  1. serie USB. O adaptador de tethering e uma funcao de um dispositivo
     composto; subindo pelos DEVPKEY_Device_Parent chega-se a
     USB\VID_xxxx&PID_xxxx\<serie>, e no Android essa serie E o serial do adb.
     Custo zero e sem efeito colateral.
  2. gateway. O Android se poe em 192.168.x.129 do lado do cabo; o adaptador do
     Windows recebe esse endereco como NextHop da rota padrao dele, e
     `adb -s SERIAL shell ip -4 -o addr` mostra o mesmo numero do lado de la.
     Uma chamada de adb, tambem sem efeito colateral.
  3. sonda empirica. So para o que sobrar: derruba o dado movel de um serial e
     ve qual adaptador perde a saida. Custa ~10s por par e mexe no aparelho,
     por isso e o ultimo.

Nada disso e guardado em cache. Seria tentador - a serie USB e estavel -, mas
ifIndex, endereco e ate o MAC do RNDIS mudam quando o cabo sai e volta, e o
preco de um mapa velho e justamente o worker rotacionando o celular do vizinho.
Refazer custa ~2s e roda uma vez por execucao.

Diagnostico sem efeito colateral: `python dispositivos.py`.
"""

import re
import sys
import time

import rede

# `ip -4 -o addr` do Android: "3: rndis0    inet 192.168.42.129/24 brd ..."
RE_INET = re.compile(r"^\d+:\s*(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)")
RE_ANCORA = re.compile(r"rndis|usb|ncm|ap\d", re.I)


def log(msg):
    print(f"[{__name__}] {msg}", flush=True)


def _enderecos_de_ancoragem(serial):
    """IPs que o celular usa do lado do cabo, vistos de dentro dele."""
    try:
        saida = rede.adb("shell", "ip", "-4", "-o", "addr", serial=serial)
    except RuntimeError:
        return set()
    achados = set()
    for linha in saida.splitlines():
        m = RE_INET.match(linha.strip())
        if m and RE_ANCORA.search(m.group(1)):
            achados.add(m.group(2))
    return achados


def _por_serie_usb(aparelhos, adaptadores):
    """Criterio 1. Devolve {ifIndex: serial} do que a serie USB resolver."""
    conhecidos = {a["serial"].upper(): a["serial"] for a in aparelhos}
    mapa = {}
    for ad in adaptadores:
        serie = rede.serial_usb(ad["pnp"])
        if serie and serie.upper() in conhecidos:
            mapa[ad["ifIndex"]] = conhecidos[serie.upper()]
    return mapa


def _por_gateway(aparelhos, adaptadores):
    """Criterio 2. Devolve {ifIndex: serial} pelo endereco do lado do cabo.

    So aceita casamento UNICO: se dois celulares sortearam a mesma faixa (o
    Android <=10 fixa 192.168.42.0/24 e nao combina nada com os vizinhos), o
    gateway deixa de identificar e o par cai para a sonda empirica.
    """
    lados = {a["serial"]: _enderecos_de_ancoragem(a["serial"]) for a in aparelhos}
    mapa = {}
    for ad in adaptadores:
        gw = ad.get("gateway")
        if not gw:
            continue
        donos = [s for s, ips in lados.items() if gw in ips]
        if len(donos) == 1:
            mapa[ad["ifIndex"]] = donos[0]
    return mapa


def _por_sonda(serial, candidatos):
    """Criterio 3. Derruba o dado do aparelho e ve qual adaptador emudece.

    Ultimo recurso, e mexe no celular - por isso religa o dado no finally mesmo
    se a sonda falhar no meio.
    """
    vivos = {ad["ifIndex"]: rede.ip_publico(timeout=5, origem=ad["ip"])
             for ad in candidatos}
    ativos = [i for i, ip in vivos.items() if ip]
    if len(ativos) < 2:
        # Um so tem saida: nao ha o que desempatar, e derrubar o dado do
        # aparelho a toa seria pior do que devolver o unico candidato.
        return ativos[0] if ativos else None
    try:
        rede.adb("shell", "svc", "data", "disable", serial=serial)
        for volta in range(6):
            if volta:
                # O desligamento do dado nao e instantaneo: sem a pausa o laco
                # gira seis vezes antes de o aparelho sequer perceber o comando.
                time.sleep(2)
            mudos = [i for i in ativos
                     if not rede.ip_publico(
                         timeout=4,
                         origem=next(a["ip"] for a in candidatos if a["ifIndex"] == i))]
            if len(mudos) == 1:
                return mudos[0]
        return None
    finally:
        try:
            rede.adb("shell", "svc", "data", "enable", serial=serial)
        except RuntimeError as e:
            log(f"AVISO: nao religuei o dado de {serial}: {str(e)[:70]}")


def parear(empirico=True):
    """Casa cada celular com o adaptador dele. Devolve a lista dos resolvidos.

    Cada item: serial, modelo, ifIndex, nome, ip, gateway, criterio.

    Levanta se dois adaptadores tiverem o MESMO IPv4: dois celulares que
    sortearam o mesmo endereco tornam a selecao de origem ambigua para o
    Windows, e seguir dali produziria workers saindo pelo aparelho errado sem
    aviso nenhum.
    """
    aparelhos = rede.listar_aparelhos()
    adaptadores = rede.adaptadores_tether()
    if not aparelhos:
        raise RuntimeError(
            "Nenhum aparelho autorizado no adb. Ligue a depuracao USB em cada "
            "celular e aceite o pedido que aparece na tela.")
    if not adaptadores:
        raise RuntimeError(
            f"{len(aparelhos)} aparelho(s) no adb, mas nenhum adaptador de "
            "tethering com endereco. Ligue 'Ancoragem USB' nas configuracoes "
            "de cada celular.")

    repetidos = [ip for ip in {a["ip"] for a in adaptadores}
                 if [a["ip"] for a in adaptadores].count(ip) > 1]
    if repetidos:
        raise RuntimeError(
            f"Dois adaptadores com o mesmo IPv4 ({', '.join(repetidos)}). O "
            "Windows nao consegue escolher a saida por endereco de origem "
            "assim. Desligue e religue a ancoragem de um dos celulares para "
            "ele sortear outra faixa.")

    mapa = _por_serie_usb(aparelhos, adaptadores)
    criterios = {i: "serie-usb" for i in mapa}

    por_gw = _por_gateway(aparelhos, adaptadores)
    for ifidx, serial in por_gw.items():
        if ifidx in mapa:
            if mapa[ifidx] != serial:
                log(f"AVISO: ifIndex {ifidx} casa com {mapa[ifidx]} pela serie "
                    f"USB e com {serial} pelo gateway. Ficando com a serie; "
                    "confira com `python dispositivos.py`.")
            continue
        if serial not in mapa.values():
            mapa[ifidx] = serial
            criterios[ifidx] = "gateway"

    if empirico:
        for ad in adaptadores:
            if ad["ifIndex"] in mapa:
                continue
            orfaos = [a["serial"] for a in aparelhos if a["serial"] not in mapa.values()]
            if len(orfaos) == 1:
                # Sobrou um de cada lado: nao ha o que sondar.
                mapa[ad["ifIndex"]] = orfaos[0]
                criterios[ad["ifIndex"]] = "eliminacao"
                continue
            candidatos = [x for x in adaptadores if x["ifIndex"] not in mapa]
            for serial in orfaos:
                achado = _por_sonda(serial, candidatos)
                if achado is not None:
                    mapa[achado] = serial
                    criterios[achado] = "sonda"
                    break

    modelos = {a["serial"]: a["modelo"] for a in aparelhos}
    saida = []
    for ad in adaptadores:
        serial = mapa.get(ad["ifIndex"])
        if not serial:
            log(f"AVISO: adaptador {ad['nome']} (ifIndex {ad['ifIndex']}, "
                f"{ad['ip']}) sem dono identificado; ficou de fora.")
            continue
        saida.append({"serial": serial, "modelo": modelos.get(serial, "?"),
                      "ifIndex": ad["ifIndex"], "nome": ad["nome"],
                      "ip": ad["ip"], "gateway": ad.get("gateway"),
                      "criterio": criterios.get(ad["ifIndex"], "?"),
                      "uplink": rede.uplink_do_aparelho(serial)})

    sem_adaptador = [a for a in aparelhos if a["serial"] not in mapa.values()]
    for a in sem_adaptador:
        log(f"AVISO: {a['serial']} ({a['modelo']}) esta no adb mas sem "
            "adaptador de rede. A 'Ancoragem USB' dele esta desligada.")
    return saida


SEM_SAIDA = "SEM SAIDA"
NO_WIFI = "ANCORANDO O WI-FI"


def situacao_de(pareado, publico):
    """'ok', SEM_SAIDA ou NO_WIFI - se o aparelho serve de worker e por que.

    A checagem que decide e o uplink do proprio celular, e nao a comparacao do
    IP publico dele com o do notebook. O motivo e que a segunda so funciona
    quando da para medir o IP do notebook, e amarrar na Wi-Fi dele pode falhar
    (aconteceu aqui: WSAEHOSTUNREACH com dois celulares plugados). O uplink vem
    do adb e sempre responde.
    """
    if not publico:
        return SEM_SAIDA
    if pareado.get("uplink") == "wifi":
        return NO_WIFI
    return "ok"


def diagnostico(empirico=True):
    """`python dispositivos.py` - imprime a tabela, nao coleta nada."""
    padrao = rede.dono_da_rota_padrao()
    print(f"adb em: {rede.caminho_adb()}")
    print(f"Rota padrao da maquina: ifIndex {padrao} "
          f"(IP publico {rede.ip_publico()})")
    try:
        pareados = parear(empirico=empirico)
    except RuntimeError as e:
        print(f"\nERRO: {e}")
        return 1

    print(f"\n{len(pareados)} aparelho(s) pareado(s):\n")
    print(f"  {'SERIAL':<20} {'MODELO':<16} {'IF':<4} {'IP LOCAL':<16} "
          f"{'IP PUBLICO':<16} {'CRITERIO':<12} SITUACAO")
    publicos, no_wifi, sem_saida = [], [], []
    for d in pareados:
        publico = rede.ip_publico(origem=d["ip"])
        situacao = situacao_de(d, publico)
        if situacao == "ok":
            publicos.append(publico)
        elif situacao == SEM_SAIDA:
            sem_saida.append(d)
        else:
            no_wifi.append(d)
        print(f"  {d['serial']:<20} {d['modelo']:<16} {d['ifIndex']:<4} "
              f"{d['ip']:<16} {str(publico):<16} {d['criterio']:<12} {situacao}")

    if no_wifi:
        quais = ", ".join(f"{d['modelo']} ({d['serial']})" for d in no_wifi)
        print(f"\nAVISO: {quais} esta ancorando o WI-FI, nao o dado movel. O "
              "Android prefere Wi-Fi quando as duas conexoes estao ligadas, "
              "entao mesmo com dados moveis ativos o trafego sai pela rede de "
              "casa - o MESMO IP do notebook. Desligue o Wi-Fi DO CELULAR.")
    if sem_saida:
        quais = ", ".join(f"{d['modelo']} ({d['serial']})" for d in sem_saida)
        print(f"\nAVISO: {quais} esta sem internet; confira o dado movel dele.")
    if len(set(publicos)) < len(publicos):
        print("\nERRO: dois aparelhos saem pelo MESMO IP publico. Ou estao no "
              "mesmo NAT, ou o pareamento esta errado - nao rode a coleta "
              "paralela assim, ela nao compraria IP nenhum.")
        return 1
    print(f"\n{len(publicos)} aparelho(s) utilizavel(is) na coleta paralela.")
    if any(d["ifIndex"] == padrao for d in pareados):
        print("\nAVISO: a rota padrao da maquina esta num dos celulares. A "
              "coleta paralela funciona (tudo dela sai amarrado), mas o "
              "trafego solto da maquina gasta a franquia desse aparelho.")
    return 0


if __name__ == "__main__":
    sys.exit(diagnostico(empirico="--rapido" not in sys.argv[1:]))
