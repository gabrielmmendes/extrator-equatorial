#!/usr/bin/env python3
r"""
Varios workers do gcenergy na mesma maquina, um por celular.

Um worker so faz uma sessao de portal por vez, e todas saindo pelo mesmo IP.
Com mil+ UCs (~47 sessoes/dia, mais com o crescimento), a escala vem de IPs, nao
de pressa: cada celular plugado vira um worker inteiro (worker.laco), saindo
pelo IP da operadora do PROPRIO aparelho e rotacionando so o dele entre uma
sessao e outra.

Tudo o que torna isso possivel ja existe no coletar_paralelo.py e e reusado
aqui: o pareamento celular <-> adaptador (dispositivos.py), o SOCKS amarrado ao
adaptador (proxy_local.py), o rotacionador que reamarra o proxy depois do modo
aviao e a devolucao dos aparelhos no fim. A diferenca de fundo: la a fila e
fatiada por titular no inicio; aqui a fila e do servidor, e quem chega primeiro
reserva. Os perfis sao o pool fixo de personas do worker.py, dividido entre os
workers pela trava de cada persona: dois jobs que caiam na mesma persona (mesmo
titular, ou titulares do mesmo grupo) rodam um depois do outro. Na primeira
largada os workers maturam o pool juntos, cada um pelo seu celular.

Cada filho tem:
  - X-Worker-Id proprio: <GCENERGY_WORKER_ID>-01, -02, ... (fixo por posicao,
    para a recuperacao do job em maos funcionar entre reinicios);
  - pasta de download propria (faturas/.baixando-wN);
  - base de porta do CDP propria.
Compartilham: o pool de personas, faturas/, a agenda (com trava entre
processos) e o indice do Drive. Nao ha teto diario de sessoes: cada worker emenda um job no
outro dentro da janela de horario.

Uso:
    python worker_paralelo.py                 # um worker por celular pronto
    python worker_paralelo.py -n 2            # no maximo 2 workers
    python worker_paralelo.py --rapido        # pareia sem a sonda empirica
    python worker_paralelo.py --dispositivos  # so a tabela de aparelhos
    python worker_paralelo.py --shard 0/3     # interno, chamado pelo pai

Parada: Ctrl+C (cada worker devolve o job em maos como PENDENTE, fecha o Chrome
com jeito e sai), ou crie estado/parar-worker.flag para parar ENTRE jobs.
"""

# credenciais.env antes de qualquer outro import do projeto (ver worker.py).
from enviar_drive import carregar_env

carregar_env()

import os  # noqa: E402
import random  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import baixar_fatura as bf  # noqa: E402
import coletar_paralelo as CP  # noqa: E402
import dispositivos  # noqa: E402
import gcenergy as GC  # noqa: E402
import navegador as nav  # noqa: E402
import orquestrador as orq  # noqa: E402
import personas as P  # noqa: E402
import proxy_local  # noqa: E402
import rede  # noqa: E402
import worker as W  # noqa: E402

RAIZ = Path(__file__).parent
BANDEIRA = RAIZ / "estado" / "parar-worker.flag"
# Ctrl+C no meio de um job: o filho sai do portal, fecha o Chrome com jeito
# (ate ~25 s) e devolve o job - um PATCH que repete por ate ~2 min se a rede
# falhar. 120 s, como no coletar_paralelo, cortaria esse ultimo passo.
PRAZO_SAIDA_S = 240


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] wpar: {msg}", flush=True)


# ------------------------------------------------------------------- filho

def trabalhador(indice):
    """Um worker inteiro atras do celular apontado pelo ambiente."""
    serial = os.environ["EQUATORIAL_DISPOSITIVO"]
    if_index = int(os.environ["EQUATORIAL_IFINDEX"])
    nome = GC.WORKER_ID

    # Antes de tudo: fotografar o celular e agendar a devolucao para todo
    # caminho de saida. Morrer dentro do modo aviao deixaria o aparelho mudo.
    rede.guardar_aparelho(serial)

    origem = rede.origem_de(if_index)
    if not origem:
        log(f"{nome}: adaptador {if_index} sem endereco; desisto")
        return 1
    proxy = proxy_local.ProxyLocal(origem)
    proxy.iniciar()
    nav.PROXY = proxy.url
    # esperar_arquivo() captura "qualquer PDF novo na pasta": pasta de download
    # compartilhada faria um worker arquivar o PDF do outro sob a UC errada.
    bf.DESCARGA = bf.FATURAS / f".baixando-w{indice}"
    bf.DESCARGA.mkdir(parents=True, exist_ok=True)
    try:
        publico = rede.ip_publico(origem=origem)
        if not publico:
            log(f"{nome}: {serial} sem saida para a internet (dado movel "
                "desligado?); desisto antes de gastar sessao")
            return 1
        log(f"{nome}: {serial} pelo IP {publico}, proxy {proxy.url}")

        # Largada escalonada: tres logins na Equatorial no mesmo segundo, de tres
        # IPs, e um padrao de velocity que nao custa nada remover.
        espera = random.randint(0, CP.LARGADA_MAX)
        log(f"{nome}: comecando em {espera}s")
        W._dormir(time.time() + espera, BANDEIRA.exists)

        orq.ROTACIONADOR = CP._rotacionador(serial, if_index, proxy)
        return W.laco(W.Drive(), gerenciar_rede=False, parar=BANDEIRA.exists)
    except GC.ErroChave as e:
        log(f"{nome}: PARANDO: {e}. Repetir nao resolve - confira GCENERGY_WORKER_KEY.")
        return 2
    except KeyboardInterrupt:
        log(f"{nome}: interrompido")
        return 130
    finally:
        proxy.parar()
        try:
            if bf.DESCARGA != bf.FATURAS and not any(bf.DESCARGA.iterdir()):
                bf.DESCARGA.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------- pai

def principal(n=None, empirico=True):
    # Chave e Drive antes de mexer em celular. Se o JSON do Drive for OAuth, o
    # navegador de autorizacao abre UMA vez aqui, no pai; os filhos reusam o
    # token gravado em estado/drive_token.json.
    log(GC.checar_chave())
    drive = W.Drive()
    log(f"Drive: envia como {drive.conta()} para a pasta {drive.pasta}")

    log("Procurando celulares...")
    prontos = CP._prontos(empirico=empirico)
    if not prontos:
        raise RuntimeError(
            "Nenhum celular pronto. Cada aparelho precisa de: cabo USB, "
            "depuracao USB autorizada, 'Ancoragem USB' ligada, dado movel "
            "funcionando e Wi-Fi DELE desligado. Diagnostico: "
            "`python worker_paralelo.py --dispositivos`.")
    if n:
        prontos = prontos[:n]

    BANDEIRA.parent.mkdir(parents=True, exist_ok=True)
    BANDEIRA.unlink(missing_ok=True)
    bf.FATURAS.mkdir(exist_ok=True)
    P.PERFIS.mkdir(exist_ok=True)

    filhos, nomes = [], []
    for i, d in enumerate(prontos):
        nome = f"{GC.WORKER_ID}-{i + 1:02d}"
        ambiente = dict(
            os.environ,
            EQUATORIAL_DISPOSITIVO=d["serial"],
            EQUATORIAL_IFINDEX=str(d["ifIndex"]),
            EQUATORIAL_ROTACIONAR="1",
            # Uma base de porta CDP por worker: a varredura do
            # navegador.porta_livre tem corrida entre processos.
            EQUATORIAL_PORTA_BASE=str(9222 + i * 100),
            GCENERGY_WORKER_ID=nome,
        )
        ambiente.pop("EQUATORIAL_PROXY", None)
        log(f"  {nome}: {d['modelo']} ({d['serial']})")
        nomes.append(nome)
        filhos.append(subprocess.Popen(
            [sys.executable, __file__, "--shard", f"{i}/{len(prontos)}"], env=ambiente))

    codigos = []
    try:
        codigos = [p.wait() for p in filhos]
    except KeyboardInterrupt:
        log(f"Interrompido: esperando os workers devolverem os jobs e fecharem "
            f"o Chrome (ate {PRAZO_SAIDA_S}s cada)")
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
        CP._devolver_aparelhos([d["serial"] for d in prontos])

    log("===== RESUMO =====")
    for nome, d, c in zip(nomes, prontos, codigos):
        log(f"  {nome} ({d['modelo']}): saida {c}")
    return 0 if all(c in (0, 130) for c in codigos) else 1


def main():
    args = sys.argv[1:]
    rapido = "--rapido" in args
    if "--dispositivos" in args:
        return dispositivos.diagnostico(empirico=not rapido)
    if "--shard" in args:
        indice = int(args[args.index("--shard") + 1].split("/")[0])
        return trabalhador(indice)
    n = None
    for flag in ("-n", "--workers"):
        if flag in args:
            n = int(args[args.index(flag) + 1])
    try:
        return principal(n=n, empirico=not rapido)
    except GC.ErroChave as e:
        sys.exit(f"API recusou a chave: {e}")
    except (RuntimeError, GC.ErroAPI) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
