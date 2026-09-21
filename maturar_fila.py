#!/usr/bin/env python3
"""Matura os perfis das UCs que ja estao na fila local, sem consultar a API.

Existe para separar duas coisas que o --maturar do orquestrador junta: buscar
mais tarefas e preparar perfis. Uma busca traz ate MF_API_LIMIT tarefas novas
(50 por padrao), e maturar 50 perfis de uma vez nao e o que se quer quando o
pedido era "prepare os que ja estao na fila".
"""

import sys

import api
import orquestrador as orq
import personas as P


if __name__ == "__main__":
    P.PERFIS.mkdir(exist_ok=True)
    contas = api.contas(atualizar=False)
    if not contas:
        sys.exit("Fila local vazia. Rode 'python api.py' para buscar tarefas.")
    pendentes = [c for c in contas
                 if not P.persona_de(c["doc"], c["apelido"]).meta().get("maturado_em")]
    print(f"Fila local: {len(contas)} UC(s); {len(pendentes)} perfil(is) por maturar; "
          f"pausa de {orq.MATURACAO_MIN}-{orq.MATURACAO_MAX}s entre eles")
    orq.maturar_pendentes(contas)
