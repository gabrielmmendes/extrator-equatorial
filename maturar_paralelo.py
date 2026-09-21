#!/usr/bin/env python3
"""Matura varios perfis ao mesmo tempo, em processos separados.

Por que paralelizar aqui e nao no login: a maturacao nao toca a Equatorial. O
passeio e por Google, portais de noticia e Wikipedia (navegador.SITES_MATURACAO),
entao nao ha reputacao da agencia virtual a preservar - o unico limite e o
proprio Google, que pontua velocity. Com poucos trabalhadores e a pausa de
sempre entre um perfil e o seguinte, cada trabalhador continua no mesmo ritmo
de antes; o que muda e que N deles andam juntos.

O ganho e tirar a maturacao do caminho critico da coleta: hoje cada UC de perfil
novo paga ~70s de passeio ANTES de poder logar, em serie com todo o resto.

Uso:
    python maturar_paralelo.py            # 4 trabalhadores
    python maturar_paralelo.py -n 6       # 6 trabalhadores
    python maturar_paralelo.py --lista    # so mostra o que falta maturar

A rotacao de IP fica desligada nos filhos de proposito: dois processos trocando
o IP do mesmo tether se derrubariam no meio do passeio um do outro.
"""

import os
import random
import subprocess
import sys
import time

import navegador as nav
import personas as P

# Poucos de proposito: o teto util nao e a CPU, e o Google vendo N perfis novos
# no mesmo IP. 4 ja da 4x sem transformar o passeio num padrao obvio.
PADRAO = int(os.environ.get("EQUATORIAL_MATURACAO_WORKERS", "4"))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] mat: {msg}", flush=True)


def pendentes(contas):
    """Perfis por maturar, sem os que estao em uso agora.

    Um perfil com Chrome vivo esta numa sessao do orquestrador: nav.subir()
    derruba o Chrome anterior do mesmo perfil antes de abrir o seu, entao mexer
    nele aqui mataria um login em andamento.
    """
    vistos, saida = set(), []
    for c in contas:
        doc = P.so_digitos(c["doc"])
        if doc in vistos:
            continue  # um perfil por titular, nao por UC
        vistos.add(doc)
        persona = P.persona_de(c["doc"], c["apelido"])
        if persona.meta().get("maturado_em"):
            continue
        if nav._pids_do_perfil(persona):
            log(f"{persona.apelido}: perfil em uso agora; deixando para depois")
            continue
        saida.append(c)
    return saida


def trabalhador(indice, total):
    """Matura a fatia `indice` da fila. Roda num processo proprio."""
    import orquestrador as orq

    contas = sorted(P.carregar_contas(atualizar=False), key=lambda c: c["uc"])
    fatia = pendentes(contas)[indice::total]
    if not fatia:
        log(f"trabalhador {indice}: nada a fazer")
        return 0
    # Largada escalonada: N navegadores abrindo o Google no mesmo segundo e o
    # padrao de velocity mais barato de detectar que existe.
    espera = random.randint(0, 25)
    log(f"trabalhador {indice}: {len(fatia)} perfil(is), comecando em {espera}s")
    time.sleep(espera)
    return orq.maturar_pendentes(fatia)


def main():
    args = sys.argv[1:]
    contas = P.carregar_contas(atualizar=False)
    fila = pendentes(contas)

    if "--lista" in args:
        log(f"{len(fila)} perfil(is) por maturar, de {len(contas)} conta(s) na fila")
        for c in fila:
            print(f"  {c['apelido']:<10} uc={c['uc']} perfil="
                  f"{P.persona_de(c['doc'], c['apelido']).dir.name}")
        return 0

    if "--shard" in args:
        i, n = (int(x) for x in args[args.index("--shard") + 1].split("/"))
        return 0 if trabalhador(i, n) is not None else 1

    n = PADRAO
    for flag in ("-n", "--workers"):
        if flag in args:
            n = max(1, int(args[args.index(flag) + 1]))
    if not fila:
        log("Nenhum perfil novo para maturar")
        return 0
    n = min(n, len(fila))
    log(f"{len(fila)} perfil(is) por maturar em {n} processo(s)")

    ambiente = dict(os.environ, EQUATORIAL_ROTACIONAR="0")
    filhos = [subprocess.Popen([sys.executable, __file__, "--shard", f"{i}/{n}"],
                               env=ambiente)
              for i in range(n)]
    codigos = [p.wait() for p in filhos]

    restam = pendentes(P.carregar_contas(atualizar=False))
    log(f"Fim: {len(fila) - len(restam)} perfil(is) maturado(s), {len(restam)} restante(s)")
    return 0 if all(c == 0 for c in codigos) else 1


if __name__ == "__main__":
    sys.exit(main())
