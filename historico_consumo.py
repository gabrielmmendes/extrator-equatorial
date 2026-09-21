#!/usr/bin/env python3
"""
Historico de consumo da UC, de uma lista em CSV, para outro CSV.

    AgenciaGO/Servicos/comum/HistoricoConsumo.aspx

Fluxo por titular (mesmo caminho do orquestrador): persona -> Chrome proprio ->
login -> HistoricoConsumo -> escolhe a UC no combo -> le a tabela -> proxima UC
do mesmo titular pelo combo, sem relogar.

Por que o combo e nao um login por UC: a tela lista TODAS as UCs do titular (e
o mesmo #CONTENT_comboBoxUC da SegundaVia). Com duas UCs no mesmo CPF, relogar
para a segunda paga dois logins pelo que uma sessao ja entrega - e login e o
recurso caro aqui, nao o clique. UC que nao aparece no combo do titular ganha
uma sessao propria, em que ela e a UC do login.

Gravacao incremental, de proposito: o worker pode morrer a qualquer momento (o
Chrome cai, o celular perde o tether, o Ctrl+C chega no meio). Cada UC fechada
e escrita e sincronizada no disco na hora, e o status vai para um segundo CSV.
Retomar e reler esse arquivo de status: UC com status ok nao roda de novo.

Nada aqui presume as colunas exatas da tabela do portal - e ASP.NET WebForms, e
o cabecalho ja mudou de nome antes neste projeto. Le-se o cabecalho real,
mapeiam-se as colunas conhecidas (competencia, consumo, dias, media, valor) e a
linha inteira vai junto no campo `bruto`, em JSON. Coluna nova aparece la, sem
perder coleta e sem exigir outra passada no site.

Uso:
    python historico_consumo.py ucs.csv
    python historico_consumo.py ucs.csv --saida saida/consumo.csv
    python historico_consumo.py ucs.csv --refazer        # ignora o ja coletado
    python historico_consumo.py ucs.csv --limite 3       # so 3 titulares
    python historico_consumo.py ucs.csv --sondar         # dumpa a tela e sai

CSV de entrada: cabecalho com `uc` e `cpf_cnpj` (aceita documento/doc/cpf/cnpj),
separador , ou ;. Colunas opcionais: nasc (DD/MM/AAAA) e apelido. Sem cabecalho
reconhecivel, assume-se "uc;cpf_cnpj" nas duas primeiras colunas.
"""

import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout

import baixar_fatura as bf
import humano as H
import login
import navegador as nav
import personas as P
import segunda_via as sv

RAIZ = Path(__file__).parent
SAIDA = RAIZ / "saida"
PADRAO_SAIDA = SAIDA / "historico-consumo.csv"

URL = ("https://goias.equatorialenergia.com.br/AgenciaGO/"
       "Servi%C3%A7os/comum/HistoricoConsumo.aspx")

# O combo da UC tem este id na tela de 2a via; aqui pode ter outro. Tentamos o
# conhecido e, se nao houver, procuramos QUALQUER select cujas opcoes contenham
# a UC - e isso que mantem a tela legivel se o id gerado mudar.
SEL_UC = "#CONTENT_comboBoxUC"
SEL_CONSULTAR = bf.SEL_CONSULTAR

# Cabecalhos que sabemos nomear. O resto vai para `bruto`.
COLUNAS = (
    ("competencia", r"m[eê]s|refer|compet|per[ií]odo|data.*leitura"),
    ("consumo_kwh", r"consumo|kwh"),
    ("dias",        r"\bdias?\b|qtde?\s*dias"),
    ("media",       r"m[eé]dia"),
    ("valor",       r"valor|r\$"),
)

CABECALHO = ["uc", "doc", "apelido", "competencia", "mes", "ano", "consumo_kwh",
             "dias", "media", "valor", "uc_confirmada", "coletado_em", "bruto"]
CABECALHO_STATUS = ["uc", "doc", "apelido", "status", "motivo", "linhas", "quando"]

# Vida util da sessao ASP.NET do portal. Igual a do orquestrador, e pelo mesmo
# motivo: sondar por uma sessao velha custa um page load a toa.
SESSAO_VALE_S = 20 * 60


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] hist: {msg}", flush=True)


def _slug(s):
    """minusculas, sem acento, sem pontuacao - para casar cabecalho."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def numero(txt):
    """"1.234,56 kWh" -> "1234.56". Devolve "" quando nao ha numero."""
    achado = re.search(r"-?\d[\d.\s]*(?:,\d+)?", str(txt or ""))
    if not achado:
        return ""
    bruto = achado.group(0).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return f"{float(bruto):g}"
    except ValueError:
        return ""


# --------------------------------------------------------------- entrada CSV

ALIAS_UC = {"uc", "unidade", "unidade_consumidora", "codigo_uc", "instalacao"}
ALIAS_DOC = {"cpf_cnpj", "cpfcnpj", "cpf", "cnpj", "documento", "doc",
             "cpf_ou_cnpj", "titular_documento"}
ALIAS_NASC = {"nasc", "nascimento", "data_nascimento", "dt_nascimento"}
ALIAS_APELIDO = {"apelido", "nome", "titular", "cliente"}


def carregar_csv(caminho):
    """Le o CSV de entrada e devolve [{uc, doc, nasc, apelido}].

    Linha invalida nao derruba o arquivo inteiro: e avisada e pulada. Parar uma
    fila de 300 UCs por causa de um CPF digitado errado trocaria um problema
    pequeno por um grande - mas passar batido tambem nao serve, porque a UC
    sumiria do resultado sem deixar rastro. Dai o aviso no log.
    """
    texto = Path(caminho).read_text(encoding="utf-8-sig")
    if not texto.strip():
        raise ValueError(f"{caminho} esta vazio")
    try:
        sep = csv.Sniffer().sniff(texto[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        sep = ";" if texto.count(";") > texto.count(",") else ","

    linhas = [l for l in csv.reader(texto.splitlines(), delimiter=sep)
              if any(c.strip() for c in l)]
    if not linhas:
        raise ValueError(f"{caminho} nao tem linhas")

    cab = [_slug(c) for c in linhas[0]]
    i_uc = next((i for i, c in enumerate(cab) if c in ALIAS_UC), None)
    i_doc = next((i for i, c in enumerate(cab) if c in ALIAS_DOC), None)
    if i_uc is None or i_doc is None:
        # Sem cabecalho reconhecivel: as duas primeiras colunas, e a primeira
        # linha e dado, nao cabecalho.
        log(f"{Path(caminho).name}: cabecalho nao reconhecido "
            f"({', '.join(cab[:4])}); assumindo uc;cpf_cnpj nas duas primeiras")
        i_uc, i_doc, i_nasc, i_apelido, corpo = 0, 1, None, None, linhas
    else:
        i_nasc = next((i for i, c in enumerate(cab) if c in ALIAS_NASC), None)
        i_apelido = next((i for i, c in enumerate(cab) if c in ALIAS_APELIDO), None)
        corpo = linhas[1:]

    def celula(linha, i):
        return linha[i].strip() if i is not None and i < len(linha) else ""

    contas, vistas = [], set()
    for n, linha in enumerate(corpo, start=2):
        uc = P.so_digitos(celula(linha, i_uc))
        doc = P.so_digitos(celula(linha, i_doc))
        if not uc or len(doc) not in (11, 14):
            log(f"linha {n}: ignorada (uc={celula(linha, i_uc)!r} "
                f"doc={celula(linha, i_doc)!r})")
            continue
        if uc in vistas:
            log(f"linha {n}: UC {uc} repetida no CSV; mantendo a primeira")
            continue
        vistas.add(uc)
        contas.append({
            "uc": uc, "doc": doc,
            "nasc": celula(linha, i_nasc),
            "apelido": celula(linha, i_apelido) or uc[-6:],
        })
    if not contas:
        raise ValueError(f"{caminho}: nenhuma linha valida (uc + cpf_cnpj)")
    return contas


# ---------------------------------------------------------------- saida CSV

class Saida:
    """Dois CSVs em modo append: os dados e o status por UC.

    flush + fsync a cada UC fechada. Sem o fsync o buffer do SO segura as
    ultimas linhas, e "worker morto do nada" e exatamente o caso em que esse
    buffer nao chega ao disco - perder a ultima UC seria perder justamente a
    que acabou de custar um login.
    """

    def __init__(self, caminho, status=None):
        self.caminho = Path(caminho)
        self.status_caminho = Path(status) if status else \
            self.caminho.with_name(self.caminho.stem + "-status.csv")
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.status_caminho.parent.mkdir(parents=True, exist_ok=True)

        self.feitas = self._ucs_prontas()
        novo = not self.caminho.exists() or self.caminho.stat().st_size == 0
        novo_status = (not self.status_caminho.exists()
                       or self.status_caminho.stat().st_size == 0)
        self._f = open(self.caminho, "a", newline="", encoding="utf-8")
        self._st = open(self.status_caminho, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=CABECALHO)
        self._ws = csv.DictWriter(self._st, fieldnames=CABECALHO_STATUS)
        if novo:
            self._w.writeheader()
        if novo_status:
            self._ws.writeheader()
        self._sincronizar()

    def _ucs_prontas(self):
        """UCs com status ok de uma execucao anterior."""
        if not self.status_caminho.exists():
            return set()
        prontas = set()
        with open(self.status_caminho, newline="", encoding="utf-8") as fh:
            for linha in csv.DictReader(fh):
                if (linha.get("status") or "").strip() == "ok":
                    prontas.add(P.so_digitos(linha.get("uc")))
        return prontas

    def _sincronizar(self):
        for fh in (self._f, self._st):
            fh.flush()
            os.fsync(fh.fileno())

    def gravar(self, conta, registros, confirmada):
        agora = datetime.now().isoformat(timespec="seconds")
        for r in registros:
            self._w.writerow({
                "uc": conta["uc"], "doc": conta["doc"],
                "apelido": conta["apelido"],
                "competencia": r.get("competencia", ""),
                "mes": r.get("mes", ""), "ano": r.get("ano", ""),
                "consumo_kwh": r.get("consumo_kwh", ""),
                "dias": r.get("dias", ""), "media": r.get("media", ""),
                "valor": r.get("valor", ""),
                "uc_confirmada": "sim" if confirmada else "nao",
                "coletado_em": agora,
                "bruto": json.dumps(r.get("bruto", {}), ensure_ascii=False),
            })

    def status(self, conta, status, motivo="", linhas=0):
        """Fecha a UC: status no disco, sincronizado. E o ponto de retomada."""
        self._ws.writerow({
            "uc": conta["uc"], "doc": conta["doc"], "apelido": conta["apelido"],
            "status": status, "motivo": motivo, "linhas": linhas,
            "quando": datetime.now().isoformat(timespec="seconds"),
        })
        self._sincronizar()
        if status == "ok":
            self.feitas.add(conta["uc"])

    def fechar(self):
        for fh in (self._f, self._st):
            try:
                fh.close()
            except Exception:
                pass


# ------------------------------------------------------------------- a tela

def dump(page, rotulo):
    """bf.dump_estrutura, mas gravando em saida/ em vez de faturas/."""
    antes = bf.DESCARGA
    bf.DESCARGA = SAIDA
    try:
        return bf.dump_estrutura(page, rotulo)
    except Exception as e:
        log(f"(dump da estrutura falhou: {str(e)[:70]})")
        return {}
    finally:
        bf.DESCARGA = antes


def select_da_uc(page, uc):
    """(seletor, value) do select que oferece esta UC, ou (None, None).

    Primeiro o id conhecido da 2a via; depois qualquer select visivel cujas
    opcoes contenham os digitos da UC. Casar por digitos, e nao por texto, e o
    que atravessa a mascara: o portal escreve a mesma UC como 000123456012345,
    "0001234560-1234" e "1234560-1234" conforme a tela.
    """
    digitos = P.so_digitos(uc)
    return page.evaluate(
        """([sel, uc]) => {
            const dig = s => (s || '').replace(/\\D/g, '');
            const todos = [...document.querySelectorAll('select')];
            const ordenados = [
                ...todos.filter(s => s.matches(sel)),
                ...todos.filter(s => !s.matches(sel) && s.offsetParent !== null),
            ];
            for (const s of ordenados) {
                for (const o of s.options) {
                    if (!o.value) continue;
                    const v = dig(o.value), t = dig(o.text);
                    if (v === uc || t === uc || v.endsWith(uc) || t.endsWith(uc)) {
                        const alvo = s.id ? '#' + s.id
                                   : (s.name ? 'select[name="' + s.name + '"]' : null);
                        return [alvo, o.value];
                    }
                }
            }
            return [null, null];
        }""",
        [SEL_UC, digitos],
    )


def ler_tabela(page):
    """Le a tabela do historico e devolve [{cabecalho_slug: celula}] ou None.

    Escolhe a tabela pelo CABECALHO, nao pelo tamanho: a pagina tem tabelas de
    layout, e "a maior" costuma ser o menu. Ganha a que tem coluna de
    consumo/kWh ou de mes/referencia; o numero de linhas so desempata.
    """
    return page.evaluate(r"""() => {
        const txt = e => (e.innerText || '').replace(/\s+/g, ' ').trim();
        const slug = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '')
                           .toLowerCase().replace(/[^a-z0-9]+/g, '_')
                           .replace(/^_|_$/g, '');
        let melhor = null, nota = -1;
        for (const t of document.querySelectorAll('table')) {
            const linhas = [...t.rows].map(r => [...r.cells].map(txt));
            if (linhas.length < 2) continue;
            const cab = linhas[0].map(slug).join(' ');
            let n = 0;
            if (/consumo|kwh/.test(cab)) n += 10;
            if (/mes|refer|compet|periodo|leitura/.test(cab)) n += 6;
            if (n === 0) continue;
            n += Math.min(linhas.length, 40) / 100;
            if (n > nota) { nota = n; melhor = linhas; }
        }
        if (!melhor) return null;
        const cab = melhor[0].map((c, i) => slug(c) || ('col' + (i + 1)));
        return melhor.slice(1)
            .filter(l => l.some(c => /\d/.test(c)))
            .map(l => Object.fromEntries(cab.map((c, i) => [c, l[i] || ''])));
    }""")


def normalizar(linhas):
    """Da forma as linhas cruas: competencia MM/AAAA e numeros com ponto."""
    saida = []
    for linha in linhas or []:
        reg = {"bruto": linha}
        for nome, padrao in COLUNAS:
            # O cabecalho ja chega em slug ("qtd_dias"), e "_" e caractere de
            # palavra: com o underline no meio, \bdias\b nunca casaria. Ler o
            # slug com espaco no lugar do underline devolve as bordas.
            chave = next((k for k in linha
                          if re.search(padrao, k.replace("_", " "), re.I)), None)
            if chave is not None:
                reg[nome] = (linha[chave] or "").strip()
        for nome in ("consumo_kwh", "dias", "media", "valor"):
            if reg.get(nome):
                reg[nome] = numero(reg[nome])
        # sv.mes_do_rotulo entende tanto "AGO/2026" quanto "08/2026". Se a
        # coluna de competencia nao rendeu, a linha inteira e o proximo lugar
        # onde a referencia costuma estar.
        ref = (sv.mes_do_rotulo(reg.get("competencia", ""))
               or sv.mes_do_rotulo(" ".join(str(v) for v in linha.values())))
        if ref:
            reg["mes"], reg["ano"] = ref
            reg["competencia"] = f"{ref[0]:02d}/{ref[1]}"
        saida.append(reg)
    return saida


def uc_na_pagina(page, uc):
    """True se os digitos da UC aparecem no texto da tela.

    Guarda contra o pior desfecho possivel aqui: gravar o consumo de uma UC sob
    o numero de outra. Quando a tela nao repete a UC a coleta segue - mas as
    linhas vao com uc_confirmada=nao, e quem le o CSV sabe o que tem na mao.
    """
    try:
        texto = P.so_digitos(login.texto_da_pagina(page))
    except Exception:
        return False
    digitos = P.so_digitos(uc)
    return digitos in texto or digitos.lstrip("0") in texto


def consultar_uc(page, uc, persona=None):
    """Poe a UC na tela e le o historico. Devolve (registros, motivo).

    motivo: "ok", "uc_ausente", "vazio" ou "login".
    """
    login.ir_para(page, URL)
    try:
        page.wait_for_selector("select, table", timeout=25_000)
    except PWTimeout:
        pass
    H.entre_telas(page, persona, mediana=2.6)

    # O campo de UC do cabecalho so existe deslogado: v e-lo aqui significa que
    # a sessao caiu no meio do titular.
    if page.locator(login.SEL_UC).count():
        return None, "login"

    seletor, valor = select_da_uc(page, uc)
    if seletor is None:
        dump(page, "consumo-sem-combo")
        log(f"UC {uc}: nao esta no combo desta sessao")
        return None, "uc_ausente"

    H.clicar(page.locator(seletor).first, page, persona, hesitacao=0.2)
    page.select_option(seletor, value=valor)
    H.pensar(page, persona, 1.1, 0.55, teto=8)

    botao = page.locator(SEL_CONSULTAR).first
    if botao.count():
        H.clicar(botao, page, persona)
        rotulo = "Consultar"
    else:
        # Sem botao, a tela consulta no proprio onchange do select: nao ha o
        # que clicar, so o que esperar.
        rotulo = "onchange do combo"
    login.esperar_postback(page, lambda: bool(ler_tabela(page)), rotulo=rotulo)
    H.ler(page, persona, segundos=H.pausa_s(3.5, 0.5, teto=18))

    linhas = ler_tabela(page)
    if not linhas:
        dump(page, f"consumo-vazio-{uc}")
        return None, "vazio"
    return normalizar(linhas), "ok"


# ---------------------------------------------------------------- titulares

def coletar_titular(contas, saida, forcar=True):
    """Roda todas as UCs de um titular, gravando a cada UC fechada.

    O laco externo existe por causa das UCs que nao aparecem no combo: elas
    voltam para a fila e ganham uma sessao propria, em que sao a UC do login.
    Uma passada que nao atende ninguem encerra o titular - insistir ali seria
    girar em falso gastando login, que e o recurso caro.

    Devolve o motivo que interrompeu o titular, ou None.
    """
    import orquestrador as orq

    persona = P.persona_de(contas[0]["doc"], contas[0]["apelido"])
    etiqueta = "/".join(c["apelido"] for c in contas)
    log(f"=== {etiqueta} | {len(contas)} UC(s) | perfil {persona.dir.name} ===")
    orq.rotacionar_ip()

    pendentes = list(contas)
    pid, porta = nav.subir(persona)
    pw = browser = None
    try:
        pw, browser, ctx, page = orq.conectar(porta)
        dialogos = login.escutar_dialogos(page)

        # Perfil que nunca navegou nada nao estreia na pagina de login: o score
        # do reCAPTCHA v3 vem sobretudo do historico do navegador.
        if not persona.meta().get("maturado_em"):
            nav.maturar(ctx, persona)
            if not forcar:
                log(f"{etiqueta}: perfil maturado; login fica para depois")
                for c in pendentes:
                    saida.status(c, "adiada", "maturado")
                return "maturado"
        if not (persona.madura or forcar):
            log(f"{etiqueta}: perfil com {persona.idade_h:.1f}h, ainda verde")
            for c in pendentes:
                saida.status(c, "adiada", "imaturo")
            return "imaturo"

        while pendentes:
            conta = pendentes[0]
            login.UC, login.DOC = conta["uc"], conta["doc"]
            login.NASC = conta.get("nasc", "")
            desde = time.time() - persona.meta().get("ultimo_login", 0)
            ok, motivo = login.tentar_login(page, dialogos, persona,
                                            sessao_possivel=desde < SESSAO_VALE_S)
            if not ok:
                log(f"{etiqueta}: login recusado ({motivo})")
                for c in pendentes:
                    saida.status(c, "falha", motivo)
                return motivo

            atendidas = []
            for c in pendentes:
                try:
                    registros, motivo = consultar_uc(page, c["uc"], persona)
                except PWTimeout as e:
                    log(f"{c['apelido']}: timeout na consulta ({str(e)[:70]})")
                    saida.status(c, "falha", "timeout")
                    atendidas.append(c)
                    continue
                except Exception as e:
                    log(f"{c['apelido']}: erro inesperado "
                        f"({type(e).__name__}: {str(e)[:80]})")
                    saida.status(c, "falha", "erro")
                    atendidas.append(c)
                    continue

                if motivo == "uc_ausente":
                    continue        # fica na fila: a proxima sessao loga com ela
                if motivo == "login":
                    log(f"{c['apelido']}: sessao caiu no meio do titular")
                    break           # relogar e retomar o resto da fila
                if motivo != "ok":
                    log(f"{c['apelido']}: sem historico ({motivo})")
                    saida.status(c, "falha", motivo)
                    atendidas.append(c)
                    continue

                confirmada = uc_na_pagina(page, c["uc"])
                if not confirmada:
                    log(f"{c['apelido']}: a tela nao repetiu a UC; "
                        "linhas marcadas com uc_confirmada=nao")
                saida.gravar(c, registros, confirmada)
                saida.status(c, "ok", "", len(registros))
                log(f"{c['apelido']}: {len(registros)} competencia(s) gravadas")
                atendidas.append(c)
                H.entre_telas(page, persona, mediana=3.0)

            login.sair(page)
            persona.gravar_meta(logins=persona.meta().get("logins", 0) + 1,
                                ultimo_login=time.time())
            if not atendidas:
                # Nem a UC do proprio login apareceu no combo: a tela nao vai
                # mudar de ideia numa segunda sessao.
                for c in pendentes:
                    saida.status(c, "falha", "uc_ausente")
                return "uc_ausente"
            pendentes = [c for c in pendentes if c not in atendidas]
            if pendentes:
                log(f"{etiqueta}: {len(pendentes)} UC(s) fora do combo; "
                    "nova sessao para elas")
                H.entre_telas(page, persona, mediana=4.0)
        return None
    except Exception as e:
        log(f"{etiqueta}: erro inesperado ({type(e).__name__}: {str(e)[:90]})")
        for c in pendentes:
            saida.status(c, "falha", "erro")
        return "erro"
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


def sondar(conta):
    """Loga uma conta, abre a tela e dumpa o que ha nela. Nao grava CSV.

    Existe pelo mesmo motivo do --sondar da segunda_via: os ids do WebForms so
    aparecem logado, e ajustar seletor as cegas custa uma sessao por tentativa.
    """
    import orquestrador as orq

    persona = P.persona_de(conta["doc"], conta["apelido"])
    pid, porta = nav.subir(persona)
    pw = browser = None
    try:
        pw, browser, ctx, page = orq.conectar(porta)
        dialogos = login.escutar_dialogos(page)
        login.UC, login.DOC = conta["uc"], conta["doc"]
        login.NASC = conta.get("nasc", "")
        ok, motivo = login.tentar_login(page, dialogos, persona)
        if not ok:
            log(f"sondagem: login recusado ({motivo})")
            return 1
        login.ir_para(page, URL)
        page.wait_for_timeout(4000)
        dump(page, "consumo-form")
        log(f"url={page.url[:100]}")
        log(f"select da UC: {select_da_uc(page, conta['uc'])}")
        linhas = ler_tabela(page)
        log(f"tabela antes de consultar: {len(linhas or [])} linha(s)")
        registros, motivo = consultar_uc(page, conta["uc"], persona)
        log(f"consulta: motivo={motivo} linhas={len(registros or [])}")
        if registros:
            log(json.dumps(registros[0], ensure_ascii=False)[:400])
        dump(page, "consumo-resultado")
        login.sair(page)
        return 0
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


def main():
    from enviar_drive import carregar_env
    import orquestrador as orq

    carregar_env()
    args = sys.argv[1:]

    def valor(flag, padrao=None):
        if flag in args and args.index(flag) + 1 < len(args):
            return args[args.index(flag) + 1]
        for a in args:
            if a.startswith(flag + "="):
                return a.split("=", 1)[1]
        return padrao

    # O valor de uma flag nao e o CSV de entrada.
    consumidos = {v for v in (valor("--saida"), valor("--limite")) if v}
    entradas = [a for a in args if not a.startswith("-") and a not in consumidos]
    if not entradas:
        sys.exit("Informe o CSV de entrada: python historico_consumo.py ucs.csv "
                 "(colunas uc e cpf_cnpj)")

    try:
        contas = carregar_csv(entradas[0])
    except (OSError, ValueError) as e:
        sys.exit(str(e))
    log(f"{len(contas)} UC(s) no CSV de entrada")

    SAIDA.mkdir(exist_ok=True)
    P.PERFIS.mkdir(exist_ok=True)

    if "--sondar" in args:
        return sondar(contas[0])

    saida = Saida(valor("--saida", PADRAO_SAIDA))
    if "--refazer" in args:
        saida.feitas = set()
    elif saida.feitas:
        antes = len(contas)
        contas = [c for c in contas if c["uc"] not in saida.feitas]
        log(f"{antes - len(contas)} UC(s) ja coletadas em "
            f"{saida.status_caminho.name}; retomando as {len(contas)} restantes")
    if not contas:
        log("Nada a coletar")
        saida.fechar()
        return 0

    grupos = orq.por_titular(contas)
    limite = valor("--limite")
    if limite:
        grupos = grupos[:int(limite)]
    log(f"{sum(len(g) for g in grupos)} UC(s) em {len(grupos)} titular(es) "
        f"-> {saida.caminho}")

    # A rota so vai para o celular com EQUATORIAL_ROTACIONAR=1; o finally
    # devolve a maquina ao estado anterior mesmo com Ctrl+C.
    estado_rede = login.preparar_rede()
    try:
        for grupo in grupos:
            if coletar_titular(grupo, saida) == "imperva":
                log("Imperva marcou este par (IP, dispositivo). Parando o lote "
                    "para nao queimar as personas restantes.")
                break
    except KeyboardInterrupt:
        log("Interrompido; o que ja foi coletado esta no CSV")
        return 130
    finally:
        login.liberar_rede(estado_rede)
        saida.fechar()

    log(f"===== FIM ===== {len(saida.feitas)} UC(s) com historico "
        f"em {saida.caminho}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
