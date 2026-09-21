#!/usr/bin/env python3
"""
Personas: uma identidade de navegador por titular (CPF/CNPJ).

Por que trocar perfil em vez de limpar cookie
---------------------------------------------
ctx.clear_cookies() apaga cookie, mas NAO apaga localStorage, IndexedDB nem
Web SQL - e e ali que o Transmit Security guarda o device ID. O resultado de
limpar so o cookie e um visitante aparentemente novo (sem visid_incap_)
carregando o device ID de outro: exatamente a incoerencia que o DRS pontua.

Trocar o --user-data-dir do Chrome troca cookie, localStorage, IndexedDB,
service workers e cache de uma vez, e de forma coerente entre si. Por isso a
persona e um diretorio de perfil, nao um estado limpo por software.

Por que os tracos sao deterministicos
-------------------------------------
Um dispositivo real tem personalidade estavel: a mesma pessoa digita mais ou
menos na mesma velocidade toda vez, usa a janela do mesmo tamanho e costuma
abrir a fatura mais ou menos no mesmo dia do mes. Sortear velocidade de
digitacao a cada execucao produz um device ID cujo comportamento muda toda
sessao - sinal pior do que digitar rapido. Entao tudo aqui deriva de um hash
do documento: mesma persona, mesmos tracos, para sempre.
"""

import hashlib
import json
import os
import random
import shutil
import time
from pathlib import Path

RAIZ = Path(__file__).parent
PERFIS = RAIZ / "perfis"
CONTAS = RAIZ / "contas.json"

# Quantas horas um perfil recem-nascido espera antes do primeiro login. Sorteado
# por persona (personas nao estreiam todas juntas) e gravado no .persona.json,
# entao mudar estes numeros so afeta perfis novos.
#
# Era 10-30 h, por inferencia: idade de dispositivo e sinal que o reCAPTCHA v3,
# o Imperva e o DRS coletam, e perfil de minutos e o padrao do descartavel. O
# que existe de medicao aponta mais baixo - o lote de 04/09/2026 rodou com
# perfis de 3-4 h e fechou 5/5. Dai 3-6 h: acima da unica idade que ja deu certo
# aqui, e sem gastar dois dias da janela de 30.
MATURACAO_H_MIN = float(os.environ.get("EQUATORIAL_MATURACAO_H_MIN", "3"))
MATURACAO_H_MAX = max(MATURACAO_H_MIN,
                      float(os.environ.get("EQUATORIAL_MATURACAO_H_MAX", "6")))

# Resolucoes comuns em desktop/notebook no Brasil. A janela nao e o viewport,
# mas e dela que saem window.outerWidth/outerHeight e, junto com a screen do
# monitor real, a coerencia que o fingerprint confere.
JANELAS = [(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)]

_TELA = None


def tela():
    """Resolucao do monitor primario, em cache.

    Precisa ser lida de verdade: a janela so e coerente em relacao a tela. Uma
    janela do tamanho exato da tela mas posicionada fora de 0,0 e impossivel -
    janela maximizada fica em 0,0 -, e essa contradicao e barata de detectar.
    """
    global _TELA
    if _TELA is None:
        try:
            import subprocess
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Add-Type -AssemblyName System.Windows.Forms; "
                 "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                 "\"$($b.Width)x$($b.Height)\""],
                capture_output=True, text=True, timeout=25)
            w, h = r.stdout.strip().lower().split("x")
            _TELA = (int(w), int(h))
        except Exception:
            _TELA = (1920, 1080)
    return _TELA

# Diretorios do perfil que sao puro peso: cache, telemetria e modelos que o
# Chrome rebaixa sozinho. Apagar nao afeta identidade - o que define a persona e
# Cookies, Local Storage, IndexedDB, History, Preferences e o Local State (que
# guarda a chave de criptografia dos cookies), e nada disso esta nesta lista.
# Sem essa poda, um perfil chega a 100 MB e 300 personas passam de 30 GB.
DESCARTAVEIS = (
    # caches
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "GraphiteDawnCache", "DawnCache", "DawnWebGPUCache", "DawnGraphiteCache",
    "component_crx_cache", "extensions_crx_cache",
    # telemetria e crash
    "Crashpad", "BrowserMetrics", "DeferredBrowserMetrics",
    # componentes baixados, todos reconstruiveis
    "optimization_guide_model_store", "optimization_guide_prediction_model_downloads",
    "OptimizationHints", "OnDeviceHeadSuggestModel", "Safe Browsing",
    "ActorSafetyLists", "CertificateRevocation", "Subresource Filter",
    "SwReporter", "Webstore Downloads", "segmentation_platform",
)
# Arquivos soltos, mesmo criterio.
DESCARTAVEIS_ARQ = ("CrashpadMetrics.pma", "CrashpadMetrics-active.pma")


def so_digitos(s):
    return "".join(filter(str.isdigit, s or ""))


class Persona:
    """Identidade estavel ligada a um CPF/CNPJ."""

    def __init__(self, doc, apelido=None):
        self.doc = so_digitos(doc)
        self.apelido = apelido or self.doc[:6]
        self.dir = PERFIS / self.doc
        # Semente propria: independente do random global, para os tracos nao
        # mudarem se alguem chamar random.seed() em outro lugar.
        semente = int(hashlib.sha256(self.doc.encode()).hexdigest()[:16], 16)
        r = random.Random(semente)

        # Digitacao: base + desvio. Humanos reais ficam entre ~90ms e ~280ms por
        # tecla em campo numerico com mascara.
        self.tecla_ms = r.randint(95, 215)
        self.tecla_desvio = r.randint(25, 70)
        # Chance por caractere de errar e corrigir com backspace. Zero para
        # algumas personas: nem todo mundo erra.
        self.taxa_erro = r.choice([0.0, 0.0, 0.012, 0.02, 0.035])
        # Permanencia na pagina de login antes de preencher.
        self.dwell_base = r.randint(22, 55)
        # Quantas paginas internas visita antes de ir ao login.
        self.passos_aquecimento = r.choice([1, 1, 2, 2, 3])
        self.janela = self._janela(r)
        # Faixa de horario preferida (hora inicial). Gente tem rotina.
        self.hora_pico = r.choice([9, 10, 11, 14, 15, 16, 19, 20])
        # Dia do mes em que essa persona costuma conferir a fatura. Limitado a
        # 28 para existir em fevereiro.
        self.dia_ancora = r.randint(3, 26)
        self._rng = r

    @staticmethod
    def _janela(r):
        """Janela que cabe na tela com folga, e com medida "suja".

        Duas coisas de proposito: (a) nunca do tamanho da tela, porque nao
        estamos posicionando em 0,0 e janela maximizada fora da origem nao
        existe; (b) largura e altura com resto - ninguem arrasta a borda ate
        exatamente 1440x900, e um outerWidth redondo demais e ele proprio um
        pequeno sinal.
        """
        tw, th = tela()
        cabem = [j for j in JANELAS if j[0] <= tw - 120 and j[1] <= th - 160]
        if not cabem:
            base = (int(tw * 0.78), int(th * 0.80))
        else:
            base = cabem[r.randrange(len(cabem))]
        return (base[0] - r.randint(0, 37), base[1] - r.randint(0, 29))

    # ---------------------------------------------------------------- estado

    @property
    def marcador(self):
        return self.dir / ".persona.json"

    def meta(self):
        if self.marcador.exists():
            try:
                return json.loads(self.marcador.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def gravar_meta(self, **campos):
        d = self.meta()
        d.update(campos)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.marcador.write_text(json.dumps(d, indent=2), encoding="utf-8")
        return d

    @property
    def nova(self):
        return not self.marcador.exists()

    @property
    def idade_h(self):
        """Horas desde a criacao do perfil. Perfil recem-nascido e sinal ruim:
        tanto o Imperva quanto o DRS pontuam idade de dispositivo, e o score do
        reCAPTCHA v3 depende de historico de cookies do Google que so existe
        depois de algum uso."""
        nascimento = self.meta().get("nascido_em")
        if not nascimento:
            return 0.0
        return (time.time() - nascimento) / 3600.0

    @property
    def madura(self):
        return self.idade_h >= float(self.meta().get("maturacao_h", MATURACAO_H_MAX))

    # ------------------------------------------------------------ manutencao

    def podar(self):
        """Apaga o cache do perfil sem tocar na identidade."""
        libertos = 0
        for base in (self.dir, self.dir / "Default"):
            if not base.is_dir():
                continue
            for nome in DESCARTAVEIS:
                alvo = base / nome
                if alvo.is_dir():
                    libertos += sum(f.stat().st_size
                                    for f in alvo.rglob("*") if f.is_file())
                    shutil.rmtree(alvo, ignore_errors=True)
            for nome in DESCARTAVEIS_ARQ:
                alvo = base / nome
                try:
                    if alvo.is_file():
                        libertos += alvo.stat().st_size
                        alvo.unlink()
                except OSError:
                    pass
        return libertos

    def tamanho(self):
        if not self.dir.exists():
            return 0
        return sum(f.stat().st_size for f in self.dir.rglob("*") if f.is_file())

    def __repr__(self):
        return f"<Persona {self.apelido} doc={self.doc[:5]}... perfil={self.dir.name}>"


# --------------------------------------------------------------------- contas

def carregar_contas(caminho=CONTAS, atualizar=True):
    """Contas a coletar, com digitos limpos.

    A fonte e a API do Minha Fatura quando ha MF_API_KEY (ou MF_FONTE=api);
    contas.json continua valendo para uso manual e como plano B. O import e
    tardio de proposito: api.py importa este modulo, e no topo daria ciclo.
    """
    import api
    if api.ativa():
        return api.contas(atualizar=atualizar)
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    saida = []
    for c in dados.get("contas", []):
        uc, doc = so_digitos(c.get("uc")), so_digitos(c.get("doc"))
        if not uc or len(doc) not in (11, 14):
            raise ValueError(f"Conta invalida em contas.json: {c!r}")
        saida.append({
            "uc": uc, "doc": doc,
            "nasc": (c.get("nasc") or "").strip(),
            "apelido": c.get("apelido") or uc[-6:],
        })
    return saida


_cache = {}


def persona_de(doc, apelido=None):
    """Devolve a Persona do documento, reaproveitando a instancia."""
    d = so_digitos(doc)
    if d not in _cache:
        _cache[d] = Persona(d, apelido)
    return _cache[d]


if __name__ == "__main__":
    PERFIS.mkdir(exist_ok=True)
    for c in carregar_contas():
        p = persona_de(c["doc"], c["apelido"])
        estado = "NOVA" if p.nova else f"{p.idade_h:.1f}h, {p.tamanho() / 1e6:.1f} MB"
        print(f"{c['apelido']:<8} uc={c['uc']} perfil={p.dir.name}  [{estado}]")
        print(f"         tecla={p.tecla_ms}+-{p.tecla_desvio}ms erro={p.taxa_erro:.1%} "
              f"dwell={p.dwell_base}s janela={p.janela[0]}x{p.janela[1]} "
              f"pico={p.hora_pico}h dia={p.dia_ancora}")
