#!/usr/bin/env python3
"""
Worker de faturas do gcenergy: reservar -> buscar -> subir no Drive -> reportar.

Contrato (guia de integracao do gcenergy): o worker pede jobs por HTTP
(gcenergy.py), cada job e UMA UC e diz o que buscar - uma lista de meses
(MESES), a fatura mais recente (MAIS_RECENTE) ou a mais antiga (MAIS_ANTIGA).
Os PDFs vao para o Google Drive e o CONCLUIDO leva so os IDs; o gcenergy importa
em segundo plano. Todo job reservado termina num PATCH, e o servidor devolve a
fila o que ficar EM_EXECUCAO por mais de 60 minutos.

Onde isso encontra a estrategia do robo
---------------------------------------
O resto do projeto existe para nao queimar reputacao no portal (ver README):
uma sessao por UC por dia, backoff de horas/dias por motivo, perfil de Chrome
que envelhece antes do primeiro login, janela de horario humano. O job chega com
prazo de 60 min e numa ordem que nao escolhemos, entao o ritmo passa a ser
controlado em dois pontos:

  * ANTES de reservar: janela de horario e intervalo/cooldown entre contas da
    agenda. Fora deles o worker simplesmente nao reserva - o job espera
    PENDENTE no servidor, sem custo e sem relogio correndo. Nao ha teto diario
    global de sessoes (EQUATORIAL_TETO_DIA so vale no orquestrador).
  * DEPOIS de reservar, por UC: se ela esta em backoff (ultima sessao terminou em
    bloqueio), ou o dispositivo do titular esta parado depois de um bloqueio, o
    job termina em ERRO TENTATIVA_ADIADA e o motivo diz ate quando. Devolver
    PENDENTE nao serve: a fila e servida por ordem, o mesmo job voltaria na
    reserva seguinte e travaria todos os de tras. Com ERRO o cron diario recria
    o job no dia seguinte - que e exatamente o ritmo de uma tentativa por UC
    por dia.

Pool de personas
----------------
Nao ha um perfil de Chrome por titular: ha um pool fixo de N (perfis/pool-NN,
GCENERGY_PERSONAS) e cada titular cai sempre no mesmo (personas.persona_para).
O pool e maturado uma vez, na primeira largada - enquanto houver persona verde
o worker matura e nao reserva. Cada persona descansa entre sessoes
(GCENERGY_PERSONA_DESCANSO_MIN) e fica parada por horas depois de um bloqueio,
porque e o dispositivo que fica marcado; o IP ja troca a cada sessao.

Antes de tudo isso vem o disco: mes que ja esta em faturas/ (baixado por
qualquer fluxo do robo) e mandado sem abrir o portal. Job inteiro em disco e
concluido sem login nenhum.

Uso:
    python worker.py            # laco continuo
    python worker.py --uma      # no maximo uma reserva, e sai
    python worker.py --checar   # confere chave da API e credencial do Drive
    python worker.py --job 42   # mostra um job (GET), sem mexer nele

Ajustes (credenciais.env), alem dos do gcenergy.py e da agenda:
    GCENERGY_PRAZO_MIN     minutos de busca por job antes de concluir com o que
                           tiver (default 45; o servidor corta em 60)
    GCENERGY_ESPERA_VAZIA  segundos entre consultas com a fila vazia (default 60)
    GCENERGY_CACHE_LOCAL   0 desliga o reaproveitamento de faturas/ (default 1)
    GCENERGY_PERSONAS      tamanho do pool de personas (default 30)
    GCENERGY_PERSONA_DESCANSO_MIN  minutos entre duas sessoes da mesma persona
                           (default 15)
    GOOGLE_DRIVE_CREDENCIAIS / GOOGLE_DRIVE_PASTA_ID   como no enviar_drive.py

Varios celulares: worker_paralelo.py roda um laco() destes por aparelho.
"""

# credenciais.env antes de qualquer outro import do projeto: agenda, login,
# personas e gcenergy leem o ambiente no momento do import.
from enviar_drive import carregar_env

carregar_env()

import json  # noqa: E402
import msvcrt  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402

import agenda as AG  # noqa: E402
import api  # noqa: E402
import baixar_fatura as bf  # noqa: E402
import enviar_drive as ED  # noqa: E402
import gcenergy as GC  # noqa: E402
import humano as H  # noqa: E402
import login  # noqa: E402
import navegador as nav  # noqa: E402
import orquestrador as orq  # noqa: E402
import personas as P  # noqa: E402
import segunda_via as sv  # noqa: E402

RAIZ = Path(__file__).parent
# Job em maos, um arquivo por X-Worker-Id (o paralelo roda varios). Se o
# processo morrer no meio, a proxima largada devolve o job a fila na hora, em
# vez de deixa-lo preso ate o timeout de 60 min do servidor.
JOB_ATUAL = RAIZ / "estado" / (
    "worker_job-" + re.sub(r"[^A-Za-z0-9_.-]", "_", GC.WORKER_ID) + ".json")
# (UC, mes, ano) -> ID no Drive do PDF ja enviado. Job repetido para o mesmo mes
# (reprocessado, ou recriado pelo cron) nao sobe o arquivo de novo.
INDICE_DRIVE = RAIZ / "estado" / "drive_ids.json"
# Uma trava por persona do pool, compartilhada entre os workers da maquina.
TRAVAS = RAIZ / "estado" / "travas"

PRAZO_S = int(float(os.environ.get("GCENERGY_PRAZO_MIN", "45")) * 60)
ESPERA_VAZIA = int(os.environ.get("GCENERGY_ESPERA_VAZIA", "60"))
CACHE_LOCAL = os.environ.get("GCENERGY_CACHE_LOCAL", "1") != "0"
PASTA = os.environ.get("GOOGLE_DRIVE_PASTA_ID", ED.PASTA_PADRAO)
# Intervalo minimo entre duas sessoes da mesma persona do pool.
DESCANSO_S = int(float(os.environ.get("GCENERGY_PERSONA_DESCANSO_MIN", "15")) * 60)
# Folga que o job precisa depois de esperar pela persona: login, busca, upload e
# PATCH tem de caber no que sobra dos 60 min do servidor.
FOLGA_SESSAO_S = 15 * 60

# Motivos de falha que pedem distancia: insistir antes do backoff da agenda
# vencer aprofunda a marca no par (dispositivo, IP). Credencial errada, falta de
# data de nascimento e "nao havia fatura" nao queimam nada - para esses so vale
# o teto de uma sessao por UC por dia.
QUEIMAM = {"imperva", "captcha", "backend", "rede", "desconhecido"}
# Destes, os que marcam o DISPOSITIVO: a persona do pool fica parada pela
# primeira faixa do backoff da agenda (horas a dias). Rede e "desconhecido" nao
# dizem nada sobre o dispositivo.
MARCAM_PERSONA = {"imperva", "captcha", "backend"}

RE_UC_DESCONHECIDA = re.compile(
    r"(unidade consumidora|\bUC\b|instala[cç][aã]o)[^.]{0,60}"
    r"(n[aã]o (localizad|encontrad|cadastrad|exist)|inexistente|inv[aá]lid)", re.I)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] worker: {msg}", flush=True)


# ----------------------------------------------------------------- utilidades

def _ler_json(caminho, padrao):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _gravar_json(caminho, dados):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, caminho)


def competencia(iso):
    """"2026-08-01" -> (8, 2026). ValueError se nao for yyyy-MM."""
    ano, mes = str(iso)[:7].split("-")
    mes, ano = int(mes), int(ano)
    if not 1 <= mes <= 12:
        raise ValueError(iso)
    return mes, ano


def rotulo(comp):
    return f"{comp[0]:02d}/{comp[1]}" if comp else "?"


def _ordem(comp):
    """Chave cronologica de uma competencia (mes, ano). Desconhecida = por ultimo."""
    return (comp[1], comp[0]) if comp else (9999, 99)


def _antes(a, b):
    return bool(a and b) and _ordem(a) < _ordem(b)


def nome_local(uc, comp):
    """Nome em faturas/ - o mesmo padrao que o resto do robo ja grava e le."""
    if not comp:
        return f"fatura-{uc}-sem-competencia.pdf"
    return f"fatura-{uc}-{comp[0]}-{comp[1]}.pdf"


def pdf_local(uc, comp):
    """PDF desta UC e competencia ja em faturas/, ou None.

    Compara por ED.chave, que ignora zero a esquerda na UC e no mes: o arquivo
    pode ter vindo do orquestrador, do paralelo ou de um job anterior.
    """
    if not CACHE_LOCAL:
        return None
    alvo = ED.chave(uc, *comp)
    for caminho in bf.FATURAS.glob(f"fatura-*{uc.lstrip('0')}-*.pdf"):
        m = ED.PADRAO_PDF.search(caminho.name)
        if not m or ED.chave(m.group(1), m.group(2), m.group(3)) != alvo:
            continue
        if caminho.stat().st_size > 1000 and bf._e_pdf(caminho):
            return caminho
    return None


def passou(prazo):
    return time.time() > prazo


class Trava:
    """Exclusao entre workers sobre um perfil de Chrome (persona do pool).

    Cada persona atende um grupo fixo de titulares, e o servidor pode entregar
    dois jobs do mesmo grupo a dois workers ao mesmo tempo. navegador.subir()
    derruba o Chrome que ja estiver no perfil, entao o segundo mataria o login do
    primeiro no meio. Como o titular e fixo na persona, a mesma trava tambem
    impede duas sessoes simultaneas na mesma conta.

    msvcrt.locking, como a trava da agenda: e do processo, entao some sozinha se
    ele morrer - um arquivo-bandeira esquecido travaria a persona para sempre.
    """

    def __init__(self, chave):
        self.caminho = TRAVAS / f"{chave}.lock"
        self.fd = None

    def tentar(self):
        TRAVAS.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.caminho, os.O_RDWR | os.O_CREAT)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def esperar(self, ate):
        """Tenta ate o instante `ate`. True se pegou."""
        while not self.tentar():
            if time.time() >= ate:
                return False
            time.sleep(10)
        return True

    def soltar(self):
        if self.fd is None:
            return
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


def erro(codigo, motivo):
    """Corpo do PATCH de ERRO. `erro` tem teto de 100 caracteres no servidor."""
    return {"status": "ERRO", "erro": codigo[:100], "motivoErro": motivo[:1000]}


def conta_do_job(job):
    """Job do gcenergy -> conta no formato que login/personas ja consomem."""
    uc = P.so_digitos(job.get("numeroUc"))
    return {
        "uc": uc,
        "doc": P.so_digitos(job.get("cpfCnpj")),
        # O site pede DD/MM/AAAA; o job manda yyyy-MM-dd.
        "nasc": api._data_br(job.get("dataNascimento")),
        "apelido": api._apelido({"razao_social": job.get("nome")}, uc),
    }


# ---------------------------------------------------------------------- Drive

class Drive:
    """Upload dos PDFs. O ID devolvido pelo files.create e o que vai no CONCLUIDO."""

    def __init__(self):
        cred = os.environ.get("GOOGLE_DRIVE_CREDENCIAIS", "").strip()
        if not cred:
            raise RuntimeError("GOOGLE_DRIVE_CREDENCIAIS nao definida - aponte "
                               "para o JSON de autorizacao do Google em credenciais.env")
        self.servico = ED.abrir_servico(cred)
        self.pasta = PASTA

    def conta(self):
        sobre = self.servico.about().get(fields="user(emailAddress)").execute()
        return sobre.get("user", {}).get("emailAddress", "?")

    def _vivo(self, fid):
        """O arquivo ainda existe e nao esta na lixeira?"""
        from googleapiclient.errors import HttpError

        try:
            f = self.servico.files().get(fileId=fid, fields="id, trashed",
                                         supportsAllDrives=True).execute()
            return not f.get("trashed")
        except HttpError as e:
            if getattr(e, "resp", None) is not None and e.resp.status == 404:
                return False
            raise

    def subir(self, uc, comp, caminho):
        """Sobe o PDF (ou reaproveita o ja enviado) e devolve o ID no Drive.

        Reaproveita so se o arquivo local for o mesmo (tamanho) e o do Drive
        ainda estiver la: ID de arquivo apagado faria a importacao falhar no
        gcenergy, e o reprocessamento mandaria o mesmo ID morto de novo.
        """
        chave = f"{uc.lstrip('0')}-{comp[0]}-{comp[1]}" if comp else caminho.name
        tamanho = caminho.stat().st_size
        indice = _ler_json(INDICE_DRIVE, {})
        reg = indice.get(chave)
        if reg and reg.get("tamanho") == tamanho and self._vivo(reg["id"]):
            log(f"  {caminho.name}: ja no Drive ({reg['id']})")
            return reg["id"]
        nome = ED.nome_no_drive(uc, *comp) if comp else caminho.name
        fid = ED.subir(self.servico, caminho, nome, self.pasta)["id"]
        # Sob a trava da agenda: os workers do paralelo gravam o mesmo indice, e
        # reler-mudar-gravar sem trava perderia o registro de um deles.
        with AG._travada():
            indice = _ler_json(INDICE_DRIVE, {})
            indice[chave] = {"id": fid, "nome": nome, "tamanho": tamanho,
                             "arquivo": caminho.name, "em": time.time()}
            _gravar_json(INDICE_DRIVE, indice)
        log(f"  {caminho.name}: enviado como {nome} ({fid})")
        return fid


# ------------------------------------------------------------ ritmo e espera

def portao(ag):
    """Instante ate o qual o worker nao deve reservar, ou None se pode agora.

    Reserva so quando daria para logar ja: janela de horario, cooldown e
    intervalo entre contas da agenda. Job nao reservado nao tem relogio correndo
    no servidor. Sem teto diario global: o volume e o que a fila e a janela
    derem.
    """
    alvo = ag.espera_ate(piso=0)
    return alvo if alvo > time.time() + 1 else None


def adiada(ag, conta):
    """Por que NAO abrir sessao para esta UC agora (texto), ou None."""
    uc = conta["uc"]
    feitas = ag.tentativas_hoje(uc)
    if feitas >= AG.TETO_UC_DIA:
        return (f"A UC ja teve {feitas} sessao(oes) no portal hoje (limite "
                f"{AG.TETO_UC_DIA} por dia); o robo tenta de novo a partir de amanha.")
    e = ag.uc(uc)
    if e.get("ultimo_motivo") in QUEIMAM and time.time() < e.get("proxima_elegivel", 0):
        return (f"A ultima sessao ({AG.humano(e.get('ultima_tentativa'))}) terminou em "
                f"'{e['ultimo_motivo']}'; o robo espera ate "
                f"{AG.humano(e['proxima_elegivel'])} para nao agravar o bloqueio.")
    return None


# ------------------------------------------------------------ pool de personas

def _pronta_idade(persona):
    """Instante em que a persona atinge a idade minima para logar."""
    falta_h = float(persona.meta().get("maturacao_h", P.MATURACAO_H_MAX)) - persona.idade_h
    return time.time() + max(0.0, falta_h) * 3600


def pronta_em(persona):
    """Quando a persona pode abrir a proxima sessao (descanso e bloqueio)."""
    m = persona.meta()
    return max(m.get("ultimo_uso", 0) + DESCANSO_S, m.get("descanso_ate", 0))


def estado_pool():
    """(verdes, jovens, quando): o que impede o pool de trabalhar.

    verdes = sem maturacao; jovens = maturadas, ainda sem a idade minima;
    quando = instante em que a ultima jovem fica pronta (None sem jovens).
    """
    verdes, jovens = [], []
    for p in P.pool():
        if not p.meta().get("maturado_em"):
            verdes.append(p)
        elif not p.madura:
            jovens.append(p)
    quando = max((_pronta_idade(p) for p in jovens), default=None)
    return verdes, jovens, quando


def maturar_uma_do_pool(verdes):
    """Matura a primeira persona verde que nenhum outro worker pegou. True se maturou.

    Os workers do paralelo dividem o pool sozinhos: cada um tenta a trava da
    persona e pula a que estiver ocupada.
    """
    for persona in random.sample(verdes, len(verdes)):
        trava = Trava(persona.chave)
        if not trava.tentar():
            continue
        try:
            if persona.meta().get("maturado_em"):
                continue  # outro worker maturou enquanto este olhava
            log(f"pool: maturando {persona.apelido} ({len(verdes)} verde(s))")
            orq.maturar_persona(persona)
        finally:
            trava.soltar()
        return True
    return False


def marcar_uso(persona, s):
    """Grava o fim da sessao na persona: descanso curto, ou longo se foi marcada."""
    campos = {"ultimo_uso": time.time()}
    if s is not None and not s.logou and s.motivo in MARCAM_PERSONA:
        lo, hi = AG.BACKOFF_H[s.motivo][0]
        campos["descanso_ate"] = time.time() + random.uniform(lo, hi) * 3600
        campos["descanso_motivo"] = s.motivo
        log(f"{persona.apelido}: dispositivo parado ate "
            f"{AG.humano(campos['descanso_ate'])} depois de '{s.motivo}'")
    persona.gravar_meta(**campos)


# ------------------------------------------------------------------- sessao

class Sessao:
    """O que uma subida do Chrome rendeu."""

    def __init__(self):
        self.logou = False
        self.motivo = None
        self.dialogos = []
        self.pediu_nasc = False
        self.baixados = {}   # competencia -> caminho
        self.via2 = set()    # competencias que vieram da tela de 2a via
        self.ausentes = []   # o portal nao tem
        self.falhas = []     # o portal tem (ou pode ter) e nao entregou
        self.excecao = None  # a busca abortou no meio: nada do que faltou e "ausente"

    def anotar(self, comp, caminho, via2=False):
        if caminho:
            self.baixados[comp] = caminho
            if via2:
                self.via2.add(comp)
        else:
            self.falhas.append(comp)


def sessao(conta, persona, buscar):
    """Sobe o Chrome da persona, loga NESTA UC, roda buscar(page, ctx, s), desloga."""
    s = Sessao()
    orq.rotacionar_ip()
    pid, porta = nav.subir(persona)
    pw = browser = None
    try:
        pw, browser, ctx, page = orq.conectar(porta)
        dialogos = login.escutar_dialogos(page)
        login.UC, login.DOC, login.NASC = conta["uc"], conta["doc"], conta["nasc"]

        desde = time.time() - persona.meta().get("ultimo_login", 0)
        ok, motivo = login.tentar_login(page, dialogos, persona,
                                        sessao_possivel=desde < orq.SESSAO_VALE_S)
        if ok and motivo == "sessao":
            # Sessao herdada pode ser de OUTRA UC do mesmo titular (job anterior
            # cujo logout falhou). Baixar com ela gravaria a fatura da outra UC
            # sob o nome desta - entao ela morre e o login e refeito para esta.
            log("sessao anterior ainda de pe; encerrando antes de logar nesta UC")
            if not login.sair(page, persona):
                s.motivo = "sessao_presa"
                return s
            ok, motivo = login.tentar_login(page, dialogos, persona, sessao_possivel=False)
        s.motivo, s.dialogos, s.pediu_nasc = motivo, list(dialogos), login.PEDIU_NASC
        if not ok:
            return s

        s.logou = True
        try:
            buscar(page, ctx, s)
        except Exception as e:
            # Fica o que ja veio; o resto nao pode ser dado como "sem fatura".
            s.excecao = f"{type(e).__name__}: {str(e)[:200]}"
            log(f"busca abortada ({s.excecao[:120]})")
        finally:
            try:
                H.entre_telas(page, persona, mediana=2.0)
                login.sair(page, persona)
            except Exception as e:
                log(f"(logout falhou: {type(e).__name__}: {str(e)[:70]})")
            persona.gravar_meta(logins=persona.meta().get("logins", 0) + 1,
                                ultimo_login=time.time())
        return s
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


def _grid_2via(page, uc, persona):
    """Linhas do grid de faturas pendentes (tela de 2a via), ou [] se nao deu."""
    try:
        motivo = sv.abrir(page, uc, persona)
        if motivo:
            log(f"segunda via indisponivel ({motivo})")
            return []
        linhas, _ = sv.emitir_grid(page, persona)
        return linhas
    except Exception as e:
        log(f"segunda via falhou ({type(e).__name__}: {str(e)[:80]})")
        return []


def _baixar_2via(page, ctx, linha, nome, persona):
    try:
        return sv.baixar_linha(page, ctx, linha, persona, nome)
    except Exception as e:
        log(f"download da 2a via falhou ({type(e).__name__}: {str(e)[:80]})")
        return None


def _tabela_historico(page, persona, **consulta):
    """Linhas do historico apos consultar. None = a consulta falhou; [] = sem fatura."""
    try:
        ano = bf.consultar(page, persona, **consulta)
    except PWTimeout as e:
        log(f"timeout no historico ({str(e)[:70]})")
        return None
    except Exception as e:
        log(f"historico falhou ({type(e).__name__}: {str(e)[:80]})")
        return None
    return bf.linhas_faturas(page) if ano else []


def _baixar_hist(page, ctx, linha, nome, persona):
    try:
        # Olhar a tabela antes de clicar no download da linha certa.
        H.ler(page, persona, segundos=H.pausa_s(4.5, 0.5, teto=25))
        return bf.baixar_espelho(ctx, linha["invoice"], nome, persona)
    except Exception as e:
        log(f"download do historico falhou ({type(e).__name__}: {str(e)[:80]})")
        return None


def _ir_historico(page, persona):
    login.ir_para(page, login.URL_FATURAS)
    H.entre_telas(page, persona, mediana=3.0)


def buscar_meses(page, ctx, conta, persona, faltam, prazo, s):
    """Job MESES: cada competencia pedida, numa sessao so.

    Primeiro a tela de 2a via, que lista as faturas PENDENTES: um Emitir so e
    cada mes pedido que estiver ali e baixado do mesmo grid. O que sobrar (ja
    pago) vem do historico, uma consulta por ano. Download que falha ganha uma
    segunda chance com o grid/tabela remontado - o indice da linha e estado de
    sessao do WebForms e pode nao sobreviver ao download anterior.
    """
    uc = conta["uc"]
    restantes = sorted(faltam, key=_ordem)

    linhas = _grid_2via(page, uc, persona) if not passou(prazo) else []
    for comp in list(restantes):
        linha = sv.escolher_pendente(linhas, *comp)
        if not linha:
            continue
        if passou(prazo):
            break
        caminho = _baixar_2via(page, ctx, linha, nome_local(uc, comp), persona)
        if not caminho:
            linhas = _grid_2via(page, uc, persona)
            linha = sv.escolher_pendente(linhas, *comp)
            caminho = linha and _baixar_2via(page, ctx, linha, nome_local(uc, comp), persona)
        # Pendente que nao veio nao esta no historico (la so ha as pagas).
        s.anotar(comp, caminho, via2=True)
        restantes.remove(comp)

    por_ano = {}
    for comp in restantes:
        por_ano.setdefault(comp[1], []).append(comp)
    if not por_ano:
        return
    if passou(prazo):
        s.falhas.extend(restantes)
        return
    _ir_historico(page, persona)
    for ano in sorted(por_ano, reverse=True):
        comps = por_ano[ano]
        if passou(prazo):
            s.falhas.extend(comps)
            continue
        tabela = _tabela_historico(page, persona, ano_alvo=ano)
        if tabela is None:
            s.falhas.extend(comps)
            continue
        for comp in comps:
            linha = bf.escolher_linha(tabela, *comp)
            if not linha:
                log(f"{rotulo(comp)} nao esta no historico")
                s.ausentes.append(comp)
                continue
            if passou(prazo):
                s.falhas.append(comp)
                continue
            caminho = _baixar_hist(page, ctx, linha, nome_local(uc, comp), persona)
            if not caminho:
                tabela = _tabela_historico(page, persona, ano_alvo=ano) or []
                linha = bf.escolher_linha(tabela, *comp)
                caminho = linha and _baixar_hist(page, ctx, linha, nome_local(uc, comp), persona)
            s.anotar(comp, caminho)
    if passou(prazo):
        log(f"prazo de {PRAZO_S // 60} min do job esgotado; concluindo com o que veio")


def buscar_extrema(page, ctx, conta, persona, mais_recente, s):
    """Jobs MAIS_RECENTE / MAIS_ANTIGA: uma fatura so.

    Mais recente: a pendente mais nova da 2a via (o caso normal - a ultima
    fatura emitida ainda nao foi paga); sem pendente, o topo do historico.

    Mais antiga: o historico varrido do ano mais antigo para frente da a paga
    mais antiga; a 2a via entra na comparacao porque um titular novo, ou com
    divida antiga, pode ter uma pendente mais velha que todas as pagas.
    """
    uc = conta["uc"]
    if mais_recente:
        pend = sv.escolher_pendente(_grid_2via(page, uc, persona))
        if pend:
            comp = sv.mes_do_rotulo(pend["rotulo"])
            s.anotar(comp, _baixar_2via(page, ctx, pend, nome_local(uc, comp), persona),
                     via2=True)
            return
        _ir_historico(page, persona)
        tabela = _tabela_historico(page, persona)
        if tabela is None:
            s.falhas.append(None)
            return
        linha = bf.escolher_linha(tabela)
        if not linha:
            s.ausentes.append(None)
            return
        comp = bf.mes_do_rotulo(linha["rotulo"])
        s.anotar(comp, _baixar_hist(page, ctx, linha, nome_local(uc, comp), persona))
        return

    pend = sv.escolher_pendente(_grid_2via(page, uc, persona), mais_antiga=True)
    comp_pend = sv.mes_do_rotulo(pend["rotulo"]) if pend else None
    _ir_historico(page, persona)
    tabela = _tabela_historico(page, persona, mais_antigo=True)
    paga = bf.escolher_linha(tabela or [], mais_antiga=True)
    comp_paga = bf.mes_do_rotulo(paga["rotulo"]) if paga else None
    if paga and not (pend and _antes(comp_pend, comp_paga)):
        s.anotar(comp_paga, _baixar_hist(page, ctx, paga, nome_local(uc, comp_paga), persona))
        return
    if pend:
        # O grid da 2a via ficou para tras quando fomos ao historico: remonta.
        pend = sv.escolher_pendente(_grid_2via(page, uc, persona), mais_antiga=True)
        if pend:
            comp = sv.mes_do_rotulo(pend["rotulo"])
            s.anotar(comp, _baixar_2via(page, ctx, pend, nome_local(uc, comp), persona),
                     via2=True)
        else:
            s.falhas.append(comp_pend)
        return
    (s.falhas if tabela is None else s.ausentes).append(None)


# --------------------------------------------------------------------- job

def erro_de_login(s):
    """ERRO a reportar quando a sessao nao chegou a area logada."""
    texto = " | ".join(s.dialogos)[-300:]
    detalhe = f" Portal: {texto}" if texto else ""
    if s.motivo == "sessao_presa":
        return erro("ERRO_INTERNO", "Uma sessao anterior do titular nao encerrou; o job "
                    "nao rodou para nao gravar a fatura de outra UC.")
    if s.pediu_nasc:
        return erro("DATA_NASCIMENTO_AUSENTE", "O portal aceitou UC e documento e pediu a "
                    "data de nascimento do titular, que o job nao tem.")
    if s.motivo in ("imperva", "captcha", "backend"):
        return erro("CAPTCHA", f"Login barrado pelo antirrobo do portal ({s.motivo}).{detalhe}")
    if s.motivo == "rede":
        return erro("DISTRIBUIDORA_INDISPONIVEL", f"O portal nao respondeu ao login.{detalhe}")
    if s.motivo == "credencial":
        if RE_UC_DESCONHECIDA.search(texto):
            return erro("UC_NAO_ENCONTRADA", f"A distribuidora nao reconheceu a UC.{detalhe}")
        return erro("LOGIN_INVALIDO", f"A distribuidora recusou os dados de login.{detalhe}")
    return erro("ERRO_INTERNO", f"O login nao concluiu (motivo '{s.motivo}').{detalhe}")


def anotar_agenda(ag, conta, persona, s):
    """Leva o desfecho da sessao para a agenda: e ela que dita o backoff."""
    uc = conta["uc"]
    if s.motivo == "sessao_presa":
        return  # nenhuma tentativa de login nova aconteceu
    if not s.logou:
        ag.registrar_falha(uc, "sem_nascimento" if s.pediu_nasc else s.motivo)
    elif s.baixados:
        ag.registrar_sucesso(uc, persona, next(iter(s.baixados.values())))
    else:
        ag.registrar_falha(uc, "sem_fatura")


PENDENTE = {"status": "PENDENTE"}


def _pelo_portal(ag, conta, tipo, faltam, prazo):
    """Abre a sessao na persona do pool que atende este titular.

    Devolve (sessao|None, corpo de ERRO ou PENDENTE|None, o_que_houve).
    """
    persona = P.persona_para(conta["doc"])
    # Esperar pela persona so vale enquanto sobrar tempo para a sessao inteira
    # dentro dos 60 min que o servidor da ao job.
    limite = prazo - FOLGA_SESSAO_S
    trava = Trava(persona.chave)
    if not trava.esperar(limite):
        log(f"{persona.apelido} ocupada por outro worker; devolvendo o job a fila")
        return None, PENDENTE, None
    try:
        quando = pronta_em(persona)
        if quando > limite:
            m = persona.meta()
            if m.get("descanso_ate", 0) >= quando:
                motivo = (f"O dispositivo que atende este titular ({persona.apelido}) "
                          f"esta parado ate {AG.humano(quando)} depois de "
                          f"'{m.get('descanso_motivo', '?')}' numa sessao anterior.")
            else:
                motivo = (f"O dispositivo que atende este titular ({persona.apelido}) "
                          f"so fica livre as {AG.humano(quando)}.")
            return None, erro("TENTATIVA_ADIADA", motivo), None
        if quando > time.time():
            log(f"{persona.apelido} descansando; sessao as {AG.humano(quando)}")
            _dormir(quando, lambda: False)

        if tipo == "MESES":
            def buscar(page, ctx, s):
                buscar_meses(page, ctx, conta, persona, faltam, prazo, s)
        else:
            def buscar(page, ctx, s):
                buscar_extrema(page, ctx, conta, persona, tipo == "MAIS_RECENTE", s)
        log(f"titular {conta['apelido']} -> {persona.apelido}")
        s = None
        try:
            s = sessao(conta, persona, buscar)
        finally:
            marcar_uso(persona, s)
        anotar_agenda(ag, conta, persona, s)
        return s, (None if s.logou else erro_de_login(s)), "sessao"
    finally:
        trava.soltar()


def executar(job, drive):
    """Roda um job ja reservado. Devolve (corpo do PATCH, o_que_houve).

    o_que_houve: "sessao" (abriu o portal) ou None.
    """
    # O relogio de 60 min do servidor comecou na reserva, nao no login.
    prazo = time.time() + PRAZO_S
    ag = AG.Agenda()
    conta = conta_do_job(job)
    uc, tipo = conta["uc"], job.get("tipo")
    if not uc:
        return erro("UC_NAO_ENCONTRADA", "O job veio sem numeroUc."), None
    if len(conta["doc"]) not in (11, 14):
        return erro("LOGIN_INVALIDO", f"cpfCnpj com {len(conta['doc'])} digitos; "
                    "esperado 11 (CPF) ou 14 (CNPJ)."), None

    if tipo == "MESES":
        alvos = []
        for bruto in job.get("meses") or []:
            try:
                alvos.append(competencia(bruto))
            except ValueError:
                return erro("ERRO_INTERNO", f"Mes ilegivel no job: {bruto!r}."), None
        alvos = list(dict.fromkeys(alvos))
        if not alvos:
            return erro("ERRO_INTERNO", "Job MESES sem nenhum mes."), None
        locais = {c: p for c in alvos if (p := pdf_local(uc, c))}
        faltam = [c for c in alvos if c not in locais]
        log(f"{len(alvos)} mes(es): {len(locais)} ja em disco, {len(faltam)} pelo portal"
            + (f" ({', '.join(rotulo(c) for c in faltam)})" if faltam else ""))
    elif tipo in ("MAIS_RECENTE", "MAIS_ANTIGA"):
        locais, faltam = {}, [tipo]
    else:
        return erro("ERRO_INTERNO", f"Tipo de job desconhecido: {tipo!r}."), None

    s = bloqueio = houve = None
    if faltam:
        motivo = adiada(ag, conta)
        if motivo:
            bloqueio = erro("TENTATIVA_ADIADA", motivo)
        else:
            s, bloqueio, houve = _pelo_portal(ag, conta, tipo, faltam, prazo)
            if bloqueio is PENDENTE:
                return dict(PENDENTE), None

    arquivos = dict(locais)
    if s:
        arquivos.update(s.baixados)
    if not arquivos:
        if bloqueio:
            return bloqueio, houve
        if s and s.excecao:
            return erro("ERRO_INTERNO", f"A busca no portal abortou: {s.excecao}"), houve
        if s and s.falhas:
            pedidos = ", ".join(rotulo(c) for c in s.falhas if c) or tipo
            return erro("DISTRIBUIDORA_INDISPONIVEL",
                        f"O portal nao entregou o PDF ({pedidos})."), houve
        if tipo == "MESES":
            return erro("FATURA_INDISPONIVEL", "Nenhum dos meses pedidos tem fatura "
                        f"no portal ({', '.join(rotulo(c) for c in faltam)})."), houve
        return erro("FATURA_INDISPONIVEL", "O portal nao tem nenhuma fatura desta UC."), houve

    if bloqueio:
        log(f"so os {len(locais)} mes(es) em disco vao no CONCLUIDO; o resto ficou "
            f"para depois ({bloqueio['erro']}: {bloqueio['motivoErro'][:90]})")
    elif s and (s.ausentes or s.falhas or s.excecao):
        log(f"conclusao parcial: sem fatura no portal {[rotulo(c) for c in s.ausentes]}, "
            f"nao vieram {[rotulo(c) for c in s.falhas]}"
            + (f", busca abortada ({s.excecao[:80]})" if s.excecao else ""))

    ids = []
    try:
        for comp, caminho in sorted(arquivos.items(), key=lambda kv: _ordem(kv[0])):
            ids.append(drive.subir(uc, comp, caminho))
    except Exception as e:
        # Os PDFs ficam em faturas/: o proximo job desta UC so sobe, sem portal.
        return erro("ERRO_INTERNO", f"Falha ao subir o PDF no Google Drive: "
                    f"{type(e).__name__}: {str(e)[:300]}"), houve

    corpo = {"status": "CONCLUIDO",
             "arquivos": [{"driveFileId": i} for i in dict.fromkeys(ids)]}
    if s and s.baixados:
        corpo["obtidaSegundaVia"] = bool(s.via2)
    return corpo, houve


def guardar_job(job):
    _gravar_json(JOB_ATUAL, {"id": job["id"], "numeroUc": job.get("numeroUc"),
                             "reservado_em": time.time()})


def esquecer_job():
    try:
        JOB_ATUAL.unlink()
    except OSError:
        pass


def reportar(job, corpo):
    """PATCH do resultado. ErroChave sobe (o laco para); o resto e so log."""
    jid = job["id"]
    rotulo_status = corpo["status"] + (f" {corpo['erro']}" if corpo.get("erro") else "")
    try:
        r = GC.atualizar(jid, corpo)
        log(f"job {jid}: {rotulo_status} -> {r}"
            + (f" ({len(corpo['arquivos'])} arquivo(s))" if corpo.get("arquivos") else ""))
    except GC.ErroChave:
        raise
    except GC.ErroPayload as e:
        log(f"job {jid}: servidor recusou o {rotulo_status} ({e})")
        if corpo["status"] != "ERRO":
            # Sem isto o job ficaria EM_EXECUCAO ate o timeout de 60 min.
            try:
                GC.atualizar(jid, erro("ERRO_INTERNO",
                                       f"O servidor recusou o resultado do worker: {e}"))
            except GC.ErroChave:
                raise
            except GC.ErroAPI as e2:
                log(f"job {jid}: nem o ERRO passou ({e2})")
    except GC.ErroAPI as e:
        log(f"job {jid}: nao consegui reportar ({e}); o servidor devolve o job a "
            "fila depois de 60 min")
    esquecer_job()


def processar(job, drive):
    """Executa e reporta um job. Devolve o_que_houve (ver executar)."""
    guardar_job(job)
    meses = ", ".join(str(m)[:7] for m in (job.get("meses") or [])[:6])
    if len(job.get("meses") or []) > 6:
        meses += ", ..."
    log(f"=== job {job['id']} | UC {job.get('numeroUc')} | {job.get('tipo')}"
        + (f" [{meses}]" if meses else "")
        + f" | {job.get('origem', '-')} | reserva n. {job.get('tentativas', '?')} ===")
    houve = None
    try:
        corpo, houve = executar(job, drive)
    except KeyboardInterrupt:
        log(f"job {job['id']}: interrompido; devolvendo a fila")
        reportar(job, {"status": "PENDENTE"})
        raise
    except (Exception, SystemExit) as e:
        # SystemExit tambem: login.py ainda usa sys.exit() numa checagem interna
        # do formulario, e ela nao pode derrubar o worker com um job em maos.
        log(f"job {job['id']}: erro inesperado ({type(e).__name__}: {str(e)[:120]})")
        corpo = erro("ERRO_INTERNO", f"{type(e).__name__}: {str(e)[:300]}")
    reportar(job, corpo)
    return houve


def recuperar():
    """Devolve a fila o job que uma execucao anterior deixou reservado."""
    d = _ler_json(JOB_ATUAL, None)
    if not d:
        return
    jid = d.get("id")
    try:
        atual = GC.consultar(jid)
        if (atual and atual.get("status") == "EM_EXECUCAO"
                and atual.get("workerId") == GC.WORKER_ID):
            log(f"job {jid} ficou reservado por uma execucao que caiu; devolvendo a fila")
            GC.atualizar(jid, {"status": "PENDENTE"})
    except GC.ErroChave:
        raise
    except GC.ErroAPI as e:
        log(f"nao consegui conferir o job {jid} deixado pela execucao anterior ({e})")
    esquecer_job()


# ---------------------------------------------------------------------- laco

def _dormir(ate, parar):
    """Dorme ate `ate`, acordando cedo se `parar()` ficar verdadeiro."""
    while time.time() < ate and not parar():
        time.sleep(min(5, max(0.1, ate - time.time())))


def portao_pool(parar, uma=False):
    """True se o pool inteiro pode logar. Senao adianta o que der e devolve False.

    Na primeira largada as N personas nascem aqui: o worker matura uma por vez
    (os do paralelo dividem o pool pela trava de cada persona), com a pausa de
    sempre entre perfis, e nao reserva nada ate todas terem a idade minima. Um
    job que caisse numa persona verde nao teria como esperar 3-6 h.
    """
    verdes, jovens, quando = estado_pool()
    if verdes:
        if uma:
            log(f"pool com {len(verdes)} persona(s) por maturar; rode sem --uma primeiro")
            return False
        if maturar_uma_do_pool(verdes):
            _dormir(time.time() + random.randint(orq.MATURACAO_MIN, orq.MATURACAO_MAX), parar)
        else:
            _dormir(time.time() + 30, parar)  # as verdes estao com outros workers
        return False
    if jovens:
        if uma:
            log(f"pool maturado; {len(jovens)} persona(s) ganhando idade ate {AG.humano(quando)}")
            return False
        log(f"pool maturado; {len(jovens)} persona(s) ganhando idade ate "
            f"{AG.humano(quando)} - so entao o worker reserva jobs")
        _dormir(min(quando, time.time() + 1800), parar)
        return False
    return True


def laco(drive, uma=False, gerenciar_rede=True, parar=None):
    """Reserva, processa e reporta ate `parar()` ou Ctrl+C.

    gerenciar_rede=False e o modo do worker_paralelo: ali cada processo sai pelo
    proxy do proprio celular, e tomar a rota padrao da maquina daria a saida de
    todos a um aparelho so.
    """
    parar = parar or (lambda: False)
    recuperar()
    estado_rede = login.preparar_rede() if gerenciar_rede else None
    vazia = False
    try:
        while not parar():
            if not portao_pool(parar, uma):
                if uma:
                    return 0
                continue
            espera = portao(AG.Agenda())
            if espera and uma:
                log(f"fora da janela da agenda ate {AG.humano(espera)}; nada reservado")
                return 0
            if not espera:
                try:
                    # Um por vez: cada job e uma sessao de portal, e o guia pede
                    # para reservar so o que cabe em 60 min.
                    jobs = GC.reservar(1)
                except GC.ErroChave:
                    raise
                except GC.ErroAPI as e:
                    log(f"reserva falhou ({e}); nova tentativa em 60s")
                    if uma:
                        return 1
                    jobs, espera = None, time.time() + 60
                if jobs:
                    vazia = False
                    for job in jobs:
                        processar(job, drive)
                    if uma:
                        return 0
                    # Quem dosa a proxima sessao e o portao() (intervalo e
                    # cooldown da agenda) e o descanso de cada persona.
                    continue
                if jobs == []:
                    if uma:
                        log("fila vazia")
                        return 0
                    if not vazia:
                        log(f"fila vazia; consultando a cada {ESPERA_VAZIA}s")
                        vazia = True
                    espera = time.time() + ESPERA_VAZIA

            if espera - time.time() > 300:
                log(f"aguardando ate {AG.humano(espera)} (janela ou intervalo da agenda)")
            _dormir(min(espera, time.time() + 1800), parar)
        log("parada pedida; encerrando entre jobs")
        return 0
    finally:
        login.liberar_rede(estado_rede)


def resumo_pool():
    """Uma linha com o estado do pool: verdes, ganhando idade, paradas, prontas."""
    verdes, jovens, quando = estado_pool()
    agora = time.time()
    paradas = [p for p in P.pool() if p.meta().get("descanso_ate", 0) > agora]
    prontas = P.POOL_TAMANHO - len(verdes) - len(jovens) - len(paradas)
    partes = [f"pool de {P.POOL_TAMANHO}: {prontas} pronta(s)"]
    if verdes:
        partes.append(f"{len(verdes)} por maturar")
    if jovens:
        partes.append(f"{len(jovens)} ganhando idade ate {AG.humano(quando)}")
    for p in paradas:
        m = p.meta()
        partes.append(f"{p.apelido} parada ate {AG.humano(m['descanso_ate'])} "
                      f"({m.get('descanso_motivo', '?')})")
    return "; ".join(partes)


def checar():
    ok = True
    log(resumo_pool())
    try:
        log(GC.checar_chave())
    except GC.ErroAPI as e:
        log(f"API: {e}")
        ok = False
    try:
        d = Drive()
        log(f"Drive: envia como {d.conta()} para a pasta {d.pasta}. A conta de "
            "servico do gcenergy precisa de leitura nessa pasta.")
    except (Exception, SystemExit) as e:
        log(f"Drive: {e}")
        ok = False
    return 0 if ok else 1


def mostrar_job(jid):
    job = GC.consultar(jid)
    if job is None:
        print(f"job {jid} nao existe")
        return 1
    doc = job.get("cpfCnpj") or ""
    if doc:
        job["cpfCnpj"] = f"{doc[:3]}***{doc[-2:]}"
    print(json.dumps(job, indent=2, ensure_ascii=False))
    return 0


def main():
    args = sys.argv[1:]
    try:
        if "--checar" in args:
            return checar()
        if "--job" in args:
            i = args.index("--job")
            if i + 1 >= len(args):
                sys.exit("--job exige o id (ex.: --job 42)")
            return mostrar_job(args[i + 1])

        try:
            drive = Drive()
        except (Exception, SystemExit) as e:
            # Antes de reservar: sem Drive o job abriria o portal para nada.
            sys.exit(f"Google Drive indisponivel: {e}")
        P.PERFIS.mkdir(exist_ok=True)
        bf.FATURAS.mkdir(exist_ok=True)
        log(f"Worker {GC.WORKER_ID} em {GC.BASE} | janela {AG.HORA_INICIO}h-{AG.HORA_FIM}h"
            f" | sem teto diario | rotacao de IP "
            f"{'ligada' if orq.ROTACIONAR else 'desligada'} | Drive {drive.pasta}")
        log(resumo_pool())
        return laco(drive, uma="--uma" in args)
    except GC.ErroChave as e:
        log(f"PARANDO: {e}. Repetir nao resolve - confira GCENERGY_WORKER_KEY.")
        return 2
    except KeyboardInterrupt:
        log("Interrompido")
        return 130


if __name__ == "__main__":
    sys.exit(main())
