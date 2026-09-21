#!/usr/bin/env python3
"""
Confere o que o site vai ler do navegador, antes de gastar uma tentativa.

Cada item aqui e um sinal que Imperva, reCAPTCHA v3 ou o DRS do Transmit lem.
Rodar isto custa nada e evita o pior desperdicio do projeto: queimar uma
tentativa (e a reputacao do IP) com um Chrome que ja se denunciava sozinho -
tipicamente renderer de software, timezone fora do Brasil ou webdriver ligado.

    python diagnostico.py            # persona descartavel
    python diagnostico.py <cpf/cnpj> # persona real (nao loga em nada)
"""

import sys

from playwright.sync_api import sync_playwright

import navegador as nav
import personas as P

SONDA = """() => {
    const gl = document.createElement('canvas').getContext('webgl');
    const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return {
        webdriver: navigator.webdriver,
        ua: navigator.userAgent,
        uaData: navigator.userAgentData
            ? JSON.stringify(navigator.userAgentData.brands) : null,
        idiomas: navigator.languages.join(','),
        plataforma: navigator.platform,
        nucleos: navigator.hardwareConcurrency,
        memoria: navigator.deviceMemory,
        plugins: navigator.plugins.length,
        fuso: Intl.DateTimeFormat().resolvedOptions().timeZone,
        offset: new Date().getTimezoneOffset(),
        tela: `${screen.width}x${screen.height}`,
        janela: `${window.outerWidth}x${window.outerHeight}`,
        viewport: `${window.innerWidth}x${window.innerHeight}`,
        dpr: window.devicePixelRatio,
        chrome: typeof window.chrome,
        permissoes: typeof navigator.permissions,
        webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
        webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
        cdcVazado: Object.keys(window).filter(k => /^[$]?cdc_|selenium|driver/i.test(k)),
    };
}"""


def avaliar(d):
    """Devolve [(gravidade, mensagem)] do que esta ruim."""
    achados = []
    if d["webdriver"]:
        achados.append(("GRAVE", "navigator.webdriver = true (flag de automacao)"))
    if d["cdcVazado"]:
        achados.append(("GRAVE", f"variaveis de automacao no window: {d['cdcVazado']}"))
    r = (d["webglRenderer"] or "").lower()
    if any(x in r for x in ("swiftshader", "llvmpipe", "software", "mesa offscreen")):
        achados.append(("GRAVE", f"renderer de software ({d['webglRenderer']}) - "
                                 "assinatura classica de headless"))
    if not d["webglRenderer"]:
        achados.append(("GRAVE", "WebGL indisponivel - navegador real sempre tem"))
    if "America/" not in (d["fuso"] or ""):
        achados.append(("GRAVE", f"fuso {d['fuso']} incoerente com IP brasileiro"))
    if not d["idiomas"].startswith("pt-BR"):
        achados.append(("ALTO", f"navigator.languages = {d['idiomas']} (esperado pt-BR)"))
    if d["chrome"] != "object":
        achados.append(("ALTO", "window.chrome ausente"))
    if (d["plugins"] or 0) == 0:
        achados.append(("MEDIO", "navigator.plugins vazio"))
    if d["janela"] == d["tela"]:
        achados.append(("MEDIO", "janela do tamanho exato da tela (padrao de kiosk)"))
    if (d["nucleos"] or 0) < 2:
        achados.append(("MEDIO", f"hardwareConcurrency = {d['nucleos']}"))
    return achados


def main():
    doc = sys.argv[1] if len(sys.argv) > 1 else "00000000000"
    persona = P.persona_de(doc, "diag")
    pid, porta = nav.subir(persona)
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{porta}")
            ctx = b.contexts[0]
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://goias.equatorialenergia.com.br/", wait_until="domcontentloaded")
            d = pg.evaluate(SONDA)
            cookies = ctx.cookies()
            b.close()
    finally:
        nav.derrubar(persona, porta, pid)

    print(f"\n--- persona {persona.apelido} / perfil {persona.dir.name} ---")
    for k in ("ua", "uaData", "idiomas", "plataforma", "fuso", "offset", "tela",
              "janela", "viewport", "dpr", "nucleos", "memoria", "plugins",
              "chrome", "webglVendor", "webglRenderer", "webdriver"):
        print(f"  {k:<14} {d[k]}")

    incap = [c["name"] for c in cookies if c["name"].startswith(("visid_incap", "incap_ses", "nlbi_", "reese84"))]
    print(f"  {'cookies Imperva':<14} {incap or 'nenhum (a pagina nao rodou o challenge?)'}")

    achados = avaliar(d)
    print()
    if not achados:
        print("Nenhum sinal ruim encontrado.")
    for gravidade, msg in achados:
        print(f"  [{gravidade}] {msg}")
    return 1 if any(g == "GRAVE" for g, _ in achados) else 0


if __name__ == "__main__":
    sys.exit(main())
