#!/usr/bin/env python3
r"""
Proxy SOCKS5 local cujo trafego de SAIDA nasce amarrado a um endereco.

Por que existe: uma maquina tem UMA rota padrao. O jeito antigo de fazer o robo
sair pelo celular (rede.assumir_rota) e empurrar o adaptador de tethering para a
metrica 1, e isso e global - dois celulares nao podem ser a rota padrao ao mesmo
tempo. Era essa a barreira que impedia rodar uma extracao por aparelho.

A saida e por socket, nao por rota: o Windows, no modelo de host forte (padrao
desde o Vista), faz busca de rota RESTRITA a interface dona do endereco de
origem quando o socket esta amarrado. Medido nesta maquina, com a rota padrao no
Wi-Fi:

    origem 10.1.22.28  (Wi-Fi)     -> 45.191.204.245
    origem 172.25.16.1 (vEthernet) -> OSError [WinError 10051] rede inacessivel

O 10051 e a prova: o Windows recusou mandar em vez de cair na rota padrao. Logo,
amarrar na origem do adaptador de um celular faz o pacote sair por AQUELE
celular, com a rota padrao intacta no Wi-Fi e sem precisar de Administrador.

O Chrome nao sabe amarrar socket, mas sabe falar SOCKS5 (`--proxy-server=`).
Entao cada worker sobe um destes, e o Chrome dele passa a sair pelo celular
certo. Escuta SO em 127.0.0.1 - sem autenticacao, porque nao ha rede onde ouvir.

asyncio e nao thread por conexao: uma pagina abre dezenas de sockets, e quatro
workers a duas threads por conexao viraria centenas de threads para relay puro.

Teste manual: `python proxy_local.py --origem 10.1.22.28`
"""

import asyncio
import errno
import ipaddress
import socket
import struct
import sys
import threading
import time

VERSAO = 0x05
SEM_AUTENTICACAO = 0x00
CONECTAR = 0x01

# Respostas do RFC 1928 que interessam aqui.
OK = 0x00
FALHA_GERAL = 0x01
REDE_INACESSIVEL = 0x03
HOST_INACESSIVEL = 0x04
RECUSADA = 0x05
COMANDO_NAO_SUPORTADO = 0x07
ENDERECO_NAO_SUPORTADO = 0x08

# Os WSA* entram junto dos errno portateis porque no Windows o socket levanta o
# codigo nativo (10051) e nao o traduzido (errno.ENETUNREACH e 101 aqui). Sem
# eles todo erro de rede virava "falha geral" e o log nao dizia o que houve.
WSAENETUNREACH = 10051
WSAEADDRNOTAVAIL = 10049
WSAEHOSTUNREACH = 10065
WSAECONNREFUSED = 10061
WSAETIMEDOUT = 10060

_POR_ERRNO = {
    errno.ENETUNREACH: REDE_INACESSIVEL,
    errno.EHOSTUNREACH: HOST_INACESSIVEL,
    errno.ECONNREFUSED: RECUSADA,
    errno.ETIMEDOUT: HOST_INACESSIVEL,
    WSAENETUNREACH: REDE_INACESSIVEL,
    WSAEHOSTUNREACH: HOST_INACESSIVEL,
    WSAECONNREFUSED: RECUSADA,
    WSAETIMEDOUT: HOST_INACESSIVEL,
}

CONECTAR_TIMEOUT_S = 20


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] proxy: {msg}", flush=True)


def _calar_reset(laco, contexto):
    """Engole o barulho de conexao resetada; qualquer outra coisa vai para o log.

    O proactor do Windows chama sock.shutdown() ao desmontar o transporte, e se
    o outro lado ja resetou (o Chrome faz isso o tempo todo ao trocar de pagina)
    o asyncio despeja um traceback de WinError 10054 por conexao. Nao e falha
    nossa nem do site - e o ciclo de vida normal de um socket. Num lote de horas
    esse traceback repetido esconde o erro que importa, entao fica calado aqui,
    e SO ele: o resto continua aparecendo.
    """
    erro = contexto.get("exception")
    if isinstance(erro, (ConnectionResetError, ConnectionAbortedError)):
        return
    log(f"AVISO: {contexto.get('message')}"
        + (f" ({type(erro).__name__}: {str(erro)[:80]})" if erro else ""))


class ProxyLocal:
    """SOCKS5 em 127.0.0.1 cujas conexoes de saida nascem em `origem`."""

    def __init__(self, origem, host="127.0.0.1"):
        self._origem = origem
        self._host = host
        self.porta = None
        self._laco = None
        self._servidor = None
        self._thread = None
        self.conexoes = 0
        self.recusas = 0
        self._origem_morta = 0

    # ------------------------------------------------------------ ciclo de vida

    def iniciar(self):
        """Sobe o servidor numa thread propria. Devolve a porta escolhida.

        Porta 0: o SO escolhe e ninguem precisa varrer faixa livre. A varredura
        do navegador.porta_livre tem corrida entre processos (connect_ex so diz
        que ninguem escuta AGORA), e aqui nao ha motivo para pagar isso - a
        porta do SOCKS nao vai para linha de comando de ninguem antes de ser
        conhecida.
        """
        pronto = threading.Event()
        erro = {}

        def rodar():
            self._laco = asyncio.new_event_loop()
            asyncio.set_event_loop(self._laco)
            self._laco.set_exception_handler(_calar_reset)
            try:
                self._servidor = self._laco.run_until_complete(
                    asyncio.start_server(self._atender, self._host, 0))
                self.porta = self._servidor.sockets[0].getsockname()[1]
            except Exception as e:  # noqa: BLE001 - vai para quem chamou
                erro["e"] = e
                pronto.set()
                return
            pronto.set()
            self._laco.run_forever()

        self._thread = threading.Thread(target=rodar, daemon=True,
                                        name=f"socks-{self._origem}")
        self._thread.start()
        if not pronto.wait(15):
            raise RuntimeError("proxy SOCKS nao subiu em 15s")
        if "e" in erro:
            raise RuntimeError(f"proxy SOCKS nao subiu: {erro['e']}")
        log(f"{self.url} saindo por {self._origem}")
        return self.porta

    @property
    def url(self):
        return f"socks5://{self._host}:{self.porta}" if self.porta else None

    def definir_origem(self, ip):
        """Troca o endereco de saida sem derrubar o proxy.

        Chamado depois de cada rotacao de IP: o endereco do adaptador vem do
        DHCP do proprio celular, e o tranco do modo aviao pode reerguer o
        tethering numa faixa nova (o Android 11+ sorteia um /24 por sessao de
        ancoragem). Proxy preso no endereco velho recusa toda conexao com
        WSAEADDRNOTAVAIL, e o sintoma chega ao log disfarcado de motivo 'rede'
        em todas as UCs seguintes.
        """
        if ip and ip != self._origem:
            log(f"origem {self._origem} -> {ip}")
            self._origem = ip

    def parar(self):
        """Fecha o servidor e derruba o laco sem deixar corrotina orfa.

        Parar o laco direto deixava as conexoes em andamento penduradas, e o
        coletor de lixo do Python cuspia um "RuntimeError: Event loop is closed"
        por conexao viva. Nada quebrava, mas num lote de horas esse ruido
        esconde o erro que importa. Cancelar antes, e dar um tique para o
        cancelamento correr, resolve.
        """
        if self._laco is None:
            return
        log(f"encerrando ({self.conexoes} conexao(oes), {self.recusas} recusa(s))")

        def encerrar():
            if self._servidor:
                self._servidor.close()
            for tarefa in asyncio.all_tasks(self._laco):
                tarefa.cancel()
            self._laco.call_later(0.2, self._laco.stop)

        self._laco.call_soon_threadsafe(encerrar)
        if self._thread:
            self._thread.join(timeout=5)
        self._laco = None

    def __enter__(self):
        self.iniciar()
        return self

    def __exit__(self, *_):
        self.parar()

    # ------------------------------------------------------------------ SOCKS5

    async def _atender(self, leitor, escritor):
        self.conexoes += 1
        saida = None
        try:
            if not await self._saudar(leitor, escritor):
                return
            alvo = await self._ler_pedido(leitor, escritor)
            if alvo is None:
                return
            host, porta = alvo
            saida = await self._conectar(host, porta, escritor)
            if saida is None:
                return
            remoto_l, remoto_e = saida
            escritor.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await escritor.drain()
            await asyncio.gather(
                self._encanar(leitor, remoto_e),
                self._encanar(remoto_l, escritor),
            )
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            for e in (escritor, saida[1] if saida else None):
                if e is None:
                    continue
                try:
                    e.close()
                except OSError:
                    pass

    async def _saudar(self, leitor, escritor):
        cabecalho = await leitor.readexactly(2)
        if cabecalho[0] != VERSAO:
            return False
        await leitor.readexactly(cabecalho[1])
        escritor.write(bytes([VERSAO, SEM_AUTENTICACAO]))
        await escritor.drain()
        return True

    async def _ler_pedido(self, leitor, escritor):
        """Devolve (host, porta) do CONNECT, ou None depois de responder o erro."""
        versao, comando, _, tipo = await leitor.readexactly(4)
        if versao != VERSAO:
            return None
        if comando != CONECTAR:
            # BIND e UDP ASSOCIATE nao existem aqui: o Chrome nao usa nenhum dos
            # dois para navegar, e implementar UDP nao ajudaria - ver o aviso
            # sobre WebRTC no navegador.py.
            await self._recusar(escritor, COMANDO_NAO_SUPORTADO)
            return None
        if tipo == 0x01:
            host = socket.inet_ntoa(await leitor.readexactly(4))
        elif tipo == 0x03:
            tamanho = (await leitor.readexactly(1))[0]
            # ASCII e nao idna: o nome ja chega em punycode pelo protocolo, e o
            # codec idna do Python nem aceita tratamento de erro - decodificar
            # com ele derruba a conexao em vez de resolver o dominio.
            host = (await leitor.readexactly(tamanho)).decode("ascii", "replace")
        elif tipo == 0x04:
            host = str(ipaddress.IPv6Address(await leitor.readexactly(16)))
        else:
            await self._recusar(escritor, ENDERECO_NAO_SUPORTADO)
            return None
        porta = struct.unpack("!H", await leitor.readexactly(2))[0]
        return host, porta

    async def _conectar(self, host, porta, escritor):
        """Abre a conexao de saida amarrada na origem, ou responde o erro."""
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(host, porta,
                                        local_addr=(self._origem, 0)),
                timeout=CONECTAR_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.recusas += 1
            await self._recusar(escritor, HOST_INACESSIVEL)
        except OSError as e:
            self.recusas += 1
            if e.errno in (errno.EADDRNOTAVAIL, WSAEADDRNOTAVAIL):
                # O adaptador perdeu o endereco em que estamos amarrados. Sem
                # este aviso o sintoma chega como falha de rede generica em
                # TODAS as UCs seguintes, e o motivo real fica invisivel.
                agora = time.time()
                if agora - self._origem_morta > 30:
                    self._origem_morta = agora
                    log(f"ERRO: origem {self._origem} nao existe mais neste "
                        "adaptador. O tethering caiu ou voltou com endereco "
                        "novo; quem rotaciona precisa chamar definir_origem().")
            await self._recusar(escritor, _POR_ERRNO.get(e.errno, FALHA_GERAL))
        return None

    async def _recusar(self, escritor, codigo):
        try:
            escritor.write(bytes([VERSAO, codigo, 0x00, 0x01]) + b"\x00" * 6)
            await escritor.drain()
        except (ConnectionError, OSError):
            pass

    @staticmethod
    async def _encanar(leitor, escritor):
        try:
            while True:
                dados = await leitor.read(65536)
                if not dados:
                    break
                escritor.write(dados)
                await escritor.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                escritor.close()
            except OSError:
                pass


def main():
    args = sys.argv[1:]
    origem = None
    if "--origem" in args:
        origem = args[args.index("--origem") + 1]
    elif "--ifindex" in args:
        import rede
        origem = rede.origem_de(int(args[args.index("--ifindex") + 1]))
    if not origem:
        sys.exit("uso: python proxy_local.py --origem <ip local> | --ifindex <N>")

    import rede

    with ProxyLocal(origem) as p:
        print(f"SOCKS pronto: {p.url}")
        print(f"IP publico por esta origem: {rede.ip_publico(origem=origem)}")
        print("Use com: chrome --proxy-server=" + p.url)
        input("Enter para encerrar...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
