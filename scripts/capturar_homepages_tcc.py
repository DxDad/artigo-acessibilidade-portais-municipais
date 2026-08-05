#!/usr/bin/env python3
# scripts/capturar_homepages_tcc.py

import csv
import json
import logging
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import pymysql
from unidecode import unidecode
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


# =========================
# Configuração geral
# =========================

BD_HOST = os.environ["BD_HOST"]
BD_PORTA = int(os.environ["BD_PORTA"])
BD_NOME = os.environ["BD_NOME"]
BD_USUARIO = os.environ["BD_USUARIO"]
BD_SENHA = os.environ["BD_SENHA"]
BD_TABELA_BASE_TCC = os.environ["BD_TABELA_BASE_TCC"]

CAMINHO_PROCESSAMENTO_TCC = Path(os.environ["CAMINHO_PROCESSAMENTO_TCC"])
CAMINHO_EVIDENCIAS_TCC = Path(os.environ["CAMINHO_EVIDENCIAS_TCC"])

SESSAO = os.environ["SESSAO"]
MODO_FILA = os.environ.get("MODO_FILA", "primeiros_n").strip().lower()
CODIGOS_IBGE = os.environ.get("CODIGOS_IBGE", "").strip()
PRIMEIROS_N = int(os.environ.get("PRIMEIROS_N", "5"))

VIEWPORT_LARGURA = int(os.environ.get("VIEWPORT_LARGURA", "1366"))
VIEWPORT_ALTURA = int(os.environ.get("VIEWPORT_ALTURA", "768"))
TIMEOUT_CAPTURA_SEGUNDOS = int(os.environ.get("TIMEOUT_CAPTURA_SEGUNDOS", "90"))
ESPERA_APOS_CARREGAMENTO_SEGUNDOS = int(os.environ.get("ESPERA_APOS_CARREGAMENTO_SEGUNDOS", "6"))
FULL_PAGE_CAPTURA = os.environ.get("FULL_PAGE_CAPTURA", "false").strip().lower() == "true"

MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "3"))
CONSOLIDAR_CAPTURAS = os.environ.get("CONSOLIDAR_CAPTURAS", "nao").strip().lower() == "sim"
SUBSTITUIR_CONSOLIDADA = os.environ.get("SUBSTITUIR_CONSOLIDADA", "nao").strip().lower() == "sim"


# =========================
# Diretórios
# =========================

DIR_SESSAO = CAMINHO_PROCESSAMENTO_TCC / "sessoes" / SESSAO
DIR_LOGS = DIR_SESSAO / "logs"
DIR_MANIFESTOS = DIR_SESSAO / "manifestos"
DIR_TEMP = DIR_SESSAO / "temp"
DIR_RESUMO = DIR_SESSAO / "resumo"

DIR_RODADA_EVIDENCIAS = CAMINHO_EVIDENCIAS_TCC / "capturas_homepages" / "rodadas" / SESSAO
DIR_RODADA_IMAGENS = DIR_RODADA_EVIDENCIAS / "imagens"
DIR_CONSOLIDADAS = CAMINHO_EVIDENCIAS_TCC / "capturas_homepages" / "consolidadas"

for d in [
    DIR_SESSAO,
    DIR_LOGS,
    DIR_MANIFESTOS,
    DIR_TEMP,
    DIR_RESUMO,
    DIR_RODADA_EVIDENCIAS,
    DIR_RODADA_IMAGENS,
    DIR_CONSOLIDADAS,
]:
    d.mkdir(parents=True, exist_ok=True)


# =========================
# Logging
# =========================

LOG_PATH = DIR_LOGS / f"captura_homepages_{SESSAO}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("capturar_homepages_tcc")


# =========================
# Helpers
# =========================

UF_POR_CODIGO = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA",
    "31": "MG", "32": "ES", "33": "RJ", "35": "SP",
    "41": "PR", "42": "SC", "43": "RS",
    "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}

def agora_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_nome() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def slug(texto: str, limite: int = 90) -> str:
    texto = unidecode(texto or "").lower()
    texto = re.sub(r"[^a-z0-9]+", "_", texto).strip("_")
    return texto[:limite] or "sem_nome"


def regiao_slug(regiao: str) -> str:
    r = slug(regiao)
    mapa = {
        "centro_oeste": "centro-oeste",
        "nordeste": "nordeste",
        "norte": "norte",
        "sudeste": "sudeste",
        "sul": "sul",
    }
    return mapa.get(r, r or "sem_regiao")


def normalizar_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def uf_do_registro(row: Dict[str, Any]) -> str:
    codigo_uf = str(row.get("codigo_uf") or "").strip()
    if codigo_uf in UF_POR_CODIGO:
        return UF_POR_CODIGO[codigo_uf]

    codigo_ibge = str(row.get("codigo_municipio_completo") or "").strip()
    if len(codigo_ibge) >= 2:
        return UF_POR_CODIGO.get(codigo_ibge[:2], "")

    nome_uf = str(row.get("nome_uf") or "").strip()
    if len(nome_uf) == 2:
        return nome_uf.upper()

    return "UF"


def nome_arquivo(row: Dict[str, Any], uf: str, ts: str) -> str:
    municipio = slug(str(row.get("nome_municipio") or "municipio"))
    codigo_ibge = str(row.get("codigo_municipio_completo") or "sem_codigo").strip()
    return f"{municipio}_{uf.lower()}_{codigo_ibge}_homepage_{ts}.png"


def conectar_banco():
    return pymysql.connect(
        host=BD_HOST,
        port=BD_PORTA,
        user=BD_USUARIO,
        password=BD_SENHA,
        database=BD_NOME,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def carregar_fila() -> List[Dict[str, Any]]:
    where_extra = ""
    limite = ""

    codigos = [
        c.strip()
        for c in CODIGOS_IBGE.split(",")
        if c.strip()
    ]

    if codigos:
        codigos_sql = ",".join(["%s"] * len(codigos))
        where_extra = f"AND CAST(codigo_municipio_completo AS CHAR) IN ({codigos_sql})"
        params = codigos
    else:
        params = []
        if MODO_FILA == "primeiros_n":
            limite = f"LIMIT {PRIMEIROS_N}"

    sql = f"""
        SELECT
            codigo_municipio_completo,
            codigo_uf,
            nome_uf,
            codigo_municipio,
            nome_municipio,
            regiao,
            url
        FROM `{BD_TABELA_BASE_TCC}`
        WHERE url IS NOT NULL
          AND TRIM(url) <> ''
          {where_extra}
        ORDER BY regiao, nome_uf, nome_municipio
        {limite}
    """

    logger.info("Tabela base TCC: %s", BD_TABELA_BASE_TCC)
    logger.info("Códigos IBGE informados: %s", CODIGOS_IBGE or "não informado")
    logger.info("SQL de fila: %s", " ".join(sql.split()))

    with conectar_banco() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return rows


def capturar(row: Dict[str, Any]) -> Dict[str, Any]:
    inicio = time.time()
    ts = timestamp_nome()

    codigo_ibge = str(row.get("codigo_municipio_completo") or "").strip()
    municipio = str(row.get("nome_municipio") or "").strip()
    regiao = str(row.get("regiao") or "").strip()
    regiao_dir = regiao_slug(regiao)
    uf = uf_do_registro(row)
    url_original = str(row.get("url") or "").strip()
    url = normalizar_url(url_original)

    pasta_regiao = DIR_RODADA_IMAGENS / regiao_dir
    pasta_regiao.mkdir(parents=True, exist_ok=True)

    arquivo = nome_arquivo(row, uf, ts)
    caminho_imagem = pasta_regiao / arquivo

    resultado = {
        "codigo_municipio_completo": codigo_ibge,
        "nome_municipio": municipio,
        "uf": uf,
        "nome_uf": row.get("nome_uf"),
        "regiao": regiao,
        "url_original": url_original,
        "url_acessada": url,
        "url_final": "",
        "titulo_pagina": "",
        "arquivo_imagem": str(caminho_imagem),
        "arquivo_consolidado": "",
        "data_hora_captura": agora_iso(),
        "status": "falha",
        "http_status": "",
        "erro": "",
        "tempo_segundos": "",
        "viewport_largura": VIEWPORT_LARGURA,
        "viewport_altura": VIEWPORT_ALTURA,
        "full_page": FULL_PAGE_CAPTURA,
        "sessao": SESSAO,
    }

    if not url:
        resultado["erro"] = "URL vazia"
        return resultado

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

            context = browser.new_context(
                viewport={"width": VIEWPORT_LARGURA, "height": VIEWPORT_ALTURA},
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
            )

            page = context.new_page()
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=TIMEOUT_CAPTURA_SEGUNDOS * 1000,
            )

            if response is not None:
                resultado["http_status"] = str(response.status)

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            time.sleep(ESPERA_APOS_CARREGAMENTO_SEGUNDOS)

            resultado["url_final"] = page.url
            try:
                resultado["titulo_pagina"] = page.title()
            except Exception:
                resultado["titulo_pagina"] = ""

            page.screenshot(path=str(caminho_imagem), full_page=FULL_PAGE_CAPTURA)

            context.close()
            browser.close()

        if caminho_imagem.exists() and caminho_imagem.stat().st_size > 0:
            resultado["status"] = "ok"
        else:
            resultado["status"] = "falha"
            resultado["erro"] = "Arquivo de imagem não foi criado ou está vazio"

        if resultado["status"] == "ok" and CONSOLIDAR_CAPTURAS:
            pasta_consolidada_regiao = DIR_CONSOLIDADAS / regiao_dir
            pasta_consolidada_regiao.mkdir(parents=True, exist_ok=True)

            caminho_consolidado = pasta_consolidada_regiao / arquivo

            if caminho_consolidado.exists() and not SUBSTITUIR_CONSOLIDADA:
                resultado["arquivo_consolidado"] = str(caminho_consolidado)
                resultado["erro"] = "Imagem consolidada já existia; não substituída"
            else:
                shutil.copy2(caminho_imagem, caminho_consolidado)
                resultado["arquivo_consolidado"] = str(caminho_consolidado)

    except PlaywrightTimeout as e:
        resultado["erro"] = f"Timeout: {e}"
    except Exception as e:
        resultado["erro"] = f"{type(e).__name__}: {e}"

    resultado["tempo_segundos"] = f"{time.time() - inicio:.2f}"
    return resultado


def salvar_manifestos(resultados: List[Dict[str, Any]]) -> None:
    campos = [
        "codigo_municipio_completo",
        "nome_municipio",
        "uf",
        "nome_uf",
        "regiao",
        "url_original",
        "url_acessada",
        "url_final",
        "titulo_pagina",
        "arquivo_imagem",
        "arquivo_consolidado",
        "data_hora_captura",
        "status",
        "http_status",
        "erro",
        "tempo_segundos",
        "viewport_largura",
        "viewport_altura",
        "full_page",
        "sessao",
    ]

    csv_path = DIR_MANIFESTOS / f"manifesto_capturas_{SESSAO}.csv"
    json_path = DIR_MANIFESTOS / f"manifesto_capturas_{SESSAO}.json"

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(resultados)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)

    shutil.copy2(csv_path, DIR_RODADA_EVIDENCIAS / csv_path.name)
    shutil.copy2(json_path, DIR_RODADA_EVIDENCIAS / json_path.name)

    if CONSOLIDAR_CAPTURAS:
        consol_csv = DIR_CONSOLIDADAS / "manifesto_consolidado.csv"
        with consol_csv.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=campos)
            writer.writeheader()
            writer.writerows([r for r in resultados if r.get("status") == "ok"])


def salvar_resumo(resultados: List[Dict[str, Any]]) -> None:
    total = len(resultados)
    ok = sum(1 for r in resultados if r.get("status") == "ok")
    falha = total - ok

    resumo = {
        "sessao": SESSAO,
        "modo_fila": MODO_FILA,
        "primeiros_n": PRIMEIROS_N if MODO_FILA == "primeiros_n" else None,
        "total": total,
        "ok": ok,
        "falha": falha,
        "consolidar_capturas": CONSOLIDAR_CAPTURAS,
        "substituir_consolidada": SUBSTITUIR_CONSOLIDADA,
        "viewport_largura": VIEWPORT_LARGURA,
        "viewport_altura": VIEWPORT_ALTURA,
        "full_page": FULL_PAGE_CAPTURA,
        "data_hora_fim": agora_iso(),
        "diretorio_rodada": str(DIR_RODADA_EVIDENCIAS),
        "diretorio_consolidadas": str(DIR_CONSOLIDADAS),
        "log": str(LOG_PATH),
    }

    resumo_path = DIR_RESUMO / f"resumo_capturas_{SESSAO}.json"
    with resumo_path.open("w", encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=2)

    shutil.copy2(resumo_path, DIR_RODADA_EVIDENCIAS / resumo_path.name)

    logger.info("Resumo: %s", json.dumps(resumo, ensure_ascii=False))


def main():
    logger.info("Iniciando captura de homepages TCC Unifal")
    logger.info("Sessão: %s", SESSAO)
    logger.info("Modo fila: %s", MODO_FILA)
    logger.info("Consolidar capturas: %s", CONSOLIDAR_CAPTURAS)
    logger.info("Diretório da sessão: %s", DIR_SESSAO)
    logger.info("Diretório de evidências da rodada: %s", DIR_RODADA_EVIDENCIAS)

    try:
        fila = carregar_fila()
        logger.info("Total de registros carregados: %s", len(fila))
    except Exception as e:
        logger.exception("Falha ao carregar a fila a partir da tabela base do TCC: %s", e)
        sys.exit(1)

    if not fila:
        logger.error("Nenhum registro encontrado para captura.")
        sys.exit(1)

    resultados = []

    workers = max(1, min(MAX_PARALLEL, len(fila)))
    logger.info("Capturas em paralelo: %s", workers)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futuros = {executor.submit(capturar, row): row for row in fila}

        for futuro in as_completed(futuros):
            row = futuros[futuro]
            municipio = row.get("nome_municipio")
            try:
                resultado = futuro.result()
            except Exception as e:
                resultado = {
                    "codigo_municipio_completo": row.get("codigo_municipio_completo"),
                    "nome_municipio": municipio,
                    "uf": uf_do_registro(row),
                    "nome_uf": row.get("nome_uf"),
                    "regiao": row.get("regiao"),
                    "url_original": row.get("url"),
                    "url_acessada": normalizar_url(row.get("url")),
                    "url_final": "",
                    "titulo_pagina": "",
                    "arquivo_imagem": "",
                    "arquivo_consolidado": "",
                    "data_hora_captura": agora_iso(),
                    "status": "falha",
                    "http_status": "",
                    "erro": f"Erro não tratado: {type(e).__name__}: {e}",
                    "tempo_segundos": "",
                    "viewport_largura": VIEWPORT_LARGURA,
                    "viewport_altura": VIEWPORT_ALTURA,
                    "full_page": FULL_PAGE_CAPTURA,
                    "sessao": SESSAO,
                }

            resultados.append(resultado)
            if resultado.get("status") == "ok":
                logger.info(
                    "[ok] %s/%s - %s",
                    resultado.get("nome_municipio"),
                    resultado.get("uf"),
                    resultado.get("arquivo_imagem"),
                )
            else:
                logger.warning(
                    "[falha] %s/%s - %s",
                    resultado.get("nome_municipio"),
                    resultado.get("uf"),
                    resultado.get("erro"),
                )

    resultados.sort(key=lambda r: (
        str(r.get("regiao") or ""),
        str(r.get("uf") or ""),
        str(r.get("nome_municipio") or ""),
    ))

    salvar_manifestos(resultados)
    salvar_resumo(resultados)

    falhas = [r for r in resultados if r.get("status") != "ok"]
    if falhas:
        logger.warning("Execução concluída com falhas: %s", len(falhas))
        sys.exit(1)

    logger.info("Execução concluída sem falhas.")


if __name__ == "__main__":
    main()
