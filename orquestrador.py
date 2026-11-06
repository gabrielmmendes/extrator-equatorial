#!/usr/bin/env python3
"""
Laco principal: uma conta por vez, espalhadas pelo mes.

Diferenca de fundo para coletar_varias.py, que este arquivo substitui:

  coletar_varias.py            orquestrador.py
  -----------------            ---------------
  1 perfil Chrome para todas   1 perfil por titular, permanente
  clear_cookies() entre contas troca o processo do Chrome inteiro
  todas as UCs em sequencia    1 UC por vez, IP novo a cada troca de UC
  ate 50 tentativas em 51 min  1 tentativa por UC por dia
  backoff em segundos          backoff em horas/dias, por motivo
  rotaciona IP a cada recusa   rotaciona IP entre contas, nunca no meio

O que permite tudo isso e a folga do problema: cada UC precisa de UMA fatura
por mes. Com N UCs, N/30 logins por dia fecham o ciclo - 2/dia para 60 UCs,
10/dia para 300. Nao ha razao para correr.

Uso:
    python orquestrador.py            # laco continuo (o modo normal)
    python orquestrador.py --uma      # roda uma conta e sai
    python orquestrador.py --maturar  # so prepara perfis novos, sem logar
    python orquestrador.py --agora UC # forca uma UC, ignorando a agenda
    python orquestrador.py --status   # relatorio da agenda

    --mes MM/AAAA baixa um mes especifico em vez da fatura mais recente. So faz
    sentido junto de --agora (uma UC ou "todas"): no laco normal quem escolhe o
    mes e a agenda, que cobra a fatura do ciclo corrente.
"""

import os
import random
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import agenda as AG
import baixar_fatura as bf
import humano as H
import login
import navegador as nav
import personas as P
import segunda_via as sv

ROTACIONAR = os.environ.get("EQUATORIAL_ROTACIONAR", "0") == "1"
# Ganchos que o coletar_paralelo.py instala nos workers dele. Vazios aqui, e e
# isso que mantem este arquivo ignorante de celular: o worker e quem sabe QUAL
# aparelho e o dele e o que mais precisa acontecer junto da troca de IP (reamarrar
# o proxy no endereco novo do adaptador). Seguem o mesmo padrao de global entre
# modulos que login.UC/login.DOC e o MES_ALVO abaixo ja usam.
ROTACIONADOR = None   # callable() que troca o IP; None = rede.rotacionar() padrao
PARAR = None          # callable() -> bool, consultado entre titulares no lote
# (mes, ano) pedido na linha de comando; None = fatura mais recente. Global
# porque atravessa toda a cadeia (lote -> conta -> baixar) sem que nenhum passo
# intermediario tenha o que decidir sobre ele.
MES_ALVO = None


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] orq: {msg}", flush=True)


def dormir_ate(ts, motivo=""):
    espera = max(0, ts - time.time())
    if espera <= 0:
        return
    alvo = datetime.fromtimestamp(ts).strftime("%d/%m %H:%M:%S")
    log(f"Dormindo {espera / 60:.0f} min ate {alvo}" + (f" ({motivo})" if motivo else ""))
    # Fatiado para o Ctrl+C responder rapido.
    fim = time.time() + espera
    while time.time() < fim:
        time.sleep(min(30, fim - time.time()))


def conectar(porta):
    """Sessao Playwright ligada ao Chrome ja em execucao naquela porta."""
    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{porta}")
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return p, browser, ctx, page


def rotacionar_ip():
    """Troca o IP ANTES de abrir o navegador da proxima persona.

    Nunca no meio de uma sessao: um dispositivo que troca de IP entre o load da
    pagina e o submit e uma incoerencia que o Imperva pontua sozinha. Entre
    sessoes e o oposto - e o que acontece com qualquer celular na rua.
    """
    if not ROTACIONAR:
        return
    try:
        if ROTACIONADOR:
            antes, depois = ROTACIONADOR()
        else:
            import rede
            antes, depois = rede.rotacionar()
        log(f"IP {antes} -> {depois}" + ("  (sem mudanca)" if antes == depois else ""))
    except Exception as e:
        log(f"Rotacao de IP falhou ({str(e)[:80]}); seguindo com o IP atual")


def ja_baixada(conta):
    """Caminho do PDF que esta execucao ja produziu para a conta, ou None.

    So existe para o lote com --mes: ali a agenda nao e tocada (ver concluir()),
    entao um lote interrompido nao tem onde ler o que ja fez. O disco tem: o
    nome do arquivo carrega UC, mes e ano. Sem isso, retomar um lote de 180
    contas significa refazer as que ja vieram - horas de sessao gastas para
    baixar de novo um PDF que ja esta em faturas/.
    """
    if not MES_ALVO:
        return None
    mes, ano = MES_ALVO
    alvo = bf.FATURAS / f"fatura-{conta['uc']}-{mes}-{ano}.pdf"
    if alvo.exists() and alvo.stat().st_size > 1000:
        return alvo
    return None


def _garantir_deslogado(page, persona):
    """Fecha qualquer sessao aberta antes de logar a proxima UC do titular.

    tentar_login() aceita de bom grado uma sessao ja valida - e o certo quando o
    perfil tem uma UC so. Num titular com varias, a sessao herdada e da UC
    ANTERIOR: seguir com ela baixaria de novo a fatura da outra UC e salvaria
    com o nome desta. Por isso a sessao morre entre uma UC e a seguinte.
    """
    try:
        if not login.area_logada(page, timeout_s=6):
            return True
    except Exception:
        return True
    return bool(login.sair(page))


# Vida util da sessao ASP.NET do portal, com folga. Passado isso nao ha sessao a
# herdar, e sondar por uma custa um page load + os 8s de area_logada a toa.
SESSAO_VALE_S = 20 * 60


def _uma_uc(page, ctx, conta, persona, ag, dialogos, sozinha):
    """Loga, baixa e desloga UMA UC dentro de uma sessao ja aberta."""
    login.UC, login.DOC = conta["uc"], conta["doc"]
    login.NASC = conta.get("nasc", "")

    # So ha sessao a herdar se este perfil logou ha pouco - o caso de uma
    # execucao anterior interrompida no meio. No fluxo normal (perfil parado ha
    # dias, ou UC anterior deslogada por _garantir_deslogado) nao ha, e a sonda
    # so gastaria tempo de relogio para confirmar o obvio.
    desde = time.time() - persona.meta().get("ultimo_login", 0)
    ok, motivo = login.tentar_login(page, dialogos, persona,
                                    sessao_possivel=desde < SESSAO_VALE_S)
    if not ok:
        # Simetrico ao concluir(): com um mes escolhido a mao a agenda nao e
        # tocada, nem no sucesso nem na falha. Anotar a falha aqui punia o ciclo
        # CORRENTE por causa de um recado historico - a UC entrava em backoff de
        # ate 72h sem nunca ter sido cobrada pelo mes dela. De quebra, e o que
        # tira quase toda a disputa pela agenda no modo paralelo.
        if not MES_ALVO:
            ag.registrar_falha(conta["uc"], motivo)
        return motivo
    if motivo == "sessao" and not sozinha:
        # Nao deveria acontecer: _garantir_deslogado roda antes. Se aconteceu, a
        # sessao aberta e de outra UC do mesmo titular e o download viria
        # trocado - abortar e mais barato que gravar um PDF errado.
        log(f"{conta['apelido']}: sessao de outra UC ainda de pe; pulando")
        return "sessao_presa"

    arquivo = baixar(page, ctx, conta, persona)
    H.entre_telas(page, persona, mediana=2.0)
    login.sair(page)
    persona.gravar_meta(logins=persona.meta().get("logins", 0) + 1,
                        ultimo_login=time.time())
    if arquivo:
        return concluir(ag, conta, persona, arquivo)
    # Logou mas nao baixou: nao e bloqueio, e a consulta que nao rendeu.
    if not MES_ALVO:
        ag.registrar_falha(conta["uc"], "sem_fatura")
    return "sem_fatura"


def coletar_doc(contas, ag, forcar=False):
    """Roda todas as UCs de UM titular numa unica subida do navegador.

    UCs do mesmo documento ja dividiam persona e perfil de Chrome
    (personas.persona_de). O que se repetia a toa era o resto do ciclo -
    rotacao de IP, boot do Chrome, maturacao e derrubada - cerca de um minuto
    por UC que nao comprava disfarce nenhum: para o site sao dois logins do
    mesmo titular no mesmo dispositivo, que e exatamente o que acontece com
    quem tem dois relogios no nome.

    Devolve {uc: resultado}.
    """
    persona = P.persona_de(contas[0]["doc"], contas[0]["apelido"])
    resultados = {}
    etiqueta = "/".join(c["apelido"] for c in contas)
    log(f"=== {etiqueta} | {len(contas)} UC(s) | perfil {persona.dir.name} ===")
    rotacionar_ip()

    pid, porta = nav.subir(persona)
    pw = browser = None
    try:
        pw, browser, ctx, page = conectar(porta)
        dialogos = login.escutar_dialogos(page)

        # Perfil que nunca navegou nada nao deve estrear na pagina de login: o
        # score do reCAPTCHA v3 vem sobretudo do historico do navegador.
        if not persona.meta().get("maturado_em"):
            nav.maturar(ctx, persona)
            if not forcar:
                # No fluxo normal o login espera o perfil envelhecer algumas
                # horas: idade de dispositivo e um dos sinais mais baratos que
                # o Imperva e o DRS tem, e um perfil nascido ha 30 segundos
                # comeca no pior degrau.
                log(f"{etiqueta}: perfil maturado; login fica para depois")
                return {c["uc"]: "maturado" for c in contas}
            log(f"{etiqueta}: perfil maturado agora e usado ja (--agora)")

        if not (persona.madura or forcar):
            log(f"{etiqueta}: perfil com {persona.idade_h:.1f}h, ainda verde")
            return {c["uc"]: "imaturo" for c in contas}

        sozinha = len(contas) == 1
        for i, conta in enumerate(contas):
            if not sozinha and not _garantir_deslogado(page, persona):
                log(f"{conta['apelido']}: nao consegui encerrar a sessao anterior; "
                    "o resto do titular fica para a proxima")
                break
            try:
                r = _uma_uc(page, ctx, conta, persona, ag, dialogos, sozinha)
            except Exception as e:
                log(f"{conta['apelido']}: erro inesperado "
                    f"({type(e).__name__}: {str(e)[:90]})")
                if not MES_ALVO:
                    ag.registrar_falha(conta["uc"], "desconhecido")
                r = "erro"
            resultados[conta["uc"]] = r
            if r == "imperva":
                # Quem chama aborta o lote; aqui basta nao gastar as irmas.
                break
            if i < len(contas) - 1:
                H.entre_telas(page, persona, mediana=4.0)
        return resultados
    except Exception as e:
        log(f"{etiqueta}: erro inesperado ({type(e).__name__}: {str(e)[:90]})")
        for c in contas:
            if c["uc"] not in resultados:
                if not MES_ALVO:
                    ag.registrar_falha(c["uc"], "desconhecido")
                resultados[c["uc"]] = "erro"
        return resultados
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass
        nav.derrubar(persona, porta, pid)


def coletar_conta(conta, ag, forcar=False):
    """Uma conta so, pelo mesmo caminho do titular inteiro."""
    return coletar_doc([conta], ag, forcar).get(conta["uc"], "erro")


def por_titular(contas):
    """Agrupa as contas por documento, preservando a ordem de chegada."""
    grupos = {}
    for c in contas:
        grupos.setdefault(P.so_digitos(c["doc"]), []).append(c)
    return list(grupos.values())


def concluir(ag, conta, persona, arquivo):
    """Anota o sucesso na agenda e devolve "ok"."""
    if MES_ALVO:
        # Fatura de um mes escolhido a mao nao fecha o ciclo: marcar
        # ciclo_coletado aqui faria a agenda pular a coleta do mes corrente,
        # que e o que ela existe para cobrar.
        log(f"{conta['apelido']}: mes avulso; agenda nao foi tocada")
    else:
        ag.registrar_sucesso(conta["uc"], persona, arquivo)
    return "ok"


def nomear(caminho, conta):
    """Renomeia o PDF para incluir a UC. Devolve o caminho final."""
    destino = caminho.with_name(
        f"fatura-{conta['uc']}-{caminho.name.replace('fatura-', '')}")
    if destino != caminho:
        if destino.exists():
            destino.unlink()
        caminho.rename(destino)
    log(f"{conta['apelido']}: PRONTO -> {destino.name} ({destino.stat().st_size} bytes)")
    return destino


def segunda_via(page, ctx, conta, persona=None):
    """Tenta a tela de 2a via (fatura pendente). Devolve (caminho|None, motivo).

    Nunca levanta: qualquer surpresa aqui vira motivo "erro" e o fluxo segue
    para o historico, que e o caminho que ja estava provado.
    """
    mes, ano = MES_ALVO if MES_ALVO else (None, None)
    try:
        caminho, motivo = sv.tentar(page, ctx, conta["uc"], persona, mes=mes, ano=ano)
    except Exception as e:
        log(f"{conta['apelido']}: segunda via falhou "
            f"({type(e).__name__}: {str(e)[:80]})")
        return None, "erro"
    if caminho:
        return nomear(caminho, conta), "ok"
    log(f"{conta['apelido']}: segunda via nao resolveu ({motivo})")
    return None, motivo


def baixar(page, ctx, conta, persona=None):
    """Traz a fatura. Devolve o caminho.

    Primeiro a tela de 2a via, que emite direto a fatura pendente da UC; o
    historico ano a ano so entra quando ela nao resolve (fatura ja paga, mes
    pedido a mao, ou a tela fora do ar).

    Sem MES_ALVO, baixa a mais recente - o que a agenda quer no ciclo do mes.
    """
    arquivo, _ = segunda_via(page, ctx, conta, persona)
    if arquivo:
        return arquivo

    mes, ano_pedido = MES_ALVO if MES_ALVO else (None, None)
    bf.FATURAS.mkdir(exist_ok=True)
    login.ir_para(page, login.URL_FATURAS)
    H.entre_telas(page, persona, mediana=3.0)
    try:
        ano = bf.consultar(page, persona, ano_alvo=ano_pedido)
    except PWTimeout as e:
        log(f"{conta['apelido']}: timeout na consulta ({str(e)[:60]})")
        return None
    if not ano:
        log(f"{conta['apelido']}: nenhuma fatura encontrada")
        return None

    caminho = bf.baixar_mais_recente(page, ctx, ano, persona, mes=mes)
    if not (caminho and caminho.exists() and caminho.stat().st_size > 1000):
        log(f"{conta['apelido']}: consulta OK mas o download nao veio")
        return None

    return nomear(caminho, conta)


# Pausa entre um perfil e o seguinte na maturacao. Curta de proposito, e por um
# motivo que nao vale para o login: a maturacao nao toca a Equatorial - o passeio
# e por Google, portais de noticia e Wikipedia. Nao ha ali reputacao da agencia
# virtual a preservar, so o historico do proprio perfil sendo construido. O que
# ainda justifica nao ser zero e o Google: dez perfis novos batendo na busca em
# sequencia imediata, do mesmo IP, e o padrao de velocity mais barato de detectar
# que existe - e o score do reCAPTCHA v3 e justamente o que a maturacao existe
# para levantar.
MATURACAO_MIN = max(0, int(os.environ.get("EQUATORIAL_MATURACAO_MIN", "30")))
MATURACAO_MAX = max(MATURACAO_MIN, int(os.environ.get("EQUATORIAL_MATURACAO_MAX", "90")))


def maturar_persona(persona):
    """Sobe o perfil, da o passeio de maturacao e derruba. Nao loga em nada."""
    rotacionar_ip()
    pid, porta = nav.subir(persona)
    pw = browser = None
    try:
        pw, browser, ctx, _ = conectar(porta)
        nav.maturar(ctx, persona)
    except Exception as e:
        log(f"{persona.apelido}: maturacao falhou ({str(e)[:70]})")
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass
        nav.derrubar(persona, porta, pid)


def maturar_pendentes(contas, limite=None):
    """Prepara perfis que ainda nao navegaram nada. Nao loga em nada."""
    novos = [c for c in contas
             if not P.persona_de(c["doc"], c["apelido"]).meta().get("maturado_em")]
    if not novos:
        log("Nenhum perfil novo para maturar")
        return 0
    random.shuffle(novos)
    novos = novos[:limite] if limite else novos
    log(f"Maturando {len(novos)} perfil(is)")
    for i, c in enumerate(novos):
        maturar_persona(P.persona_de(c["doc"], c["apelido"]))
        # Espaco entre perfis, mas nao depois do ultimo: quem chamou ja tem a
        # propria pausa, e dormir aqui no fim so atrasa o retorno.
        if i < len(novos) - 1:
            time.sleep(random.randint(MATURACAO_MIN, MATURACAO_MAX))
    return len(novos)


# Espacamento do lote forcado. Zero por padrao: uma conta emenda na outra. O que
# separa as sessoes deixou de ser o relogio e passou a ser a troca de IP - cada
# UC sobe num perfil de Chrome proprio e atras de um IP novo (com
# EQUATORIAL_ROTACIONAR=1), entao a velocity que a espera existia para diluir
# nao se acumula mais no mesmo par (IP, dispositivo). Sem rotacao ligada, subir
# EQUATORIAL_TESTE_MIN/MAX de volta e o que reduz o risco.
TESTE_MIN = int(os.environ.get("EQUATORIAL_TESTE_MIN", "0"))
TESTE_MAX = max(TESTE_MIN, int(os.environ.get("EQUATORIAL_TESTE_MAX", "0")))


def lote_forcado(contas, ag):
    """Roda todas as contas pendentes agora, ignorando a agenda.

    Serve ao teste de ponta a ponta: login, consulta, download e logout so podem
    ser exercitados contra o site de verdade. O que NAO se abre mao aqui e a
    rotacao de IP entre titulares: e ela, e nao a espera, que separa uma sessao
    da seguinte agora que o intervalo padrao e zero.

    Duas coisas evitam trabalho repetido: as UCs de um mesmo titular rodam na
    mesma subida do navegador (coletar_doc), e as que ja tem o PDF do mes em
    faturas/ nem entram na fila (ja_baixada) - e o que permite retomar um lote
    interrompido de onde ele parou.

    Aborta o restante se aparecer 'imperva': esse motivo significa que o par
    (IP, dispositivo) foi marcado, e seguir queimaria as personas seguintes por
    dias - o preco mais caro que este projeto pode pagar.
    """
    # Com --mes quem decide o que falta e o disco, nao a agenda. `pendentes()`
    # exclui quem ja foi "coletada no ciclo", e isso e verdade para uma UC que
    # entregou a fatura de 06 ou 07/2026 e segue sem a de 08: ela ficava fora da
    # fila para sempre, e nenhum relance a alcancava. ja_baixada() logo abaixo
    # faz o corte certo - olha o PDF do mes pedido em faturas/.
    pend = list(contas) if MES_ALVO else ag.pendentes(contas)
    resultados = {}
    if MES_ALVO:
        prontas = [c for c in pend if ja_baixada(c)]
        for c in prontas:
            resultados[c["uc"]] = "ja_tinha"
        if prontas:
            log(f"{len(prontas)} conta(s) ja tem o PDF de "
                f"{MES_ALVO[0]:02d}/{MES_ALVO[1]} em faturas/; retomando o resto")
        pend = [c for c in pend if c["uc"] not in resultados]
    if not pend:
        log("Nada pendente no ciclo")
        return 0
    grupos = por_titular(pend)
    random.shuffle(grupos)  # ordem fixa e, ela propria, um padrao
    espaco = (f"{TESTE_MIN}-{TESTE_MAX}s entre elas" if TESTE_MAX
              else "sem intervalo entre elas")
    log(f"Lote forcado: {len(pend)} conta(s) em {len(grupos)} titular(es), "
        f"{espaco}, rotacao de IP {'ligada' if ROTACIONAR else 'DESLIGADA'}")

    abortou = False
    for i, grupo in enumerate(grupos):
        if PARAR and PARAR():
            # Entre titulares e o unico ponto seguro para desistir: no meio de
            # uma sessao ficaria um Chrome de pe e um login pela metade.
            log("Pedido de parada recebido; encerrando entre titulares")
            abortou = True
            break
        resultados.update(coletar_doc(grupo, ag, forcar=True))
        for c in grupo:
            log(f"{c['apelido']}: resultado={resultados.get(c['uc'], 'nao rodou')}")
        if "imperva" in resultados.values():
            log("Imperva marcou este par (IP, dispositivo). Abortando o lote "
                "para nao queimar as personas restantes.")
            abortou = True
            break
        if i < len(grupos) - 1 and TESTE_MAX > 0:
            espera = random.randint(TESTE_MIN, TESTE_MAX)
            log(f"Aguardando {espera}s antes do proximo titular")
            time.sleep(espera)

    log("===== RESUMO DO LOTE =====")
    ok = 0
    for grupo in grupos:
        for c in grupo:
            r = resultados.get(c["uc"], "nao rodou")
            if r == "ok":
                ok += 1
            log(f"  {'OK   ' if r == 'ok' else 'FALHA'} {c['apelido']:<8} "
                f"{c['uc']}  {r}")
    log(f"{ok}/{len(pend)} faturas baixadas")
    return 0 if ok == len(pend) and not abortou else 1


def recarregar(contas):
    """Rebusca a lista de contas. Mantem a anterior se a leitura falhar.

    Com a fila vindo da API, UCs novas entram no meio do ciclo - nao da para ler
    a lista uma vez na largada e nunca mais. Falha aqui nunca derruba o laco: a
    lista em maos ja e suficiente para trabalhar o dia todo.
    """
    try:
        novas = P.carregar_contas()
    except Exception as e:
        log(f"lista de contas nao atualizou ({type(e).__name__}: {str(e)[:80]})")
        return contas
    if not novas:
        return contas
    entraram = {c["uc"] for c in novas} - {c["uc"] for c in contas}
    if entraram:
        log(f"{len(entraram)} UC(s) nova(s) na fila (total {len(novas)})")
    return novas


def uma_rodada(ag, contas):
    """Escolhe e roda no maximo uma conta. Devolve o instante da proxima checagem."""
    pend = ag.pendentes(contas)
    if not pend:
        # Ciclo fechado: so volta no mes que vem.
        log(f"Ciclo {AG.ciclo()} completo: {len(contas)}/{len(contas)} coletadas")
        return time.time() + 6 * 3600

    teto = ag.teto_hoje(len(pend))
    if ag.feitos_hoje() >= teto:
        # Teto batido nao se resolve em 20 minutos: so no dia seguinte. Voltar
        # antes disso so encheria o log com a mesma linha.
        log(f"Teto do dia atingido ({ag.feitos_hoje()}/{teto}); {len(pend)} pendente(s)")
        return ag.amanha_cedo()
    if not ag.em_horario() or ag.em_cooldown():
        return ag.espera_ate()

    fila = ag.fila(contas, P.persona_de)
    if not fila:
        # Ninguem elegivel: ou todos em backoff, ou perfis ainda verdes. Se ha
        # perfil que nunca navegou, este e o melhor uso do tempo ocioso.
        if maturar_pendentes(contas, limite=1):
            return time.time() + random.randint(600, 1800)
        alvo = ag.proximo_evento(contas, P.persona_de)
        log(f"Ninguem elegivel agora ({len(pend)} pendente(s)); "
            f"proximo em {datetime.fromtimestamp(alvo).strftime('%d/%m %H:%M')}")
        return alvo

    conta = fila[0]
    resultado = coletar_conta(conta, ag)
    log(f"{conta['apelido']}: resultado={resultado} "
        f"| dia {ag.feitos_hoje()}/{teto} | pendentes {len(ag.pendentes(contas))}")
    # piso=0: quem acabou de coletar emenda na proxima conta. A separacao entre
    # sessoes e a rotacao de IP feita no inicio de coletar_conta, nao a espera.
    return ag.espera_ate(piso=0)


def extrair_mes(args):
    """Tira --mes/-m (e seu valor) de `args` e devolve (args_limpos, alvo).

    Retirar antes de qualquer outra leitura evita que "--agora --mes 07/2026"
    tome "--mes" como numero de UC.
    """
    limpos, alvo, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a in ("--mes", "-m"):
            if i + 1 >= len(args):
                sys.exit("--mes exige um valor (ex.: --mes 07/2026)")
            alvo = bf.parse_mes(args[i + 1])
            i += 2
            continue
        if a.startswith("--mes="):
            alvo = bf.parse_mes(a.split("=", 1)[1])
            i += 1
            continue
        limpos.append(a)
        i += 1
    return limpos, alvo


def main():
    global MES_ALVO
    try:
        args, da_linha = extrair_mes(sys.argv[1:])
        MES_ALVO = da_linha or bf.mes_do_ambiente()
    except ValueError as e:
        sys.exit(str(e))
    if MES_ALVO and "--agora" not in args:
        # Pedir mes no laco continuo nao faz sentido: a agenda cobra a fatura do
        # ciclo corrente, mes a mes. Na linha de comando isso e engano do
        # usuario e para tudo; vindo do ambiente (um EQUATORIAL_MES esquecido no
        # credenciais.env) so avisamos - derrubar o laco por isso seria pior.
        if da_linha:
            sys.exit("--mes so vale com --agora <UC|todas>: no laco normal quem "
                     "decide o mes e a agenda.")
        log(f"EQUATORIAL_MES={MES_ALVO[0]:02d}/{MES_ALVO[1]} ignorado fora do --agora")
        MES_ALVO = None
    if MES_ALVO:
        log(f"Mes pedido: {MES_ALVO[0]:02d}/{MES_ALVO[1]}")
    contas = P.carregar_contas()
    ag = AG.Agenda()
    P.PERFIS.mkdir(exist_ok=True)
    bf.FATURAS.mkdir(exist_ok=True)

    if "--status" in args:
        AG.relatorio()
        return 0
    if "--maturar" in args:
        maturar_pendentes(contas)
        return 0

    # A rota so vai para o celular com EQUATORIAL_ROTACIONAR=1, e o finally
    # devolve a maquina ao estado anterior mesmo com Ctrl+C.
    estado_rede = login.preparar_rede()
    try:
        if "--agora" in args:
            bruto = args[args.index("--agora") + 1]
            if bruto.lower() in ("todas", "todos", "tudo", "all"):
                return lote_forcado(contas, ag)
            alvo = P.so_digitos(bruto)
            conta = next((c for c in contas if c["uc"] == alvo), None)
            if not conta:
                # Pode ser uma UC que a API ainda nao tinha servido quando a
                # lista foi lida. Uma busca extra e barata; desistir sem tentar,
                # nao.
                contas = recarregar(contas)
                conta = next((c for c in contas if c["uc"] == alvo), None)
            if not conta:
                sys.exit(f"UC {alvo} nao esta na fila ({len(contas)} conta(s) conhecida(s))")
            log(f"Resultado: {coletar_conta(conta, ag, forcar=True)}")
            return 0
        if "--uma" in args:
            uma_rodada(ag, contas)
            return 0
        log(f"Laco iniciado: {len(contas)} conta(s), janela "
            f"{AG.HORA_INICIO}h-{AG.HORA_FIM}h, rotacao de IP "
            f"{'ligada' if ROTACIONAR else 'desligada'}")
        while True:
            contas = recarregar(contas)
            proximo = uma_rodada(ag, contas)
            dormir_ate(proximo)
    except KeyboardInterrupt:
        log("Interrompido")
        return 130
    finally:
        login.liberar_rede(estado_rede)


if __name__ == "__main__":
    sys.exit(main())
