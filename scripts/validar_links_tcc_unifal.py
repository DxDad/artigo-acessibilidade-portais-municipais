#!/usr/bin/env python3
# scripts/validar_link_v4.0.py
# v4.0 (AccessMonitor via extensão - perfil Chromium por processo)

import os
import shutil
import sys
import json
import time
import random
import hashlib
import re
import traceback
import logging
from datetime import datetime
from urllib.parse import quote
from contextlib import contextmanager
from typing import Optional, Tuple, Dict, Any, List

import pymysql
from unidecode import unidecode
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


# ========= Versão =========
ROBO_VERSAO = os.environ.get("ROBO_VERSAO", "v4.0.1").strip() or "v4.0.1"

# ========= Logging =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ========= Config / ENV =========
# Script específico do TCC Unifal.
# Preserva os mecanismos de validação do validar_link_v4.0.py do SAD,
# alterando apenas entradas, pastas e gravação em tabelas próprias do TCC.

ROBO_VERSAO = os.environ.get("ROBO_VERSAO", "tcc-unifal-v1.0").strip() or "tcc-unifal-v1.0"

CAMINHO_PROCESSAMENTO_TCC = os.environ["CAMINHO_PROCESSAMENTO_TCC"].rstrip("/")
CAMINHO_EVIDENCIAS_TCC = os.environ["CAMINHO_EVIDENCIAS_TCC"].rstrip("/")

# Mantém os nomes internos usados pelas funções originais.
CAMINHO_SUPORTE = CAMINHO_PROCESSAMENTO_TCC
CAMINHO_RELATORIOS = os.path.join(CAMINHO_EVIDENCIAS_TCC, "relatorios_ferramentas")

BD_HOST = os.environ["BD_HOST"]
BD_PORTA = int(os.environ["BD_PORTA"])
BD_NOME = os.environ["BD_NOME"]
BD_USUARIO = os.environ["BD_USUARIO"]
BD_SENHA = os.environ["BD_SENHA"]

BD_TABELA_BASE = os.environ.get("BD_TABELA_BASE_TCC", "tcc_unifal_base_amostral").strip() or "tcc_unifal_base_amostral"
BD_TABELA_EXECUCOES = os.environ.get("BD_TABELA_EXECUCOES_TCC", "tcc_unifal_execucoes").strip() or "tcc_unifal_execucoes"
BD_TABELA_LOG = os.environ.get("BD_TABELA_LOG_TCC", "tcc_unifal_logs_validacao").strip() or "tcc_unifal_logs_validacao"

RODADA = os.environ.get("RODADA", "r01").strip() or "r01"
SESSAO = os.environ.get("SESSAO", "").strip() or "sessao"

# parâmetros operacionais
MAX_TENTATIVAS = int(os.environ.get("MAX_TENTATIVAS", "3"))
TIMEOUT_TENTATIVA = int(os.environ.get("TIMEOUT_TENTATIVA_SEGUNDOS", "180"))
ATRASO_RETRY_BASE = int(os.environ.get("ATRASO_ENTRE_TENTATIVAS_SEGUNDOS", "15"))
DELAY_FERRAMENTAS = int(os.environ.get("DELAY_ENTRE_FERRAMENTAS_SEGUNDOS", "10"))

FERR = os.environ.get("FERR", os.environ.get("FERRAMENTA", "ambos")).strip().lower()
RUN_AMA = FERR in ("ambos", "ama", "amaweb")
RUN_ACCESS = FERR in ("ambos", "access", "accessmonitor")
MODO_EXECUCAO = os.environ.get("MODO_EXECUCAO", "fila_completa").strip().lower()

SELETOR_AMA_PRONTO = (os.environ.get("SELETOR_AMA_PRONTO", "") or "").strip()
SELETOR_ACC_PRONTO = (os.environ.get("SELETOR_ACCESS_PRONTO", "") or "").strip()

URL_AMA_BASE = os.environ.get("URL_INICIAL_AMA", "https://amaweb.unifesp.br/avaliador/").strip()
URL_ACC_BASE = os.environ.get("URL_INICIAL_ACCESS", "https://accessmonitor.acessibilidade.gov.pt/").strip()

CAMINHO_EXTENSAO_ACCESSMONITOR = os.environ.get("CAMINHO_EXTENSAO_ACCESSMONITOR", "").strip()
TEMPO_MAX_SERVICE_WORKER_SEGUNDOS = int(os.environ.get("TEMPO_MAX_SERVICE_WORKER_SEGUNDOS", "15"))
TEMPO_MAX_MENSAGEM_SEGUNDOS = int(os.environ.get("TEMPO_MAX_MENSAGEM_SEGUNDOS", "90"))

# ========= Args =========
if len(sys.argv) < 4:
    print("Uso: validar_links_tcc_unifal.py CODIGO_IBGE URL MUNICIPIO [UF] [REGIAO]")
    sys.exit(2)

CODIGO = sys.argv[1].strip()
URL = sys.argv[2].strip()
ENTIDADE = sys.argv[3].strip()  # município
UF = sys.argv[4].strip() if len(sys.argv) >= 5 else ""
REGIAO = sys.argv[5].strip() if len(sys.argv) >= 6 else ""
LINK_N = "homepage"

# ========= Helpers =========
def _agora_sp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug(s: str) -> str:
    s = unidecode(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80] or "entidade"


def _garantir_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _normaliza_nota_str(s: str) -> Optional[str]:
    """
    Normaliza nota para GRAVAÇÃO no BD.

    Agora as colunas amaweb_YYYY-MM-DD / accessmonitor_YYYY-MM-DD são VARCHAR(50),
    então gravamos com VÍRGULA como separador decimal (ex: '9,6').
    """
    s = (s or "").strip()
    if not s:
        return None

    # aceita entrada com vírgula ou ponto, valida como float
    s_float = s.replace(",", ".")
    try:
        v = float(s_float)
    except Exception:
        return None

    if v < 0.0 or v > 10.0:
        return None

    # grava com vírgula
    return f"{v:.1f}".replace(".", ",")


def _calcular_backoff(tentativa: int) -> int:
    base = max(1, ATRASO_RETRY_BASE)
    return min(120, base * tentativa + random.randint(0, 5))


def _construir_url_resultado_ama(url: str) -> str:
    # PDFs mostram: https://amaweb.unifesp.br/avaliador/results/<url_encoded>
    base = URL_AMA_BASE.rstrip("/")
    return f"{base}/results/{quote(url, safe='')}"


def _wait_any_selector(page, selectors: List[str], timeout_ms: int) -> Optional[str]:
    start = time.time()
    for sel in selectors:
        try:
            remaining = max(0.2, (timeout_ms / 1000) - (time.time() - start))
            page.wait_for_selector(sel, timeout=int(remaining * 1000), state="visible")
            return sel
        except Exception:
            pass
    return None




def extrair_nota_de_pdf(pdf_path: str, ferramenta: str) -> Optional[str]:
    """
    Extrai a nota a partir do TEXTO do PDF gerado.
    Retorna string com ponto e 1 casa (ex: '9.2') ou None.
    ferramenta: 'amaweb' ou 'accessmonitor'
    """
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        logger.error(f"PyMuPDF não disponível (fitz): {e}")
        return None

    if not pdf_path or not os.path.exists(pdf_path):
        return None

    try:
        doc = fitz.open(pdf_path)
        texto = "\n".join((page.get_text("text") or "") for page in doc)
        doc.close()
    except Exception as e:
        logger.error(f"Falha ao ler PDF ({pdf_path}): {e}")
        return None

    t = " ".join(texto.replace("\xa0", " ").split())
    t = t.replace(",", ".")  # por segurança, caso o PDF traga vírgula

    # padrões "número + rótulo" (os PDFs dessas ferramentas geralmente seguem isso)
    if ferramenta.lower() == "amaweb":
        # Ex.: "9.2 Nota"
        m = re.search(r"\b(10(?:\.\d)?|[0-9](?:\.\d))\b\s*Nota\b", t, re.IGNORECASE)
    else:
        # Ex.: "9.0 Pontuação"
        m = re.search(r"\b(10(?:\.\d)?|[0-9](?:\.\d))\b\s*Pontuaç(?:ão|ao)\b", t, re.IGNORECASE)

    if not m:
        return None

    try:
        v = float(m.group(1))
    except Exception:
        return None

    if v < 0.1 or v > 10.0:
        return None

    return f"{v:.1f}".replace(".", ",")





def _bool_env(nome: str, padrao: str = "nao") -> bool:
    """
    Lê variável de ambiente booleana em formato textual.
    Aceita: 1, true, yes, y, sim, s.
    """
    return os.environ.get(nome, padrao).strip().lower() in ("1", "true", "sim", "s", "yes", "y")


def _localizar_executavel_chrome() -> Optional[str]:
    """
    Localiza Chrome/Chromium do sistema quando PREFERIR_CHROME_SISTEMA=sim.
    Se não encontrar, retorna None e o Playwright usa o Chromium gerenciado.
    """
    candidatos = [
        os.environ.get("CHROME_EXECUTABLE", "").strip(),
        os.environ.get("CHROME_BIN", "").strip(),
        shutil.which("chromium-browser") or "",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]
    for caminho in candidatos:
        if caminho and os.path.exists(caminho) and os.access(caminho, os.X_OK):
            return caminho
    return None


def _validar_extensao_accessmonitor() -> None:
    """
    Confere se a extensão do AccessMonitor está disponível no caminho informado.
    Não instala, não baixa e não tenta localizar a extensão automaticamente.
    """
    if not CAMINHO_EXTENSAO_ACCESSMONITOR:
        raise RuntimeError("CAMINHO_EXTENSAO_ACCESSMONITOR não informado para execução do AccessMonitor via extensão")
    if not os.path.isdir(CAMINHO_EXTENSAO_ACCESSMONITOR):
        raise RuntimeError(f"Pasta da extensão AccessMonitor não encontrada: {CAMINHO_EXTENSAO_ACCESSMONITOR}")
    manifest = os.path.join(CAMINHO_EXTENSAO_ACCESSMONITOR, "manifest.json")
    if not os.path.exists(manifest):
        raise RuntimeError(f"manifest.json não encontrado na pasta da extensão: {manifest}")


def _obter_service_worker_accessmonitor(context):
    for sw in context.service_workers:
        if "chrome-extension://" in sw.url:
            return sw
    try:
        sw = context.wait_for_event("serviceworker", timeout=TEMPO_MAX_SERVICE_WORKER_SEGUNDOS * 1000)
        if "chrome-extension://" in sw.url:
            return sw
    except Exception:
        pass
    if context.service_workers:
        return context.service_workers[0]
    return None


def _aguardar_pagina_estavel(page, ciclos: int = 2, intervalo_ms: int = 1500) -> None:
    ultimo = None
    estavel = 0
    for _ in range(max(1, ciclos + 4)):
        try:
            atual = page.evaluate("() => document.documentElement ? document.documentElement.outerHTML.length : 0")
        except Exception:
            atual = None
        if atual is not None and atual == ultimo:
            estavel += 1
            if estavel >= ciclos:
                return
        else:
            estavel = 0
            ultimo = atual
        try:
            page.wait_for_timeout(intervalo_ms)
        except Exception:
            time.sleep(intervalo_ms / 1000)


def _resumir_acoes_accessmonitor(bruto: Dict[str, Any]) -> Dict[str, Any]:
    actions = (bruto or {}).get("actions") or {}
    resumo = {}
    for nome, dado in actions.items():
        if isinstance(dado, dict):
            resposta = dado.get("resposta")
            resumo[nome] = {
                "ok": dado.get("ok"),
                "erro": dado.get("erro"),
                "keys": list(resposta.keys())[:20] if isinstance(resposta, dict) else None,
            }
        else:
            resumo[nome] = {"tipo": type(dado).__name__}
    return resumo


# ========= DB =========
def _nota_decimal(nota: Optional[str]):
    if nota is None:
        return None
    s = str(nota).strip().replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    if v < 0 or v > 10:
        return None
    return round(v, 1)


class MySQLConnection:
    def __init__(self):
        self.conn = None

    def get_connection(self):
        if self.conn:
            try:
                self.conn.ping(reconnect=True)
                return self.conn
            except Exception:
                self.conn = None
        self.conn = pymysql.connect(
            host=BD_HOST,
            port=BD_PORTA,
            user=BD_USUARIO,
            password=BD_SENHA,
            database=BD_NOME,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
        )
        return self.conn

    def garantir_execucao(self):
        conn = self.get_connection()
        with conn.cursor() as c:
            sql = f"""
                INSERT INTO `{BD_TABELA_EXECUCOES}`
                    (rodada, sessao, codigo_municipio_completo, status_geral)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    sessao = VALUES(sessao),
                    atualizado_em = CURRENT_TIMESTAMP
            """
            c.execute(sql, (RODADA, SESSAO, CODIGO, "em_execucao"))

    def atualizar_resultado(self, ferramenta: str, nota: Optional[str], status: str, quando: Optional[str] = None):
        nota_dec = _nota_decimal(nota)
        quando = quando or _agora_sp()
        conn = self.get_connection()
        with conn.cursor() as c:
            if ferramenta == "amaweb":
                sql = f"""
                    UPDATE `{BD_TABELA_EXECUCOES}`
                    SET nota_amaweb = %s,
                        status_amaweb = %s,
                        avaliado_amaweb_em = %s,
                        tem_resultado_amaweb = %s,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE rodada = %s AND codigo_municipio_completo = %s
                """
                c.execute(sql, (nota_dec, status, quando, 1 if nota_dec is not None else 0, RODADA, CODIGO))
            elif ferramenta == "accessmonitor":
                sql = f"""
                    UPDATE `{BD_TABELA_EXECUCOES}`
                    SET nota_accessmonitor = %s,
                        status_accessmonitor = %s,
                        avaliado_accessmonitor_em = %s,
                        tem_resultado_accessmonitor = %s,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE rodada = %s AND codigo_municipio_completo = %s
                """
                c.execute(sql, (nota_dec, status, quando, 1 if nota_dec is not None else 0, RODADA, CODIGO))

    def finalizar_status_geral(self):
        conn = self.get_connection()
        with conn.cursor() as c:
            c.execute(
                f"""
                SELECT tem_resultado_amaweb, tem_resultado_accessmonitor, status_amaweb, status_accessmonitor
                FROM `{BD_TABELA_EXECUCOES}`
                WHERE rodada = %s AND codigo_municipio_completo = %s
                LIMIT 1
                """,
                (RODADA, CODIGO),
            )
            row = c.fetchone()
            if not row:
                return
            tem_ama, tem_acc, st_ama, st_acc = row
            if RUN_AMA and RUN_ACCESS:
                if tem_ama and tem_acc:
                    geral = "completo"
                elif tem_ama or tem_acc:
                    geral = "parcial"
                else:
                    geral = "falha_total"
            elif RUN_AMA:
                geral = "completo" if tem_ama else "falha_total"
            elif RUN_ACCESS:
                geral = "completo" if tem_acc else "falha_total"
            else:
                geral = "sem_ferramenta"
            c.execute(
                f"UPDATE `{BD_TABELA_EXECUCOES}` SET status_geral=%s, atualizado_em=CURRENT_TIMESTAMP WHERE rodada=%s AND codigo_municipio_completo=%s",
                (geral, RODADA, CODIGO),
            )

    def inserir_log_validacao(self, dados: Dict[str, Any]):
        campos = [
            "rodada", "sessao", "codigo_municipio_completo", "ferramenta", "tentativa",
            "url_enviada", "url_final", "dominio_final", "houve_redirecionamento", "titulo_pagina",
            "inicio_em", "fim_em", "duracao_segundos", "status_tentativa", "nota_extraida", "nota_bruta",
            "caminho_pdf", "caminho_csv", "caminho_print", "caminho_json", "mensagem_erro", "traceback_resumido",
        ]

        payload = {k: None for k in campos}

        try:
            dados = dados or {}
            for k in campos:
                payload[k] = dados.get(k)

            payload["rodada"] = payload.get("rodada") or RODADA
            payload["sessao"] = payload.get("sessao") or SESSAO
            payload["codigo_municipio_completo"] = payload.get("codigo_municipio_completo") or CODIGO
            payload["ferramenta"] = payload.get("ferramenta") or "sistema"
            payload["tentativa"] = payload.get("tentativa") or 1
            payload["nota_extraida"] = _nota_decimal(payload.get("nota_extraida"))

            nomes = ", ".join(f"`{k}`" for k in campos)
            marc = ", ".join(["%s"] * len(campos))
            sql = f"INSERT INTO `{BD_TABELA_LOG}` ({nomes}) VALUES ({marc})"

            conn = self.get_connection()
            with conn.cursor() as c:
                c.execute(sql, tuple(payload[k] for k in campos))

        except Exception as e:
            logger.error(f"Erro ao inserir log no BD (ignorado): {e}")

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

# ========= Playwright =========
@contextmanager
def playwright_browser(timeout_s: int):
    pw = None
    browser = None
    context = None
    page = None
    try:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        pw = sync_playwright().start()

        if RUN_ACCESS:
            _validar_extensao_accessmonitor()

            # Cada processo paralelo precisa de um perfil persistente próprio.
            # O Chromium bloqueia o reuso simultâneo do mesmo user_data_dir via SingletonLock.
            perfil_id = _slug(
                f"{CODIGO}_{LINK_N}_{hashlib.sha1(URL.encode('utf-8')).hexdigest()[:8]}_{os.getpid()}"
            )
            perfil = os.path.join(
                CAMINHO_SUPORTE,
                "sessoes",
                SESSAO,
                "perfis_chrome_accessmonitor",
                perfil_id,
            )

            # Se sobrar lixo de uma execução interrompida com o mesmo PID/hash, remove antes de abrir.
            if os.path.exists(perfil):
                shutil.rmtree(perfil, ignore_errors=True)

            _garantir_dir(perfil)
            logger.info(f"Perfil temporário Chromium AccessMonitor: {perfil}")

            preferir_chrome_sistema = _bool_env("PREFERIR_CHROME_SISTEMA", "nao")
            chrome_exe = _localizar_executavel_chrome() if preferir_chrome_sistema else None

            args = [
                f"--disable-extensions-except={CAMINHO_EXTENSAO_ACCESSMONITOR}",
                f"--load-extension={CAMINHO_EXTENSAO_ACCESSMONITOR}",
                "--enable-extensions",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1366,900",
            ]

            kwargs = {
                "user_data_dir": perfil,
                "headless": False,
                "args": args,
                "ignore_default_args": ["--disable-extensions"],
                "locale": "pt-PT",
                "timezone_id": "America/Sao_Paulo",
                "user_agent": ua,
                "viewport": {"width": 1366, "height": 900},
                "accept_downloads": True,
            }
            if chrome_exe:
                kwargs["executable_path"] = chrome_exe
                logger.info(f"Usando Chrome/Chromium do sistema para AccessMonitor extensão: {chrome_exe}")
            else:
                logger.info("Usando Chromium gerenciado pelo Playwright para AccessMonitor extensão")

            context = pw.chromium.launch_persistent_context(**kwargs)
            page = context.new_page()
        else:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                locale="pt-PT",
                timezone_id="America/Sao_Paulo",
                user_agent=ua,
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        context.set_extra_http_headers(
            {
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "DNT": "1",
            }
        )

        context.set_default_timeout(timeout_s * 1000)
        context.set_default_navigation_timeout(timeout_s * 1000)
        page.set_default_timeout(timeout_s * 1000)
        page.set_default_navigation_timeout(timeout_s * 1000)

        yield pw, browser, context, page
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
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


# ========= Extração =========
def extrair_nota_ama(page) -> Optional[str]:
    for sel in [".score-circle-value", ".score-circle-value--lg", ".score-circle-value.score-circle-value--lg"]:
        try:
            loc = page.locator(sel)
            if loc.count():
                txt = (loc.first.inner_text() or "").strip()
                txt = txt.replace("Nota:", "").strip()
                n = _normaliza_nota_str(txt)
                if n:
                    return n
        except Exception:
            pass

    # fallback: procurar "Nota" próximo ao texto geral
    try:
        texto = page.text_content("body") or ""
        m = re.search(r"(?:Nota|Pontuação|Score)\s*[:\s]*([0-9]+(?:[.,][0-9]+)?)", texto, flags=re.I)
        if m:
            return _normaliza_nota_str(m.group(1))
    except Exception:
        pass

    return None


_RE_NOTA_ACCESS = re.compile(r"^(?:\d{1,2})\.\d$")  # x.y ou xy.z

def extrair_nota_access(page, timeout_s: int = 60) -> Optional[str]:
    """
    Extrai a nota do AccessMonitor a partir do campo exato do SVG:
    .chart_container .ama.gauge text.element_title  -> ex: '7.2'
    """
    seletor = ".chart_container .ama.gauge text.element_title"

    # Gatilho: só segue quando o "número grande" existir no SVG
    try:
        page.wait_for_selector(seletor, timeout=timeout_s * 1000, state="visible")
    except Exception:
        return None

    try:
        raw = page.locator(seletor).first.inner_text(timeout=3000)
    except Exception:
        return None

    s = (raw or "").strip()

    # valida formato x.y ou xy.z
    if not _RE_NOTA_ACCESS.match(s):
        return None

    # valida faixa 0.1 a 10.0
    try:
        v = float(s)
    except Exception:
        return None

    if v < 0.1 or v > 10.0:
        return None

    return f"{v:.1f}".replace(".", ",")


def pagina_parece_carregada_ama(page) -> bool:
    try:
        if page.locator(".score-circle-value").count():
            return True
        txt = (page.text_content("body") or "").lower()
        return ("nota" in txt and "baixar" in txt) or ("avaliar novamente" in txt)
    except Exception:
        return False


def pagina_parece_carregada_access(page) -> bool:
    try:
        if page.locator("svg text.element_title").count():
            return True
        txt = (page.text_content("body") or "").lower()
        return ("pontuação" in txt) or ("score" in txt) or ("wcag" in txt)
    except Exception:
        return False


# ========= Navegação =========
def _submeter_accessmonitor(page, url: str, timeout_s: int) -> None:
    page.goto(URL_ACC_BASE, wait_until="domcontentloaded")

    # Espera o input ser renderizado (React)
    input_selectors = [
        "input[type='url']",
        "input[type='text']",
        "input[placeholder*='http' i]",
        "#root input",
        "form input",
    ]
    found = _wait_any_selector(page, input_selectors, timeout_s * 1000)
    if not found:
        raise PlaywrightTimeout("AccessMonitor: input não apareceu a tempo")

    # Preenche usando um locator mais específico (evita pegar input “errado”)
    for sel in input_selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill(url)
                break
        except Exception:
            continue

    # botão/analisar
    btn_selectors = [
        "button:has-text('Analisar')",
        "button:has-text('Avaliar')",
        "button:has-text('Validar')",
        "button[type='submit']",
        "#root button",
    ]
    clicked = False
    for sel in btn_selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click()
                clicked = True
                break
        except Exception:
            pass
    if not clicked:
        # alguns UIs submetem por Enter
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass


def _esperar_resultados(page, is_ama: bool, timeout_s: int) -> None:
    start = time.time()
    remaining_ms = timeout_s * 1000

    ready_selectors = []
    if is_ama:
        if SELETOR_AMA_PRONTO:
            ready_selectors.append(SELETOR_AMA_PRONTO)
        ready_selectors += [".score-circle-value", "text=Nota", "text=Nossas ferramentas"]
    else:
        if SELETOR_ACC_PRONTO:
            ready_selectors.append(SELETOR_ACC_PRONTO)
        ready_selectors += ["svg text.element_title", "text=Pontuação", "text=WCAG"]

    ready_selectors = [s for s in ready_selectors if s]

    # tenta achar algum marcador “de página pronta”
    for sel in ready_selectors:
        try:
            remaining_ms = max(1_000, int(timeout_s * 1000 - (time.time() - start) * 1000))
            page.wait_for_selector(sel, timeout=remaining_ms)
            return
        except Exception:
            pass

    # fallback: networkidle (nem sempre funciona bem em SPA, mas ajuda)
    try:
        remaining_ms = max(1_000, int(timeout_s * 1000 - (time.time() - start) * 1000))
        page.wait_for_load_state("networkidle", timeout=remaining_ms)
    except Exception:
        pass


def navegar_e_extrair_ama(page, url: str, timeout_s: int) -> Tuple[str, Optional[str]]:
    # 1) tenta ir direto à página de resultados do AMA (evita dependência do input/render SPA)
    try:
        direct = _construir_url_resultado_ama(url)
        page.goto(direct, wait_until="domcontentloaded")
        _esperar_resultados(page, is_ama=True, timeout_s=timeout_s)
        nota = extrair_nota_ama(page)

        # decisão final única
        if nota:
            return "sucesso", nota

        # sem nota: mantém sua lógica atual
        if pagina_parece_carregada_ama(page):
            return "sucesso_sem_nota", None

        return "falha_de_carregamento", None

    except Exception:
        pass

    # 2) fallback: fluxo via home
    try:
        page.goto(URL_AMA_BASE, wait_until="domcontentloaded")
        # tenta achar input e submeter, mas sem forçar demais
        input_selectors = [
            "input[type='url']",
            "input[type='text']",
            "#root input",
            "form input",
        ]
        found = _wait_any_selector(page, input_selectors, timeout_s * 1000)
        if not found:
            return "falha_de_carregamento", None

        for sel in input_selectors:
            try:
                loc = page.locator(sel)
                if loc.count():
                    loc.first.fill(url)
                    break
            except Exception:
                continue

        btn_selectors = [
            "button:has-text('Validar')",
            "button:has-text('Analisar')",
            "button[type='submit']",
            "text='Validar'",
        ]
        for sel in btn_selectors:
            try:
                loc = page.locator(sel)
                if loc.count():
                    loc.first.click()
                    break
            except Exception:
                pass

        _esperar_resultados(page, is_ama=True, timeout_s=timeout_s)
        nota = extrair_nota_ama(page)
        if nota:
            return "sucesso", nota
        if pagina_parece_carregada_ama(page):
            return "sucesso_sem_nota", None
        return "falha_de_carregamento", None
    except PlaywrightTimeout:
        return "timeout", None
    except Exception as e:
        logger.error(f"AMA: erro na navegação: {e}")
        return "erro", None


def avaliar_accessmonitor_extensao(context, page, url: str, timeout_s: int, evidencia_path: str) -> Tuple[str, Optional[str], Dict[str, Any]]:
    payload_exec = {
        "timestamp": _agora_sp(),
        "url": url,
        "status": "iniciado",
        "nota": None,
        "metodo": "accessmonitor_extensao_parseEvaluationReport_processReportData",
        "erro": None,
        "worker_url": None,
        "resumo_acoes": {},
        "processamento_oficial": {},
    }

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=min(15000, timeout_s * 1000))
        except Exception:
            logger.warning("AccessMonitor extensão: networkidle não atingido; seguindo com estabilização curta.")
        _aguardar_pagina_estavel(page, ciclos=2, intervalo_ms=1500)

        sw = _obter_service_worker_accessmonitor(context)
        if not sw:
            payload_exec["status"] = "erro"
            payload_exec["erro"] = "service_worker_nao_encontrado"
            return "erro", None, payload_exec

        payload_exec["worker_url"] = sw.url

        script = r'''
        async ({ urlAvaliada, timeoutMs }) => {
          const withTimeout = (promise, label) => {
            return Promise.race([
              promise,
              new Promise((_, reject) => setTimeout(() => reject(new Error("timeout_" + label)), timeoutMs))
            ]);
          };

          const limpa = (u) => (u || "").split("#")[0].replace(/\/$/, "");
          const tabs = await chrome.tabs.query({});
          let tab = tabs.find(t => limpa(t.url) === limpa(urlAvaliada));
          if (!tab) tab = tabs.find(t => (t.url || "").startsWith(urlAvaliada));
          if (!tab) tab = tabs.find(t => t.active && t.url && !t.url.startsWith("chrome-extension://"));

          if (!tab) return { ok: false, erro: "aba_avaliada_nao_encontrada", tabs: tabs.map(t => ({ id: t.id, active: t.active, url: t.url, title: t.title })) };

          const sendRaw = async (payload, label) => {
            try { return { ok: true, resposta: await withTimeout(chrome.tabs.sendMessage(tab.id, payload), label) }; }
            catch (e) { return { ok: false, erro: String(e && e.message ? e.message : e) }; }
          };
          const send = async (action, message) => await sendRaw(message === undefined ? { action } : { action, message }, action);
          const resumirModulo = (mod) => {
            if (!mod || typeof mod !== "object") return null;
            const assertions = mod.assertions || {};
            return { type: mod.type, metadata: mod.metadata || null, assertions_count: Object.keys(assertions).length };
          };

          const out = { ok: true, tab: { id: tab.id, url: tab.url, title: tab.title }, actions: {}, resumo_modulos: {}, processamento_oficial: null, score_oficial: null, metodo_score: null };

          out.actions.startEvaluation = await send("startEvaluation");
          out.actions.getHTML = await send("getHTML");
          out.actions.evaluateACT = await send("evaluateACT");
          out.actions.evaluateWCAG = await send("evaluateWCAG");
          out.actions.evaluateBP = await send("evaluateBP");
          out.actions.evaluateCounter = await send("evaluateCounter");
          out.actions.endingEvaluation = await send("endingEvaluation");

          const html = out.actions.getHTML.ok ? (out.actions.getHTML.resposta || "") : "";
          const act = out.actions.evaluateACT.ok ? out.actions.evaluateACT.resposta : null;
          const wcag = out.actions.evaluateWCAG.ok ? out.actions.evaluateWCAG.resposta : null;
          const bp = out.actions.evaluateBP.ok ? out.actions.evaluateBP.resposta : null;
          const counter = out.actions.evaluateCounter.ok ? out.actions.evaluateCounter.resposta : null;

          if (out.actions.getHTML.ok) out.actions.getHTML = { ok: true, tipo: "html", tamanho: html.length, inicio: String(html || "").slice(0, 500) };

          out.resumo_modulos = {
            act: resumirModulo(act),
            wcag: resumirModulo(wcag),
            bp: resumirModulo(bp),
            counter: counter ? { type: counter.type, elementCount: counter.elementCount, data_keys: Object.keys(counter.data || {}) } : null
          };

          const totalReport = {
            system: { url: { completeUrl: tab.url || urlAvaliada || "" }, page: { dom: { html: html, title: tab.title || "", elementCount: (counter && counter.elementCount) ? counter.elementCount : 0 } } },
            modules: { "act-rules": act, "wcag-techniques": wcag, "best-practices": bp, "counter": counter }
          };

          const extrairScore = (obj) => {
            const candidatos = [
              ["processed.data.score", obj && obj.data && obj.data.score],
              ["processed.data.tot.info.score", obj && obj.data && obj.data.tot && obj.data.tot.info && obj.data.tot.info.score],
              ["processed.metadata.score", obj && obj.metadata && obj.metadata.score],
              ["processed.info.score", obj && obj.info && obj.info.score],
              ["processed.score", obj && obj.score]
            ];
            for (const [metodo, valor] of candidatos) if (valor !== undefined && valor !== null && String(valor).trim() !== "") return { metodo, valor };
            return { metodo: null, valor: null };
          };

          const resumirProcessado = (processed) => {
            const data = processed && processed.data ? processed.data : null;
            const tot = data && data.tot ? data.tot : (processed && processed.tot ? processed.tot : null);
            const info = tot && tot.info ? tot.info : (processed && processed.info ? processed.info : null);
            return { tem_processed: !!processed, keys_processed: processed && typeof processed === "object" ? Object.keys(processed).slice(0, 30) : [], tem_data: !!data, keys_data: data && typeof data === "object" ? Object.keys(data).slice(0, 30) : [], rawUrl: data && data.rawUrl, title: data && data.title, score: data && data.score, score_info: info && info.score, metadata_score: processed && processed.metadata && processed.metadata.score, conform: data && data.conform, date: data && data.date, htmlTags: info && info.htmlTags, size: info && info.size, results_count: tot && tot.results ? Object.keys(tot.results).length : null, elems_count: data && data.elems ? Object.keys(data.elems).length : null, nodes_count: data && data.nodes ? Object.keys(data.nodes).length : null, sample_results: tot && tot.results ? Object.fromEntries(Object.entries(tot.results).slice(0, 20)) : null };
          };

          out.actions.parseEvaluationReport = await send("parseEvaluationReport", totalReport);
          const parsed = out.actions.parseEvaluationReport.ok ? out.actions.parseEvaluationReport.resposta : null;

          let processed = null;
          let tentativa = null;
          if (parsed && parsed.data && parsed.data.tot) {
            tentativa = "parsed.data.tot";
            out.actions.processReportData = await send("processReportData", { tot: parsed.data.tot, url: tab.url || urlAvaliada || "" });
            processed = out.actions.processReportData.ok ? out.actions.processReportData.resposta : null;
          }

          let score = extrairScore(processed);
          if (!score.valor && parsed) {
            const scoreParsed = extrairScore(parsed);
            if (scoreParsed.valor) { processed = parsed; tentativa = "parseEvaluationReport"; score = scoreParsed; }
          }

          if (!score.valor) {
            out.actions.processReportData_rawReport = await send("processReportData", { tot: totalReport, url: tab.url || urlAvaliada || "" });
            const processedRaw = out.actions.processReportData_rawReport.ok ? out.actions.processReportData_rawReport.resposta : null;
            const scoreRaw = extrairScore(processedRaw);
            if (scoreRaw.valor) { processed = processedRaw; tentativa = "raw_totalReport"; score = scoreRaw; }
          }

          if (processed) {
            out.score_oficial = score.valor;
            out.metodo_score = tentativa + ":" + (score.metodo || "sem_score");
            out.processamento_oficial = resumirProcessado(processed);
            if (out.actions.parseEvaluationReport && out.actions.parseEvaluationReport.ok) out.actions.parseEvaluationReport = { ok: true, resposta_resumo: resumirProcessado(parsed) };
            if (out.actions.processReportData && out.actions.processReportData.ok) out.actions.processReportData = { ok: true, tentativa, resposta_resumo: resumirProcessado(processed) };
          }

          return out;
        }
        '''

        bruto = sw.evaluate(script, {"urlAvaliada": url, "timeoutMs": TEMPO_MAX_MENSAGEM_SEGUNDOS * 1000})

        payload_exec["resumo_acoes"] = _resumir_acoes_accessmonitor(bruto)
        payload_exec["processamento_oficial"] = (bruto or {}).get("processamento_oficial") or {}
        payload_exec["metodo_score"] = (bruto or {}).get("metodo_score")
        payload_exec["score_bruto"] = (bruto or {}).get("score_oficial")
        payload_exec["tab"] = (bruto or {}).get("tab")
        payload_exec["resumo_modulos"] = (bruto or {}).get("resumo_modulos")

        nota = _normaliza_nota_str(str((bruto or {}).get("score_oficial") or ""))
        payload_exec["nota"] = nota

        if bruto and bruto.get("ok") is False:
            payload_exec["status"] = "erro"
            payload_exec["erro"] = bruto.get("erro")
            return "erro", None, payload_exec

        if nota:
            payload_exec["status"] = "sucesso"
            return "sucesso", nota, payload_exec

        payload_exec["status"] = "sucesso_sem_nota"
        payload_exec["erro"] = "processamento_oficial_sem_score"
        return "sucesso_sem_nota", None, payload_exec

    except PlaywrightTimeout:
        payload_exec["status"] = "timeout"
        payload_exec["erro"] = "timeout"
        return "timeout", None, payload_exec
    except Exception as e:
        payload_exec["status"] = "erro"
        payload_exec["erro"] = f"{type(e).__name__}: {str(e)[:1000]}"
        payload_exec["traceback"] = traceback.format_exc()
        logger.error(f"AccessMonitor extensão: erro na validação: {payload_exec['erro']}")
        return "erro", None, payload_exec
    finally:
        try:
            _salvar_json_exec(evidencia_path, payload_exec)
        except Exception as e:
            logger.error(f"Falha ao salvar evidência JSON AccessMonitor extensão: {e}")


def navegar_e_extrair_access(page, url: str, timeout_s: int) -> Tuple[str, Optional[str]]:
    return "erro", None


# ========= PDF / JSON =========
def gerar_pdf(page, pdf_path: str, ferramenta: str, tentativa: int) -> bool:
    try:
        _garantir_dir(os.path.dirname(pdf_path))
        page.pdf(
            path=pdf_path,
            format="A3",
            print_background=True,
            display_header_footer=True,
            header_template=f"<div style='font-size:10px;margin-left:10px;'>TCC Unifal • {ferramenta} • t{tentativa} • {SESSAO}</div>",
            footer_template=f"<div style='font-size:10px;margin-left:10px;width:100%;'>gerado em {_agora_sp()} • robô {ROBO_VERSAO}</div>",
            margin={"top": "40px", "bottom": "40px", "left": "20px", "right": "20px"},
        )
        return True
    except Exception:
        return False


def _salvar_json_exec(path: str, payload: Dict[str, Any]):
    _garantir_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)



# ========= Runner =========
def _dominio_de_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").lower() or None
    except Exception:
        return None


def _url_atual(page) -> Optional[str]:
    try:
        return page.url
    except Exception:
        return None


def _titulo_atual(page) -> Optional[str]:
    try:
        return page.title()
    except Exception:
        return None


def rodar():
    logger.info(f"Iniciando validação TCC Unifal - Rodada: {RODADA} | Código IBGE: {CODIGO} | Município: {ENTIDADE} | URL: {URL}")

    entidade_slug = _slug(f"{ENTIDADE}_{UF}_{CODIGO}")
    pasta_sessao = os.path.join(CAMINHO_SUPORTE, "sessoes", SESSAO)
    pasta_logs = os.path.join(pasta_sessao, "logs")
    pasta_ama = os.path.join(CAMINHO_RELATORIOS, SESSAO, "amaweb", entidade_slug)
    pasta_acc = os.path.join(CAMINHO_RELATORIOS, SESSAO, "accessmonitor", entidade_slug)
    pasta_json = os.path.join(pasta_sessao, "json")

    for p in (pasta_sessao, pasta_logs, pasta_ama, pasta_acc, pasta_json):
        _garantir_dir(p)

    hash_url = hashlib.sha1(URL.encode("utf-8")).hexdigest()[:8]
    json_path = os.path.join(pasta_json, f"{RODADA}_{CODIGO}_{hash_url}.json")

    db = MySQLConnection()
    db.garantir_execucao()

    exec_log: Dict[str, Any] = {
        "sessao": SESSAO,
        "rodada": RODADA,
        "robo_versao": ROBO_VERSAO,
        "codigo_municipio_completo": CODIGO,
        "municipio": ENTIDADE,
        "uf": UF,
        "regiao": REGIAO,
        "url": URL,
        "ferramenta_solicitada": FERR,
        "tentativas": [],
    }

    try:
        with playwright_browser(TIMEOUT_TENTATIVA) as (_pw, _browser, context, page):
            run_ama_link = RUN_AMA
            run_acc_link = RUN_ACCESS

            resultado_ama = {"tool": "amaweb", "final": None, "tentativas": []}
            if run_ama_link:
                logger.info("Iniciando validação AMAWeb")
                for t in range(1, MAX_TENTATIVAS + 1):
                    inicio_dt = _agora_sp()
                    inicio = time.time()
                    status, nota = navegar_e_extrair_ama(page, URL, TIMEOUT_TENTATIVA)

                    pdf_path = os.path.join(
                        pasta_ama,
                        f"{_slug(ENTIDADE)}_{UF.lower()}_{CODIGO}_{RODADA}_amaweb_t{t}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                    )
                    pdf_ok = gerar_pdf(page, pdf_path, "AMAWeb", t)
                    duracao = round(time.time() - inicio, 2)
                    fim_dt = _agora_sp()
                    url_final = _url_atual(page)

                    if (not nota) and pdf_ok:
                        try:
                            nota_pdf = extrair_nota_de_pdf(pdf_path, "amaweb")
                            if nota_pdf:
                                nota = nota_pdf
                                if not status.startswith("sucesso"):
                                    status = "sucesso"
                                logger.info(f"Nota AMAWeb extraída do PDF: {nota}")
                        except Exception as _e:
                            logger.error(f"Falha no fallback PDF AMAWeb: {_e}")

                    tentativa_payload = {
                        "t": t,
                        "ferramenta": "amaweb",
                        "status": status,
                        "nota": nota,
                        "pdf": pdf_path,
                        "pdf_ok": bool(pdf_ok),
                        "url_final": url_final,
                        "duracao_s": duracao,
                        "timestamp": fim_dt,
                    }
                    resultado_ama["tentativas"].append(tentativa_payload)

                    db.inserir_log_validacao({
                        "ferramenta": "amaweb",
                        "tentativa": t,
                        "url_enviada": URL,
                        "url_final": url_final,
                        "dominio_final": _dominio_de_url(url_final),
                        "houve_redirecionamento": 1 if (url_final and url_final.rstrip('/') != URL.rstrip('/')) else 0,
                        "titulo_pagina": _titulo_atual(page),
                        "inicio_em": inicio_dt,
                        "fim_em": fim_dt,
                        "duracao_segundos": duracao,
                        "status_tentativa": status,
                        "nota_extraida": nota,
                        "nota_bruta": nota,
                        "caminho_pdf": pdf_path if pdf_ok else None,
                        "caminho_json": json_path,
                        "mensagem_erro": None if status.startswith("sucesso") else status,
                    })

                    if status.startswith("sucesso") and nota and pdf_ok:
                        resultado_ama["final"] = nota
                        db.atualizar_resultado("amaweb", nota, "sucesso", fim_dt)
                        break

                    if t == MAX_TENTATIVAS:
                        db.atualizar_resultado("amaweb", None, status or "sem_sucesso", fim_dt)

                    if t < MAX_TENTATIVAS:
                        delay = _calcular_backoff(t)
                        logger.info(f"Aguardando {delay}s antes da próxima tentativa...")
                        time.sleep(delay)

            if run_ama_link and run_acc_link:
                delay = DELAY_FERRAMENTAS + random.randint(1, 3)
                logger.info(f"Aguardando {delay}s entre ferramentas...")
                time.sleep(delay)

            resultado_acc = {"tool": "accessmonitor", "final": None, "tentativas": []}
            if run_acc_link:
                logger.info("Iniciando validação AccessMonitor")
                for t in range(1, MAX_TENTATIVAS + 1):
                    inicio_dt = _agora_sp()
                    inicio = time.time()

                    evidencia_path = os.path.join(
                        pasta_acc,
                        f"{_slug(ENTIDADE)}_{UF.lower()}_{CODIGO}_{RODADA}_accessmonitor_t{t}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                    )

                    status, nota, evidencia_access = avaliar_accessmonitor_extensao(
                        context, page, URL, TIMEOUT_TENTATIVA, evidencia_path
                    )
                    duracao = round(time.time() - inicio, 2)
                    fim_dt = _agora_sp()
                    url_final = None
                    titulo = None
                    try:
                        tab = (evidencia_access or {}).get("tab") or {}
                        url_final = tab.get("url")
                        titulo = tab.get("title")
                    except Exception:
                        pass

                    tentativa_payload = {
                        "t": t,
                        "ferramenta": "accessmonitor",
                        "status": status,
                        "nota": nota,
                        "evidencia_json": evidencia_path,
                        "url_final": url_final,
                        "duracao_s": duracao,
                        "timestamp": fim_dt,
                    }
                    resultado_acc["tentativas"].append(tentativa_payload)

                    db.inserir_log_validacao({
                        "ferramenta": "accessmonitor",
                        "tentativa": t,
                        "url_enviada": URL,
                        "url_final": url_final,
                        "dominio_final": _dominio_de_url(url_final),
                        "houve_redirecionamento": 1 if (url_final and url_final.rstrip('/') != URL.rstrip('/')) else 0,
                        "titulo_pagina": titulo,
                        "inicio_em": inicio_dt,
                        "fim_em": fim_dt,
                        "duracao_segundos": duracao,
                        "status_tentativa": status,
                        "nota_extraida": nota,
                        "nota_bruta": nota,
                        "caminho_json": evidencia_path,
                        "mensagem_erro": None if status.startswith("sucesso") else status,
                        "traceback_resumido": (evidencia_access or {}).get("traceback"),
                    })

                    if status.startswith("sucesso") and nota and os.path.exists(evidencia_path):
                        resultado_acc["final"] = nota
                        db.atualizar_resultado("accessmonitor", nota, "sucesso", fim_dt)
                        break

                    if t == MAX_TENTATIVAS:
                        db.atualizar_resultado("accessmonitor", None, status or "sem_sucesso", fim_dt)

                    if t < MAX_TENTATIVAS:
                        delay = (_calcular_backoff(t) // 2) + random.randint(2, 5)
                        logger.info(f"Aguardando {delay}s antes da próxima tentativa...")
                        time.sleep(delay)

            exec_log["tentativas"] = []
            if run_ama_link:
                exec_log["tentativas"] += resultado_ama["tentativas"]
            if run_acc_link:
                exec_log["tentativas"] += resultado_acc["tentativas"]

            db.finalizar_status_geral()
            _salvar_json_exec(json_path, exec_log)
            logger.info(f"JSON de execução salvo: {json_path}")

            logger.info("=" * 60)
            logger.info("RESUMO DA VALIDAÇÃO TCC UNIFAL")
            logger.info(f"Rodada: {RODADA} | Código IBGE: {CODIGO} | Município: {ENTIDADE}/{UF}")
            if RUN_AMA:
                logger.info(f"AMAWeb: {resultado_ama.get('final') or 'sem sucesso'}")
            if RUN_ACCESS:
                logger.info(f"AccessMonitor: {resultado_acc.get('final') or 'sem sucesso'}")
            logger.info("=" * 60)

    except Exception as e:
        err_txt = f"{type(e).__name__}: {str(e)[:800]}"
        logger.error(f"Erro crítico durante execução: {err_txt}")
        traceback.print_exc()
        db.inserir_log_validacao({
            "ferramenta": "sistema",
            "tentativa": 0,
            "url_enviada": URL,
            "status_tentativa": "erro_critico",
            "mensagem_erro": err_txt,
            "caminho_json": json_path,
            "traceback_resumido": traceback.format_exc()[:4000],
        })
        db.finalizar_status_geral()
        sys.exit(1)

    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    rodar()
