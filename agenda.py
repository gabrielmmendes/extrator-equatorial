#!/usr/bin/env python3
"""
Agenda e orcamento de tentativas.

A premissa antiga do projeto era que as recusas eram independentes, entao valia
metralhar: ~50 tentativas em 51 minutos. O teste manual derrubou isso - um login
deu certo e a tentativa seguinte, no mesmo navegador e mesmo IP, ja foi recusada;
a mesma UC entrou de primeira no celular. Ou seja, as tentativas NAO sao
independentes: o sucesso queima o par (dispositivo, IP) por um tempo.

Com isso a estrategia se inverte. O recurso escasso deixou de ser tempo e passou
a ser reputacao, e nos temos tempo de sobra: cada UC precisa de UMA fatura por
mes, ou seja, ~30 dias de janela para 1 sucesso. Espalhar N UCs por 30 dias custa
nada e derruba a taxa de requisicao por IP para algo que nenhuma regra de
velocity pega.

Numeros que saem disso:
    60 UCs  -> 2 logins/dia
   300 UCs  -> 10 logins/dia
O intervalo entre eles e zero por padrao: o que separa uma sessao da outra e a
troca de IP (EQUATORIAL_ROTACIONAR=1) mais o perfil de Chrome proprio de cada
persona. EQUATORIAL_INTERVALO_MIN/MAX e EQUATORIAL_COOLDOWN_MIN/MAX (segundos)
trazem a espera de volta quando nao ha rotacao.
"""

import contextlib
import json
import msvcrt
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import personas as P

RAIZ = Path(__file__).parent
ARQUIVO = RAIZ / "estado" / "agenda.json"
# Trava de escrita entre processos, usada pelo coletar_paralelo.py: N workers
# anotam sucesso e falha no MESMO arquivo. Fica a parte da agenda de proposito -
# a gravacao termina em os.replace, entao o arquivo antigo (e qualquer trava
# dentro dele) deixa de existir no meio da operacao.
TRAVA = RAIZ / "estado" / "agenda.lock"

# Janela de horario em que um cliente de verdade acessa a agencia virtual.
# Login as 04h da manha e, sozinho, um sinal.
HORA_INICIO = int(os.environ.get("EQUATORIAL_HORA_INICIO", "8"))
HORA_FIM = int(os.environ.get("EQUATORIAL_HORA_FIM", "22"))

TETO_UC_DIA = int(os.environ.get("EQUATORIAL_TETO_UC_DIA", "1"))
TETO_DIA_MAX = int(os.environ.get("EQUATORIAL_TETO_DIA", "10"))
# Intervalo entre uma coleta e a seguinte. Zero por padrao: quem separa as
# sessoes agora e a troca de IP + o perfil de Chrome proprio de cada persona,
# nao o relogio. Sem EQUATORIAL_ROTACIONAR=1 vale subir estes de volta para
# 900/2400, senao N logins seguidos saem todos do mesmo IP.
INTERVALO_MIN = int(os.environ.get("EQUATORIAL_INTERVALO_MIN", "0"))
INTERVALO_MAX = max(INTERVALO_MIN, int(os.environ.get("EQUATORIAL_INTERVALO_MAX", "0")))

# Backoff por motivo, em horas (faixa sorteada). Motivo importa: timeout de rede
# nao e bloqueio e nao merece castigo; challenge do Imperva significa que o par
# (perfil, IP) foi marcado e insistir so aprofunda a marca.
BACKOFF_H = {
    "backend":  [(3, 7), (10, 22), (26, 50)],
    "captcha":  [(12, 30), (24, 48), (40, 72)],
    "imperva":  [(30, 72), (48, 96), (72, 144)],
    "rede":     [(0.3, 1.0), (0.5, 2.0), (1.0, 4.0)],
    "desconhecido": [(6, 14), (16, 30), (30, 60)],
}
# Depois de um sucesso o par (dispositivo, IP) fica quente: foi exatamente ai que
# o bloqueio apareceu no teste manual. Com rotacao de IP entre contas o par muda
# inteiro na proxima coleta (IP novo + perfil novo), entao o descanso default e
# zero; EQUATORIAL_COOLDOWN_MIN/MAX (segundos) trazem a espera de volta quando o
# robo roda sem rotacao.
COOLDOWN_MIN = int(os.environ.get("EQUATORIAL_COOLDOWN_MIN", "0"))
COOLDOWN_MAX = max(COOLDOWN_MIN, int(os.environ.get("EQUATORIAL_COOLDOWN_MAX", "0")))
COOLDOWN_SUCESSO = (COOLDOWN_MIN, COOLDOWN_MAX)


def agora():
    return time.time()


def ciclo(ts=None):
    return datetime.fromtimestamp(ts or agora()).strftime("%Y-%m")


def dia(ts=None):
    return datetime.fromtimestamp(ts or agora()).strftime("%Y-%m-%d")


def humano(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "-"


@contextlib.contextmanager
def _travada(teto_s=30):
    """Exclusao mutua entre processos enquanto o bloco roda.

    msvcrt.locking trava uma faixa de bytes e e a unica trava entre processos
    que o Windows da sem dependencia externa. LK_LOCK ja retenta por ~10s por
    conta propria; o laco aqui estica isso ate `teto_s`.

    Nao conseguir a trava nao aborta: segue sem ela, avisando alto. Escrita
    corrida e o comportamento de hoje - perder o registro de um titular por
    causa de uma trava emperrada seria trocar um problema raro por um pior.
    """
    TRAVA.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TRAVA, os.O_RDWR | os.O_CREAT)
    preso = False
    fim = time.time() + teto_s
    while time.time() < fim:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            preso = True
            break
        except OSError:
            continue
    if not preso:
        print(f"[{time.strftime('%H:%M:%S')}] agenda: AVISO: trava presa ha "
              f"{teto_s}s; gravando sem ela", flush=True)
    try:
        yield
    finally:
        if preso:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


class Agenda:
    def __init__(self, caminho=ARQUIVO):
        self.caminho = Path(caminho)
        self.dados = self._ler()

    def _ler(self):
        """Le o arquivo do disco, completando o que faltar.

        JSON quebrado nao pode virar agenda vazia em silencio: o `except: pass`
        que estava aqui gravava por cima um mes inteiro de estado na gravacao
        seguinte. Agora o arquivo suspeito e posto de lado com nome proprio e o
        aviso sai no log.
        """
        dados = {"ucs": {}, "global": {"por_dia": {}, "cooldown_ate": 0,
                                       "ultima_execucao": 0}}
        if self.caminho.exists():
            try:
                dados.update(json.loads(self.caminho.read_text(encoding="utf-8")))
            except Exception as e:
                suspeito = self.caminho.with_suffix(
                    f".json.corrompida-{int(time.time())}")
                try:
                    self.caminho.rename(suspeito)
                except OSError:
                    pass
                print(f"[{time.strftime('%H:%M:%S')}] agenda: ARQUIVO ILEGIVEL "
                      f"({str(e)[:60]}); guardado como {suspeito.name} e "
                      "recomecando do zero", flush=True)
        dados.setdefault("ucs", {})
        g = dados.setdefault("global", {})
        g.setdefault("por_dia", {})
        g.setdefault("cooldown_ate", 0)
        g.setdefault("ultima_execucao", 0)
        return dados

    def _transacao(self, mutacao):
        """Reaplica `mutacao` sobre a versao MAIS RECENTE do arquivo, sob trava.

        Reler antes de mutar e o ponto: cada worker carregou a agenda na largada
        e gravar essa copia apagaria tudo que os outros anotaram desde entao.
        Como a fila do paralelo e fatiada por titular, cada processo so toca UCs
        que sao so dele - reler e reaplicar a mudanca desta chamada basta, nao
        ha merge a fazer.
        """
        with _travada():
            self.dados = self._ler()
            mutacao()
            self._gravar()

    def _gravar(self):
        """Grava em arquivo temporario e troca. Chamado ja sob trava.

        os.replace e atomico no Windows: um Ctrl+C no meio deixa a agenda antiga
        inteira, em vez do JSON pela metade que o write_text direto produzia.
        """
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        # Poda o historico diario: so os ultimos 60 dias interessam.
        pd = self.dados["global"]["por_dia"]
        for d in sorted(pd)[:-60]:
            pd.pop(d, None)
        tmp = self.caminho.with_suffix(f".json.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(self.dados, indent=2), encoding="utf-8")
        os.replace(tmp, self.caminho)

    def salvar(self):
        """Grava o que esta em memoria, sob trava. Nao rele o disco.

        Quem muda estado de UC passa pelo _transacao, que rele antes de mutar.
        Este aqui fica para gravacao avulsa, e nao pode reler: reler descartaria
        justamente a mudanca que quem chamou acabou de fazer em memoria.
        """
        with _travada():
            self._gravar()

    # ------------------------------------------------------------- por conta

    def uc(self, codigo):
        return self.dados["ucs"].setdefault(codigo, {
            "ciclo_coletado": None, "ultimo_sucesso": 0, "ultima_tentativa": 0,
            "proxima_elegivel": 0, "falhas_seguidas": 0, "ultimo_motivo": None,
            "tentativas_dia": {}, "arquivo": None,
        })

    def coletada_no_ciclo(self, codigo):
        return self.uc(codigo)["ciclo_coletado"] == ciclo()

    def tentativas_hoje(self, codigo):
        return self.uc(codigo)["tentativas_dia"].get(dia(), 0)

    def registrar_sucesso(self, codigo, persona, arquivo=None):
        def mutar():
            e = self.uc(codigo)
            e["ciclo_coletado"] = ciclo()
            e["ultimo_sucesso"] = agora()
            e["ultima_tentativa"] = agora()
            e["falhas_seguidas"] = 0
            e["ultimo_motivo"] = "ok"
            e["arquivo"] = str(arquivo) if arquivo else None
            e["proxima_elegivel"] = self._proximo_ciclo(persona)
            self._contar(codigo)
            g = self.dados["global"]
            g["cooldown_ate"] = agora() + random.uniform(*COOLDOWN_SUCESSO)

        self._transacao(mutar)

    def registrar_falha(self, codigo, motivo):
        def mutar():
            e = self.uc(codigo)
            e["falhas_seguidas"] = min(e["falhas_seguidas"] + 1, 9)
            e["ultima_tentativa"] = agora()
            e["ultimo_motivo"] = motivo
            faixas = BACKOFF_H.get(motivo, BACKOFF_H["desconhecido"])
            lo, hi = faixas[min(e["falhas_seguidas"] - 1, len(faixas) - 1)]
            e["proxima_elegivel"] = agora() + random.uniform(lo, hi) * 3600
            self._contar(codigo)

        self._transacao(mutar)

    def _contar(self, codigo):
        e = self.uc(codigo)
        hoje = dia()
        e["tentativas_dia"][hoje] = e["tentativas_dia"].get(hoje, 0) + 1
        for d in sorted(e["tentativas_dia"])[:-10]:
            e["tentativas_dia"].pop(d, None)
        g = self.dados["global"]
        g["por_dia"][hoje] = g["por_dia"].get(hoje, 0) + 1
        g["ultima_execucao"] = agora()

    def _proximo_ciclo(self, persona):
        """Proximo mes, no dia-ancora da persona, perto do horario de pico dela.

        Ancorar o retorno da mesma persona ao mesmo dia e horario e o padrao de
        quem tem rotina - que e o que a maioria dos clientes tem com a conta de
        luz.
        """
        hoje = datetime.fromtimestamp(agora())
        alvo = (hoje.replace(day=1) + timedelta(days=32)).replace(
            day=min(persona.dia_ancora, 28),
            hour=persona.hora_pico, minute=random.randint(0, 59),
            second=random.randint(0, 59), microsecond=0)
        return alvo.timestamp()

    # -------------------------------------------------------------- global

    def teto_hoje(self, pendentes):
        """Quantos logins cabem hoje.

        Deriva da propria demanda: o que falta coletar, dividido pelos dias que
        sobram no mes, com folga de 40% para absorver recusas. Sem isso a gente
        ou corre demais (bloqueio) ou de menos (nao fecha o mes).
        """
        hoje = datetime.fromtimestamp(agora())
        prox = (hoje.replace(day=1) + timedelta(days=32)).replace(day=1)
        dias_restantes = max(1, (prox.date() - hoje.date()).days)
        alvo = pendentes / dias_restantes * 1.4
        return max(1, min(TETO_DIA_MAX, int(alvo + 0.999)))

    def feitos_hoje(self):
        return self.dados["global"]["por_dia"].get(dia(), 0)

    def em_cooldown(self):
        return agora() < self.dados["global"].get("cooldown_ate", 0)

    def em_horario(self, ts=None):
        return HORA_INICIO <= datetime.fromtimestamp(ts or agora()).hour < HORA_FIM

    # ------------------------------------------------------------- decisao

    def fila(self, contas, personas):
        """Contas elegiveis agora, em ordem de prioridade.

        Prioridade: quem esta mais perto do proprio dia-ancora primeiro, depois
        quem esta ha mais tempo sem sucesso - assim a persona tende a aparecer
        sempre na mesma epoca do mes.
        """
        hoje = datetime.fromtimestamp(agora()).day
        elegiveis = []
        for c in contas:
            e = self.uc(c["uc"])
            p = personas(c["doc"], c["apelido"])
            if self.coletada_no_ciclo(c["uc"]):
                continue
            if self.tentativas_hoje(c["uc"]) >= TETO_UC_DIA:
                continue
            if agora() < e["proxima_elegivel"]:
                continue
            if not p.madura:
                continue
            dist = min(abs(hoje - p.dia_ancora), 31 - abs(hoje - p.dia_ancora))
            elegiveis.append((dist, -(agora() - (e["ultimo_sucesso"] or 0)), c))
        elegiveis.sort(key=lambda t: (t[0], t[1]))
        return [c for _, _, c in elegiveis]

    def pendentes(self, contas):
        return [c for c in contas if not self.coletada_no_ciclo(c["uc"])]

    def na_janela(self, ts):
        """Empurra um instante para dentro da janela de horario humano."""
        d = datetime.fromtimestamp(ts)
        if d.hour < HORA_INICIO:
            d = d.replace(hour=HORA_INICIO, minute=random.randint(2, 55), second=0)
        elif d.hour >= HORA_FIM:
            d = (d + timedelta(days=1)).replace(
                hour=HORA_INICIO, minute=random.randint(2, 55), second=0)
        return d.timestamp()

    def espera_ate(self, piso=300):
        """Instante em que faz sentido tentar de novo (cooldown/horario).

        O piso existe porque `ultima_execucao` e 0 num estado novo: sem ele o
        max() cai no proprio agora(), o laco nao dorme nada e fica girando
        apertado sobre a fila vazia. Quem acabou de coletar passa piso=0: ali o
        laco ja gastou minutos de verdade numa sessao, nao ha giro apertado a
        evitar, e a proxima conta deve emendar.
        """
        alvo = max(agora() + piso,
                   self.dados["global"].get("cooldown_ate", 0),
                   self.dados["global"].get("ultima_execucao", 0)
                   + random.uniform(INTERVALO_MIN, INTERVALO_MAX))
        return self.na_janela(alvo)

    def amanha_cedo(self):
        """Comeco da janela do proximo dia, com minuto sorteado."""
        d = (datetime.fromtimestamp(agora()) + timedelta(days=1)).replace(
            hour=HORA_INICIO, minute=random.randint(2, 55), second=0, microsecond=0)
        return d.timestamp()

    def proximo_evento(self, contas, personas):
        """Quando a proxima conta pendente se torna elegivel.

        Melhor que reconsultar a fila a cada poucos minutos: aqui sabemos a hora
        exata em que cada backoff vence e em que cada perfil verde amadurece,
        entao o laco dorme ate la em vez de acordar so para descobrir que nao ha
        nada a fazer.
        """
        candidatos = []
        for c in contas:
            if self.coletada_no_ciclo(c["uc"]):
                continue
            p = personas(c["doc"], c["apelido"])
            if not p.meta().get("maturado_em"):
                continue  # maturacao tem caminho proprio, nao espera relogio
            t = max(self.uc(c["uc"])["proxima_elegivel"], agora())
            if not p.madura:
                falta = float(p.meta().get("maturacao_h", P.MATURACAO_H_MAX)) - p.idade_h
                t = max(t, agora() + falta * 3600)
            candidatos.append(t)
        return self.na_janela(min(candidatos) if candidatos else agora() + 3600)


def relatorio(caminho=ARQUIVO):
    ag = Agenda(caminho)
    # Relatorio le a fila que ja esta em disco: consultar a API aqui so mudaria
    # o numero que o usuario esta conferindo enquanto ele confere.
    contas = P.carregar_contas(atualizar=False)
    pend = ag.pendentes(contas)
    print(f"Ciclo {ciclo()} | {len(contas) - len(pend)}/{len(contas)} coletadas "
          f"| hoje {ag.feitos_hoje()}/{ag.teto_hoje(len(pend))} "
          f"| cooldown {'SIM' if ag.em_cooldown() else 'nao'}")
    for c in contas:
        e = ag.uc(c["uc"])
        p = P.persona_de(c["doc"], c["apelido"])
        marca = "OK " if ag.coletada_no_ciclo(c["uc"]) else "   "

        # Perfil verde nao entra na fila, entao dizer so "imaturo" esconde a
        # informacao que interessa: a partir de quando ele entra.
        if not p.meta().get("maturado_em"):
            perfil = " [perfil nao maturado - rode --maturar]"
        elif not p.madura:
            falta = float(p.meta().get("maturacao_h", P.MATURACAO_H_MAX)) - p.idade_h
            pronto = agora() + falta * 3600
            perfil = f" [perfil verde, pronto {humano(pronto)}]"
        else:
            perfil = ""
        print(f" {marca}{c['apelido']:<8} {c['uc']}  "
              f"motivo={e['ultimo_motivo'] or '-':<12} "
              f"falhas={e['falhas_seguidas']} "
              f"elegivel={humano(e['proxima_elegivel'])}{perfil}")


if __name__ == "__main__":
    relatorio()
