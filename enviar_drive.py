#!/usr/bin/env python3
"""
Sobe as faturas de um mes para uma pasta do Google Drive e gera o CSV
uc;mes;ano;link.

O link so existe depois do upload - por isso as duas coisas moram no mesmo
script: subir e depois montar a planilha em passos separados abriria espaco
para a planilha citar arquivo que nao foi enviado (ou o contrario).

Antes de enviar, lista o que ja esta na pasta e reaproveita a fatura que ja
esta la. Rodar duas vezes nao duplica nada nem gasta upload de novo, entao dar
`--mes 08/2026` outra vez depois de baixar mais 10 faturas envia so as 10.

O nome no Drive nao e o nome do arquivo local: a pasta ja segue
`<UC>_FATURA_<mes>_<ano>.pdf` (mais de mil arquivos, alguns com o mes zerado a
esquerda, outros nao). Subir como `fatura-<uc>-<mes>-<ano>.pdf` criaria uma
segunda copia de cada UC ja enviada, entao o script grava no padrao da pasta e
compara por (UC, mes, ano) - a UC com zeros a esquerda ou sem eles e a mesma UC.

Credencial: caminho do JSON em GOOGLE_DRIVE_CREDENCIAIS (credenciais.env).
Aceita os dois tipos de JSON que o Google entrega:

  service account   servico a servico, sem navegador. Conta de servico nao tem
                    cota de armazenamento propria, entao a pasta destino precisa
                    estar num Shared Drive ou compartilhada como editor com o
                    e-mail da conta - em My Drive comum o upload falha com
                    storageQuotaExceeded.
  OAuth client      ("installed"/"web") abre o navegador uma vez e guarda o
                    token em estado/drive_token.json; o dono dos arquivos passa
                    a ser voce, sem o problema de cota acima.

Uso:
    python enviar_drive.py --mes 08/2026
    python enviar_drive.py --mes 08/2026 --csv saida/faturas-drive-08-2026.csv
    python enviar_drive.py --mes 08/2026 --seco     # so lista o que faria
"""

import argparse
import csv
import glob
import os
import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
FATURAS = AQUI / "faturas"
ESTADO = AQUI / "estado"
ENV = AQUI / "credenciais.env"
TOKEN = ESTADO / "drive_token.json"
PADRAO_PDF = re.compile(r"fatura-(\d+)-(\d+)-(\d+)\.pdf$")
PADRAO_DRIVE = re.compile(r"(\d+)_FATURA_(\d+)_(\d+)\.pdf$", re.I)
ESCOPO = ["https://www.googleapis.com/auth/drive.file"]
PASTA_PADRAO = "1vligcM_szlS_R9SUqq03StdG-HX6FAOX"


def carregar_env():
    """Le credenciais.env ("export CHAVE=valor") sem sobrescrever o ambiente.

    As .ps1 fazem isso antes de chamar o python, mas este script tambem roda
    solto no terminal - e a credencial do Drive so mora ali.
    """
    if not ENV.exists():
        return
    for linha in ENV.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        linha = re.sub(r"^export\s+", "", linha)
        if "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave = chave.strip()
        valor = valor.strip().strip('"').strip("'")
        if chave and chave not in os.environ:
            os.environ[chave] = valor


def parse_mes(bruto):
    """(mes, ano) de 08/2026, 8-2026, 2026-08. Formato invalido levanta erro."""
    achado = re.fullmatch(r"\s*(\d{1,4})\s*[/\-.]\s*(\d{1,4})\s*", str(bruto or ""))
    if not achado:
        raise ValueError(f"Mes invalido: {bruto!r}. Use MM/AAAA (ex.: 08/2026)")
    a, b = int(achado.group(1)), int(achado.group(2))
    mes, ano = (b, a) if a > 12 else (a, b)
    if not 1 <= mes <= 12 or not 1900 <= ano <= 2999:
        raise ValueError(f"Mes invalido: {bruto!r}. Use MM/AAAA (ex.: 08/2026)")
    return mes, ano


def pdfs_do_mes(mes, ano):
    """[(uc, caminho)] das faturas do mes, pelo nome do arquivo, ordenado."""
    achados = []
    for caminho in glob.glob(str(FATURAS / "fatura-*.pdf")):
        m = PADRAO_PDF.search(os.path.basename(caminho))
        if not m:
            continue
        if int(m.group(2)) == mes and int(m.group(3)) == ano:
            achados.append((m.group(1), Path(caminho)))
    achados.sort()
    return achados


def chave(uc, mes, ano):
    """Identidade da fatura no Drive, imune a zero a esquerda na UC e no mes."""
    return (str(uc).lstrip("0"), int(mes), int(ano))


def nome_no_drive(uc, mes, ano):
    return f"{uc}_FATURA_{mes}_{ano}.pdf"


def credenciais(caminho):
    """Credencial do Drive a partir do JSON, seja ele service account ou OAuth."""
    import json

    from google.oauth2 import service_account

    caminho = Path(caminho).expanduser()
    if not caminho.exists():
        sys.exit(f"JSON de autorizacao nao encontrado: {caminho}")
    dados = json.loads(caminho.read_text(encoding="utf-8"))

    if dados.get("type") == "service_account":
        return service_account.Credentials.from_service_account_file(
            str(caminho), scopes=ESCOPO)

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    cred = None
    if TOKEN.exists():
        cred = Credentials.from_authorized_user_file(str(TOKEN), ESCOPO)
    if cred and cred.expired and cred.refresh_token:
        cred.refresh(Request())
    if not cred or not cred.valid:
        fluxo = InstalledAppFlow.from_client_secrets_file(str(caminho), ESCOPO)
        cred = fluxo.run_local_server(port=0)
        ESTADO.mkdir(parents=True, exist_ok=True)
        TOKEN.write_text(cred.to_json(), encoding="utf-8")
    return cred


def ja_na_pasta(servico, pasta):
    """Mapa (uc, mes, ano) -> link dos PDFs de fatura ja na pasta.

    Ignora o que nao casa com o padrao de nome (a pasta tambem guarda XML) -
    esses nao sao candidatos a reuso.
    """
    achados = {}
    pagina = None
    while True:
        r = servico.files().list(
            q=f"'{pasta}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageSize=1000, pageToken=pagina,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in r.get("files", []):
            m = PADRAO_DRIVE.match(f["name"])
            if not m:
                continue
            achados[chave(m.group(1), m.group(2), m.group(3))] = (
                f.get("webViewLink") or link_de(f["id"]))
        pagina = r.get("nextPageToken")
        if not pagina:
            return achados


def link_de(id_arquivo):
    return f"https://drive.google.com/file/d/{id_arquivo}/view"


def enviar(servico, caminho, nome, pasta):
    """Sobe um PDF com o nome do padrao da pasta e devolve o link."""
    from googleapiclient.http import MediaFileUpload

    midia = MediaFileUpload(str(caminho), mimetype="application/pdf",
                            resumable=True)
    arquivo = servico.files().create(
        body={"name": nome, "parents": [pasta]},
        media_body=midia, fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return arquivo.get("webViewLink") or link_de(arquivo["id"])


def escrever_csv(linhas, destino):
    """CSV com ; e BOM: e o que o Excel em pt-BR abre sem pedir nada."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["uc", "mes", "ano", "link"],
                           delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    return destino


def main():
    carregar_env()
    p = argparse.ArgumentParser(description="Sobe faturas do mes para o Drive.")
    p.add_argument("--mes", default=os.environ.get("EQUATORIAL_MES"),
                   help="MM/AAAA das faturas a enviar (ex.: 08/2026)")
    p.add_argument("--pasta", default=os.environ.get("GOOGLE_DRIVE_PASTA_ID",
                                                     PASTA_PADRAO),
                   help="ID da pasta do Drive")
    p.add_argument("--csv", help="destino do CSV (padrao: saida/faturas-drive-MM-AAAA.csv)")
    p.add_argument("--seco", action="store_true",
                   help="lista o que seria enviado e sai, sem tocar no Drive")
    args = p.parse_args()

    if not args.mes:
        sys.exit("Diga o mes: --mes 08/2026 (ou defina EQUATORIAL_MES).")
    mes, ano = parse_mes(args.mes)

    pdfs = pdfs_do_mes(mes, ano)
    if not pdfs:
        sys.exit(f"Nenhuma fatura de {mes:02d}/{ano} em {FATURAS}.")
    print(f"{len(pdfs)} faturas de {mes:02d}/{ano} em {FATURAS}")

    destino = Path(args.csv) if args.csv else AQUI / "saida" / f"faturas-drive-{mes:02d}-{ano}.csv"
    if args.seco:
        for uc, caminho in pdfs:
            print(f"  {caminho.name} -> {nome_no_drive(uc, mes, ano)}")
        print(f"(seco) CSV iria para {destino}")
        return

    json_cred = os.environ.get("GOOGLE_DRIVE_CREDENCIAIS", "").strip()
    if not json_cred:
        sys.exit("GOOGLE_DRIVE_CREDENCIAIS nao definida - aponte para o JSON "
                 "de autorizacao do Google em credenciais.env.")

    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    servico = build("drive", "v3", credentials=credenciais(json_cred),
                    cache_discovery=False)
    try:
        existentes = ja_na_pasta(servico, args.pasta)
    except HttpError as e:
        sys.exit(f"Nao consegui ler a pasta {args.pasta}: {e}")
    print(f"{len(existentes)} faturas ja na pasta {args.pasta}")

    linhas, enviados, reusados, falhas = [], 0, 0, []
    for uc, caminho in pdfs:
        nome = nome_no_drive(uc, mes, ano)
        link = existentes.get(chave(uc, mes, ano))
        if link:
            reusados += 1
        else:
            try:
                link = enviar(servico, caminho, nome, args.pasta)
                enviados += 1
                print(f"  enviado {nome}")
            except HttpError as e:
                # Uma UC que falha nao pode derrubar as outras 187 - anota e
                # segue; a linha dela fica de fora do CSV, que so lista link
                # que existe.
                falhas.append((nome, str(e)))
                print(f"  FALHOU {nome}: {e}")
                continue
        linhas.append({"uc": uc, "mes": mes, "ano": ano, "link": link})

    if linhas:
        escrever_csv(linhas, destino)
        print(f"\n{len(linhas)} linhas em {destino}")
    print(f"enviados: {enviados}  ja estavam la: {reusados}  falhas: {len(falhas)}")
    if falhas:
        sys.exit(1)


if __name__ == "__main__":
    main()
