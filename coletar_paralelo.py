#!/usr/bin/env python3
r"""
Uma extracao por celular plugado, todas ao mesmo tempo.

O que destravou isso: ate agora "sair pelo celular" era pisar na rota padrao da
maquina (rede.assumir_rota), e rota padrao so ha uma - por isso o
maturar_paralelo.py desliga a rotacao nos filhos, "dois processos trocando o IP
do mesmo tether se derrubariam". Aqui a saida e escolhida por SOCKET, nao por
rota: cada worker sobe um SOCKS local amarrado ao IP do adaptador do SEU
aparelho (proxy_local.py) e o Chrome dele sai por ali. A rota padrao fica no
Wi-Fi, ninguem precisa de Administrador, e N celulares rendem N IPs de operadora
simultaneos.

Cada worker e um orquestrador inteiro rodando lote_forcado() - o mesmo caminho de
login, 2a via, historico e download de sempre. O que este arquivo faz e so:
descobrir os aparelhos, dividir a fila e instalar em cada worker o rotacionador
que sabe qual celular e o dele.

Uso:
    python coletar_paralelo.py --mes 07/2026            # 1 worker por celular
    python coletar_paralelo.py --mes 07/2026 -n 2       # limita os workers
    python coletar_paralelo.py --mes 07/2026 --limite 1 # 1 titular por worker (fumaca)
    python coletar_paralelo.py --dispositivos           # so a tabela, nao coleta
    python coletar_paralelo.py --shard 0/3              # interno, chamado pelo pai

--mes e OBRIGATORIO. Sem ele quem decide o que falta e a agenda - justamente o
estado compartilhado que este modo mantem fora do caminho. Com ele, quem decide
e o disco (orquestrador.ja_baixada), que e por UC e nao disputa com ninguem.
"""

import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import baixar_fatura as bf
import dispositivos
import personas as P
import proxy_local
import rede
from enviar_drive import carregar_env

RAIZ = Path(__file__).parent
# Bandeira de parada limpa. O pai a levanta no Ctrl+C e os workers desistem
# ENTRE titulares - matar no meio deixaria um Chrome de pe e, pior, um perfil
# fechado no braco (o LevelDB com o device ID da persona so grava no encerramento
# gracioso; ver navegador.derrubar).
BANDEIRA = RAIZ / "estado" / "parar.flag"

# Largada escalonada. Quatro logins na Equatorial no mesmo segundo, vindos de
# quatro IPs, e um padrao de velocity que nao custa nada remover.
LARGADA_MAX = int(os.environ.get("EQUATORIAL_LARGADA_MAX", "90"))
# Espera pelo encerramento gracioso de cada worker no Ctrl+C, antes de insistir.
PRAZO_SAIDA_S = 120


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] par: {msg}", flush=True)


# --------------------------------------------------------------------- fila

def fatias(total, limite=None):
    """Divide a fila em `total` partes, uma por worker. Devolve lista de listas.

    Fatiar por TITULAR e nao por UC e carga, nao estilo: as UCs de um mesmo
    documento dividem um unico perfil de Chrome (personas.persona_de), e
    navegador.subir() derruba qualquer Chrome que ja esteja segurando aquele
    perfil. Dois workers no mesmo titular se matariam no meio do login.

    A lista vem de carregar_contas(atualizar=False) - o cache que o pai acabou
    de atualizar -, ordenada por UC, para que pai e filhos cheguem exatamente a
    mesma divisao sem precisar passar nada pela linha de comando.
    """
    contas = sorted(P.carregar_contas(atualizar=False), key=lambda c: c["uc"])
    import orquestrador as orq

    grupos = orq.por_titular(contas)
    saida = []
    for i in range(total):
        meus = grupos[i::total]
        if limite:
            meus = meus[:limite]
        saida.append([c for g in meus for c in g])
    return saida


# ------------------------------------------------------------------- worker

def _rotacionador(serial, if_index, proxy):
    """Troca o IP DESTE celular e reamarra o proxy no endereco novo.

    Reamarrar nao e zelo: o IP do adaptador vem do DHCP do proprio celular, e o
    tranco do modo aviao pode reerguer o tethering em outra faixa (o Android 11+
    sorteia um /24 por sessao de ancoragem). Proxy presoo no endereco velho
    recusa toda conexao com WSAEADDRNOTAVAIL, e o sintoma chega ao log como
    motivo 'rede' em todas as UCs seguintes - falha cara de diagnosticar por um
    motivo barato de evitar.
    """
    def trocar():
        antes, depois = rede.rotacionar(serial=serial, if_index=if_index)
        novo = rede.esperar_origem(if_index)
        if novo:
            proxy.definir_origem(novo)
        else:
            log(f"{serial}: adaptador nao voltou com endereco depois da rotacao")
        return antes, depois

    return trocar


def trabalhador(indice, total, limite=None):
    """Roda a fatia `indice` atras do celular apontado pelo ambiente."""
    import orquestrador as orq
    import navegador as nav
    import agenda as AG

    serial = os.environ["EQUATORIAL_DISPOSITIVO"]
    if_index = int(os.environ["EQUATORIAL_IFINDEX"])

    # Primeira coisa: fotografar o celular e agendar a devolucao para TODO
    # caminho de saida (fim normal, excecao, Ctrl+C, SIGTERM). Um worker morto
    # dentro da janela de 6s do modo aviao deixaria o aparelho mudo.
    rede.guardar_aparelho(serial)

    origem = rede.origem_de(if_index)
    if not origem:
        log(f"trabalhador {indice}: adaptador {if_index} sem endereco; desisto")
        return 1

    proxy = proxy_local.ProxyLocal(origem)
    proxy.iniciar()
    nav.PROXY = proxy.url
    # Pasta de download so deste worker: esperar_arquivo() captura "qualquer PDF
    # novo na pasta", entao compartilhar faturas/ faria um worker arquivar o PDF
    # do outro sob a UC errada. Fica dentro de faturas/ para o rename final nao
    # atravessar volume.
    bf.DESCARGA = bf.FATURAS / f".baixando-{indice}"
    bf.DESCARGA.mkdir(parents=True, exist_ok=True)

    try:
        publico = rede.ip_publico(origem=origem)
        if not publico:
            log(f"trabalhador {indice}: {serial} sem saida para a internet "
                "(dado movel desligado?); desisto antes de gastar sessao")
            return 1
        fatia = fatias(total, limite)[indice]
        if not fatia:
            log(f"trabalhador {indice}: nada na fatia")
            return 0
        log(f"trabalhador {indice}: {serial} pelo IP {publico}, "
            f"{len(fatia)} conta(s), proxy {proxy.url}")

        espera = random.randint(0, LARGADA_MAX)
        log(f"trabalhador {indice}: comecando em {espera}s")
        time.sleep(espera)

        orq.MES_ALVO = bf.parse_mes(os.environ["EQUATORIAL_MES"])
        orq.ROTACIONADOR = _rotacionador(serial, if_index, proxy)
        orq.PARAR = BANDEIRA.exists
        return orq.lote_forcado(fatia, AG.Agenda())
    finally:
        proxy.parar()
        try:
            if bf.DESCARGA != bf.FATURAS and not any(bf.DESCARGA.iterdir()):
                bf.DESCARGA.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------- pai

def _prontos(empirico=True):
    """Aparelhos pareados que de fato entregam um IP de operadora proprio."""
    pareados = dispositivos.parear(empirico=empirico)
    prontos, publicos = [], {}
    for d in pareados:
        ip = rede.ip_publico(origem=d["ip"])
        situacao = dispositivos.situacao_de(d, ip)
        if situacao == dispositivos.SEM_SAIDA:
            log(f"{d['serial']} ({d['modelo']}) nao tem saida para a internet; "
                "fora da rodada. Confira o dado movel dele.")
            continue
        if situacao == dispositivos.NO_WIFI:
            # O Android prefere Wi-Fi quando as duas conexoes estao ligadas,
            # entao este aparelho ancora a rede de casa e sai pelo MESMO IP
            # publico do notebook. Usa-lo seria trabalhar com o IP residencial
            # achando que e o da operadora - e e justamente o IP que o
            # diagnostico deste projeto ja mostrou recusado.
            log(f"{d['serial']} ({d['modelo']}) sai por {ip} ancorando o Wi-Fi, "
                "nao o dado movel. Desligue o Wi-Fi DELE. Fora da rodada.")
            continue
        publicos[d["serial"]] = ip
        prontos.append(d)

    repetidos = {ip for ip in publicos.values()
                 if list(publicos.values()).count(ip) > 1}
    if repetidos:
        raise RuntimeError(
            f"Aparelhos saindo pelo MESMO IP publico ({', '.join(repetidos)}). "
            "Ou estao no mesmo NAT, ou o pareamento errou - rodar assim nao "
            "compraria IP nenhum. Confira com `python dispositivos.py`.")

    for d in prontos:
        log(f"  {d['serial']:<20} {d['modelo']:<14} ifIndex={d['ifIndex']:<4} "
            f"{d['ip']:<15} -> {publicos[d['serial']]}  ({d['criterio']})")
    return prontos


def _devolver_aparelhos(seriais):
    """Rede de seguranca do pai: modo aviao desligado e dado ligado em todos.

    O worker ja restaura o proprio celular no finally. Isto cobre o caso em que
    ele nao chegou la - morto com /F, travado, ou derrubado junto do console.
    Restaurar duas vezes nao custa nada; nao restaurar deixa um celular mudo.
    """
    for serial in seriais:
        for args in (("shell", "cmd", "connectivity", "airplane-mode", "disable"),
                     ("shell", "svc", "data", "enable")):
            try:
                rede.adb(*args, serial=serial)
            except RuntimeError as e:
                log(f"AVISO: {serial} nao respondeu a {args[-1]}: {str(e)[:60]}")


def principal(mes, n=None, limite=None, empirico=True):
    log("Procurando celulares...")
    prontos = _prontos(empirico=empirico)
    if not prontos:
        raise RuntimeError(
            "Nenhum celular pronto. Cada aparelho precisa de: cabo USB, "
            "depuracao USB autorizada, 'Ancoragem USB' ligada e dado movel "
            "funcionando. Diagnostico: `python dispositivos.py`.")
    if n:
        prontos = prontos[:n]

    # A fila e lida UMA vez aqui, com atualizacao: os filhos leem o cache
    # (atualizar=False), entao todos enxergam a mesma lista e chegam a mesma
    # divisao. Buscar na API em cada filho daria listas diferentes e fatias
    # sobrepostas - dois workers no mesmo titular.
    contas = P.carregar_contas()
    total = len(prontos)
    divisao = fatias(total, limite)
    log(f"{len(contas)} conta(s) na fila, {total} celular(es), "
        f"mes {mes[0]:02d}/{mes[1]}")
    for i, (d, f) in enumerate(zip(prontos, divisao)):
        log(f"  worker {i}: {d['modelo']} ({d['serial']}) <- {len(f)} conta(s)")
    if not any(divisao):
        log("Nada a coletar")
        return 0

    BANDEIRA.parent.mkdir(parents=True, exist_ok=True)
    BANDEIRA.unlink(missing_ok=True)
    bf.FATURAS.mkdir(exist_ok=True)
    P.PERFIS.mkdir(exist_ok=True)

    filhos = []
    for i, d in enumerate(prontos):
        ambiente = dict(
            os.environ,
            EQUATORIAL_DISPOSITIVO=d["serial"],
            EQUATORIAL_IFINDEX=str(d["ifIndex"]),
            EQUATORIAL_MES=f"{mes[0]:02d}/{mes[1]}",
            EQUATORIAL_ROTACIONAR="1",
            # Uma base de porta CDP por worker: a varredura do
            # navegador.porta_livre tem corrida entre processos.
            EQUATORIAL_PORTA_BASE=str(9222 + i * 100),
        )
        args = [sys.executable, __file__, "--shard", f"{i}/{len(prontos)}"]
        if limite:
            args += ["--limite", str(limite)]
        filhos.append(subprocess.Popen(args, env=ambiente))

    codigos = []
    try:
        codigos = [p.wait() for p in filhos]
    except KeyboardInterrupt:
        log("Interrompido: pedindo parada entre titulares e esperando os "
            f"workers fecharem o Chrome (ate {PRAZO_SAIDA_S}s cada)")
        BANDEIRA.touch()
        for p in filhos:
            try:
                p.wait(timeout=PRAZO_SAIDA_S)
            except subprocess.TimeoutExpired:
                log(f"worker pid {p.pid} nao saiu a tempo; encerrando")
                p.terminate()
        codigos = [p.returncode for p in filhos]
    finally:
        BANDEIRA.unlink(missing_ok=True)
        _devolver_aparelhos([d["serial"] for d in prontos])

    log("===== RESUMO =====")
    for i, (d, c) in enumerate(zip(prontos, codigos)):
        log(f"  worker {i} ({d['modelo']}): saida {c}")
    baixados = len(list(bf.FATURAS.glob(f"fatura-*-{mes[0]}-{mes[1]}.pdf")))
    log(f"{baixados} fatura(s) de {mes[0]:02d}/{mes[1]} em faturas/")
    return 0 if all(c == 0 for c in codigos) else 1


def main():
    carregar_env()
    import orquestrador as orq

    args, mes = orq.extrair_mes(sys.argv[1:])
    if mes is None:
        mes = bf.mes_do_ambiente()

    def valor(*flags, tipo=int):
        for f in flags:
            if f in args:
                return tipo(args[args.index(f) + 1])
        return None

    rapido = "--rapido" in args

    if "--dispositivos" in args:
        return dispositivos.diagnostico(empirico=not rapido)

    if "--shard" in args:
        i, n = (int(x) for x in args[args.index("--shard") + 1].split("/"))
        return trabalhador(i, n, valor("--limite"))

    if not mes:
        sys.exit(
            "--mes e obrigatorio (ex.: --mes 07/2026). Sem ele quem decide o "
            "que falta e a agenda, que e estado compartilhado entre os "
            "workers; com ele quem decide e o PDF em faturas/, que e por UC.")

    try:
        return principal(mes, n=valor("-n", "--workers"),
                         limite=valor("--limite"), empirico=not rapido)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    sys.exit(main())
